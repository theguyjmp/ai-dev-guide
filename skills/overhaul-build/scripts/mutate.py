"""mutate.py: a small AST mutation tester (overhaul v3 probe). Python standard library only; works on Windows.

    py -3 mutate.py --repo R --paths src/pkg/core/ src/pkg/money.py --tests tests/unit/core tests/property/test_f01.py
                    [--cmd "py -3 -m pytest -q -x -p no:cacheprovider {tests}"] [--max-mutants 40] [--timeout 0]
                    [--seed 0] [--out result.json]

Copies R (without .git, .overhaul and caches) to a temp dir, runs the tests once on the copy (the baseline), then for
each sampled mutant writes one mutated file, runs the tests in a fresh subprocess with a timeout and restores the file.
A mutant is killed when the tests fail or time out, and survives when they pass.

Mutations: flip a comparison (== !=, < >=, > <=, is, in), boundary off-by-one (< <=, > >=, int n -> n+1), arithmetic
(+ -, * /, // *, % //), boolean constants and and/or, a return value -> None, and a raise -> pass.
At most --max-mutants are sampled (seeded, so a rerun picks the same ones). --timeout 0 means 3x the baseline + 5 s.

Prints one JSON object: {score, mutants, killed, survived, timeouts, sites, files, baseline, survivors[], counterexamples[]}.
score is killed/mutants (1.0 with no mutants). A red baseline gives baseline "red", score 0.0 and the failing output's
counterexample lines (Hypothesis "Falsifying example") instead of mutants. Exit 0 on a result, 2 on bad usage.
"""
import argparse
import ast
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SKIP_DIRS = {".git", ".overhaul", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".hypothesis",
             ".mypy_cache", ".ruff_cache", "target"}
CMP_FLIP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt,
            ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
CMP_EDGE = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}
ARITH = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult, ast.FloorDiv: ast.Mult,
         ast.Mod: ast.FloorDiv}
DEFAULT_CMD = '"{python}" -m pytest -q -x -p no:cacheprovider {tests}'
TAIL = 30


