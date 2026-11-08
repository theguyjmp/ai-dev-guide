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

Rust: when every --paths entry is a .rs file or a directory holding .rs files (and no .py), the backend is cargo-mutants
(`cargo install cargo-mutants --locked`), run from --repo (the cargo workspace root) on its own temp copy, never
--in-place. It lists the mutants of the module's .rs files (tests/ dirs left out), samples --max-mutants of them with
--seed and runs only those (examine_re in a generated --config; the project's .cargo/mutants.toml is not read).
--tests picks cargo test targets: a file or directory selects every test target whose source is that file or under it
(tests/*.rs, tests/*/main.rs, [[test]] paths), a path under src/ adds --lib, and a word that is not a path is a
test-name filter after `--`; nothing selected runs the package's tests. --cmd, when given, is extra cargo-mutants
options (e.g. "--test-tool=nextest -j 2"). --timeout 0 lets cargo-mutants auto-set it. CARGO_TARGET_DIR defaults to a
dir under the system temp keyed by the --repo path, so a rerun reuses compiled dependencies and each mutant is an
incremental build; the workspace's own packages are cleaned from it first (`cargo clean -p`), because cargo-mutants'
copy keeps source mtimes and cargo would otherwise take the last run's mutated build as fresh. Unviable mutants (they do not compile) go in "unviable", out of mutants and score; a timeout
is a kill; survivors also carry cargo-mutants' "name". A red baseline reads its log for counterexamples (proptest
"minimal failing input", "panicked at", failing tests, compile errors). Exit 3 when cargo or cargo-mutants is missing
or ends without a result.
"""
import argparse
import ast
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11: [[test]] paths are not read
    tomllib = None

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


# ---- Rust backend (cargo-mutants) ----
class ToolError(Exception):
    """cargo or cargo-mutants is missing or ended without a result."""


RE_META = set("\\.+*?()|[]{}^$#&-~")
RUST_CEX = re.compile(r"minimal failing input|panicked at|^test \S+ \.\.\. FAILED|^error")


def walk(full: Path, repo: Path) -> list[Path]:
    """Files under `full`, skipping SKIP_DIRS."""
    return [f for f in full.rglob("*") if f.is_file() and not (set(f.relative_to(repo).parts) & SKIP_DIRS)]


def is_rust(repo: Path, paths: list[str]) -> bool:
    """True when every path is a .rs file or a directory holding .rs files and no .py files."""
    def one(p: str) -> bool:
        full = repo / p.rstrip("/\\")
        if full.is_file():
            return full.suffix == ".rs"
        sufs = {f.suffix for f in walk(full, repo)} if full.is_dir() else set()
        return ".rs" in sufs and ".py" not in sufs
    return bool(paths) and all(one(p) for p in paths)


def rs_files(repo: Path, paths: list[str]) -> list[str]:
    """Repo-relative .rs files under `paths`, tests/ directories left out."""
    found = set()
    for p in paths:
        full = repo / p.rstrip("/\\")
        for f in [full] if full.is_file() else walk(full, repo) if full.is_dir() else []:
            rel = f.relative_to(repo).as_posix()
            if f.suffix == ".rs" and "tests" not in rel.split("/"):
                found.add(rel)
    return sorted(found)


def test_targets(repo: Path) -> dict[str, str]:
    """{repo-relative source path: cargo test target name} for every package under repo."""
    out = {}
    tomls = [repo / "Cargo.toml"] + [t for t in walk(repo, repo) if t.name == "Cargo.toml" and t.parent != repo]
    for toml in tomls:
        if not toml.is_file():
            continue
        try:
            data = tomllib.loads(toml.read_text(encoding="utf-8")) if tomllib else {"package": {}}
        except (ValueError, OSError):
            continue
        if "package" not in data:
            continue
        pkg, tdir = toml.parent, toml.parent / "tests"
        if data["package"].get("autotests", True) and tdir.is_dir():
            out.update({f.relative_to(repo).as_posix(): f.stem for f in tdir.glob("*.rs")})
            out.update({f.relative_to(repo).as_posix(): f.parent.name for f in tdir.glob("*/main.rs")})
        for t in data.get("test", []):
            if t.get("name"):
                src = (pkg / t.get("path", f"tests/{t['name']}.rs")).resolve()
                out[src.relative_to(repo.resolve()).as_posix()] = t["name"]
    return out


def rust_test_args(repo: Path, tests: list[str]) -> tuple[list[str], list[str]]:
    """(cargo test options, test-name filters) for the probe's --tests."""
    targets, opts, filters = test_targets(repo), [], []
    for t in tests:
        rel = t.replace("\\", "/").rstrip("/")
        if not (repo / rel).exists():
            filters.append(t)
            continue
        hits = [n for p, n in sorted(targets.items()) if p == rel or p.startswith(rel + "/")]
        opts += [f"--test={n}" for n in hits] or (["--lib"] if "src" in rel.split("/") else [])
    return list(dict.fromkeys(opts)), filters


def re_exact(name: str) -> str:
    """A regex matching exactly `name` (cargo-mutants examine_re)."""
    return "^" + "".join("\\" + c if c in RE_META else c for c in name) + "$"


def rust_counterexamples(text: str) -> list[str]:
    lines = [ln.strip() for ln in text.splitlines()]
    picks = [ln for ln in lines if RUST_CEX.search(ln)]
    return [ln[:300] for ln in picks][:10] or [ln[:300] for ln in lines[-10:]]