def sites(tree: ast.AST) -> list[tuple[int, str]]:
    """(node index in ast.walk order, mutation op) for every mutable spot of a parsed module."""
    out = []
    for i, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.Compare):
            op = type(node.ops[0])
            if op in CMP_FLIP:
                out.append((i, "flip_compare"))
            if op in CMP_EDGE:
                out.append((i, "boundary"))
        elif isinstance(node, (ast.BinOp, ast.AugAssign)) and type(node.op) in ARITH:
            out.append((i, "arithmetic"))
        elif isinstance(node, ast.BoolOp):
            out.append((i, "bool_op"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            out.append((i, "bool_const"))
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            out.append((i, "off_by_one"))
        elif isinstance(node, ast.Return) and node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            out.append((i, "return_none"))
        elif isinstance(node, ast.Raise):
            out.append((i, "remove_raise"))
    return out


def apply(source: str, index: int, op: str) -> tuple[str, int, str, str]:
    """Mutate node `index` of `source` with `op`; returns (new source, line, before, after)."""
    tree = ast.parse(source)
    node = next(n for i, n in enumerate(ast.walk(tree)) if i == index)
    before = ast.get_source_segment(source, node) or ast.unparse(node)
    line = getattr(node, "lineno", 0)
    if op == "flip_compare":
        node.ops[0] = CMP_FLIP[type(node.ops[0])]()
    elif op == "boundary":
        node.ops[0] = CMP_EDGE[type(node.ops[0])]()
    elif op == "arithmetic":
        node.op = ARITH[type(node.op)]()
    elif op == "bool_op":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif op == "bool_const":
        node.value = not node.value
    elif op == "off_by_one":
        node.value += 1
    elif op == "return_none":
        node.value = ast.Constant(None)
    elif op == "remove_raise":
        repl = ast.Pass()
        for parent in ast.walk(tree):
            for _name, value in ast.iter_fields(parent):
                if isinstance(value, list) and any(v is node for v in value):
                    value[[v is node for v in value].index(True)] = repl
        node = repl
    after = ast.unparse(node)
    return ast.unparse(tree) + "\n", line, before.splitlines()[0][:120], after.splitlines()[0][:120]


def py_files(repo: Path, paths: list[str]) -> list[str]:
    """Repo-relative .py files under `paths`, test files left out."""
    found = set()
    for p in paths:
        full = repo / p.rstrip("/")
        cands = [full] if full.is_file() else sorted(full.rglob("*.py")) if full.is_dir() else []
        for f in cands:
            rel = f.relative_to(repo).as_posix()
            if f.suffix == ".py" and not (set(rel.split("/")) & SKIP_DIRS) and not f.name.startswith("test_") \
                    and not rel.startswith("tests/"):
                found.add(rel)
    return sorted(found)


def copy_repo(repo: Path) -> Path:
    dst = Path(tempfile.mkdtemp(prefix="mutate_"))
    shutil.copytree(repo, dst / "r", ignore=lambda _d, names: [n for n in names if n in SKIP_DIRS])
    return dst / "r"


def run_tests(cmd: str, cwd: Path, timeout: float | None) -> tuple[str, str]:
    """('pass' | 'fail' | 'timeout', output tail). A fresh process each time: nothing is forked."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True})
    proc = subprocess.Popen(cmd, cwd=cwd, shell=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, **kw)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return "timeout", ""
    text = out.decode("utf-8", "replace")
    return ("pass" if proc.returncode == 0 else "fail"), "\n".join(text.splitlines()[-TAIL:])


def counterexamples(text: str) -> list[str]:
    lines = text.splitlines()
    picks = [i for i, ln in enumerate(lines) if "Falsifying example" in ln or ln.startswith(("FAILED", "E "))]
    return [lines[i].strip()[:300] for i in picks][:10] or [ln[:300] for ln in lines[-10:]]


def mutate(repo: Path, paths: list[str], tests: list[str], cmd: str = DEFAULT_CMD, max_mutants: int = 40,
           timeout: float = 0, seed: int = 0) -> dict:
    files = py_files(repo, paths)
    pool = []
    for rel in files:
        src = (repo / rel).read_text(encoding="utf-8")
        try:
            pool += [(rel, i, op) for i, op in sites(ast.parse(src))]
        except SyntaxError:
            continue
    picked = sorted(random.Random(seed).sample(pool, max_mutants)) if len(pool) > max_mutants else pool
    full = cmd.replace("{python}", sys.executable).replace("{tests}", " ".join(tests))
    result = {"score": 1.0, "mutants": len(picked), "killed": 0, "survived": 0, "timeouts": 0, "sites": len(pool),
              "files": len(files), "baseline": "green", "survivors": [], "counterexamples": []}
    work = copy_repo(repo)
    try:
        start = time.monotonic()
        state, tail = run_tests(full, work, None)
        if state != "pass":
            result.update(score=0.0, baseline="red", counterexamples=counterexamples(tail))
            return result
        limit = timeout or 3 * (time.monotonic() - start) + 5
        for rel, index, op in picked:
            target = work / rel
            original = target.read_text(encoding="utf-8")
            new, line, before, after = apply(original, index, op)
            target.write_text(new, encoding="utf-8")
            try:
                state, _ = run_tests(full, work, limit)
            finally:
                target.write_text(original, encoding="utf-8")
            if state == "pass":
                result["survived"] += 1
                result["survivors"].append({"file": rel, "line": line, "op": op, "before": before, "after": after})
            else:
                result["killed"] += 1
                result["timeouts"] += state == "timeout"
        if picked:
            result["score"] = round(result["killed"] / len(picked), 4)
        return result
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AST mutation tester (overhaul v3 probe)")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--paths", nargs="+", required=True, help="the module's source files or directories")
    ap.add_argument("--tests", nargs="+", required=True, help="the module's unit and property test paths")
    ap.add_argument("--cmd", default=DEFAULT_CMD, help="test command; {tests} and {python} are filled in")
    ap.add_argument("--max-mutants", type=int, default=40)
    ap.add_argument("--timeout", type=float, default=0, help="seconds per mutant; 0 = 3x baseline + 5")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", help="also write the JSON here")
    a = ap.parse_args(argv)
    repo = Path(a.repo)
    if not repo.is_dir():
        print(f"mutate: --repo {a.repo} is not a directory", file=sys.stderr)
        return 2
    text = json.dumps(mutate(repo, a.paths, a.tests, a.cmd, a.max_mutants, a.timeout, a.seed))
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