def span_text(repo: Path, rel: str, span: dict) -> str:
    """The first line of the source a mutant replaces (columns 1-based, end exclusive)."""
    try:
        lines = (repo / rel).read_text(encoding="utf-8").splitlines()
        a, b = span["start"], span["end"]
        row = lines[a["line"] - 1]
        text = row[a["column"] - 1:] if a["line"] != b["line"] else row[a["column"] - 1:b["column"] - 1]
    except (OSError, KeyError, IndexError, TypeError):
        return ""
    return text.strip()[:120]


def cargo_mutants(args: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["cargo", "mutants", *args], cwd=cwd, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError as e:
        raise ToolError("cargo is not on PATH") from e


def clean_members(repo: Path, env: dict) -> None:
    """`cargo clean -p` every workspace package in CARGO_TARGET_DIR, keeping the compiled dependencies; with no
    package list, cargo-mutants' own build dir in its temp copy instead."""
    kw = {"cwd": repo, "env": env, "capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
          "stdin": subprocess.DEVNULL, "check": False}
    try:
        meta = subprocess.run(["cargo", "metadata", "--no-deps", "--format-version", "1"], **kw)
        names = sorted({p["name"] for p in json.loads(meta.stdout)["packages"]})
    except FileNotFoundError as e:
        raise ToolError("cargo is not on PATH") from e
    except (ValueError, TypeError, KeyError):
        names = []
    if names:
        subprocess.run(["cargo", "clean", *[a for n in names for a in ("-p", n)]], **kw)
    else:
        env.pop("CARGO_TARGET_DIR", None)


def tail_of(proc: subprocess.CompletedProcess) -> str:
    return " | ".join((proc.stdout + proc.stderr).strip().splitlines()[-5:])


def mutate_rust(repo: Path, paths: list[str], tests: list[str], cmd: str = DEFAULT_CMD, max_mutants: int = 40,
                timeout: float = 0, seed: int = 0) -> dict:
    files = rs_files(repo, paths)
    result = {"score": 1.0, "mutants": 0, "killed": 0, "survived": 0, "timeouts": 0, "unviable": 0, "sites": 0,
              "files": len(files), "baseline": "green", "survivors": [], "counterexamples": []}
    if not files:
        return result
    env = dict(os.environ)
    if not env.get("CARGO_TARGET_DIR"):
        key = hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:12]
        env["CARGO_TARGET_DIR"] = str(Path(tempfile.gettempdir()) / "overhaul-mutants" / key)
    clean_members(repo, env)
    base = ["-d", str(repo)] + [a for f in files for a in ("-f", f)]
    listed = cargo_mutants(base + ["--no-config", "--list", "--json"], repo, env)
    try:
        names = sorted({m["name"] for m in json.loads(listed.stdout)})
    except (ValueError, TypeError, KeyError):
        raise ToolError("cargo mutants --list failed (cargo install cargo-mutants --locked): "
                        + tail_of(listed)) from None
    picked = sorted(random.Random(seed).sample(names, max_mutants)) if len(names) > max_mutants else names
    result.update(sites=len(names), mutants=len(picked))
    if not picked:
        return result
    opts, filters = rust_test_args(repo, tests)
    out = Path(tempfile.mkdtemp(prefix="mutate_rs_"))
    try:
        conf = out / "mutants.toml"
        conf.write_text("examine_re = [\n" + "".join(f"  {json.dumps(re_exact(n))},\n" for n in picked) + "]\n",
                        encoding="utf-8")
        args = base + ["--config", str(conf), "-o", str(out), "--no-shuffle"]
        args += (["--timeout", str(timeout)] if timeout else []) + [f"--cargo-test-arg={o}" for o in opts]
        args += shlex.split(cmd) if cmd.strip() and cmd != DEFAULT_CMD else []
        ran = cargo_mutants(args + (["--", *filters] if filters else []), repo, env)
        mo = out / "mutants.out"
        try:
            outcomes = json.loads((mo / "outcomes.json").read_text(encoding="utf-8"))["outcomes"]
        except (OSError, ValueError, KeyError):
            raise ToolError(f"cargo mutants exit {ran.returncode} without outcomes.json: {tail_of(ran)}") from None
        for o in outcomes:
            sc, summary = o.get("scenario"), o.get("summary")
            if sc == "Baseline":
                if summary != "Success":
                    log = mo / (o.get("log_path") or "log/baseline.log")
                    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ran.stdout
                    result.update(score=0.0, baseline="red", counterexamples=rust_counterexamples(text))
                    return result
                continue
            m = sc.get("Mutant", {}) if isinstance(sc, dict) else {}
            if summary == "Unviable":
                result["unviable"] += 1
            elif summary == "MissedMutant":
                result["survived"] += 1
                span = m.get("span") or {}
                result["survivors"].append({
                    "file": m.get("file"), "line": span.get("start", {}).get("line", 0), "op": m.get("genre", "mutant"),
                    "before": span_text(repo, m.get("file", ""), span), "after": str(m.get("replacement", ""))[:120],
                    "name": m.get("name", "")})
            elif summary in ("CaughtMutant", "Timeout"):
                result["killed"] += 1
                result["timeouts"] += summary == "Timeout"
        result["mutants"] = result["killed"] + result["survived"]
        if result["mutants"]:
            result["score"] = round(result["killed"] / result["mutants"], 4)
        return result
    finally:
        shutil.rmtree(out, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="mutation tester (overhaul v3 probe): Python AST or Rust cargo-mutants")
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
    run_backend = mutate_rust if is_rust(repo, a.paths) else mutate
    try:
        res = run_backend(repo, a.paths, a.tests, a.cmd, a.max_mutants, a.timeout, a.seed)
    except ToolError as e:
        print(f"mutate: {e}", file=sys.stderr)
        return 3
    text = json.dumps(res)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
