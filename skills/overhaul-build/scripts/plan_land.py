#!/usr/bin/env python3
"""plan_land.py: the check a plan commit passes before main moves to it, run by supervise.py and by planner agents.

    py -3 plan_land.py --repo R --branch B [--main main] [--wt W] [--tests-only] [--timeout S] [--ff]

On B merged with main (in W when given, where B is checked out on top of main; else in a detached worktree under
R/.overhaul/plan-land/ that merges main and is removed afterwards):
1. plan_validate.py (static PLAN OK), then the [commands] dup and literals gates (skipped with --tests-only:
   supervise.py runs those itself);
2. the proven acceptance tests the commit can break: each proven row whose acceptance file it touches, every proven
   row of a module it touches or of a module depending on one (land.py's footprint_rows), and every proven row when it
   touches a file outside every module's Paths that is neither spec/*.md nor an acceptance file. Batched like a land
   (land.regression), CARGO_TARGET_DIR R/.overhaul/target/plan.
Last line `PLAN LAND OK: ...` (exit 0) or `PLAN LAND RED: ...` (exit 1). With --ff it then moves main to B through
fast_forward() (also supervise.py's one way to move main): the main guard judges the ref update before any file of
the checkout changes, so a refusal leaves main and the checkout exactly as they were (never `git merge --ff-only`,
which writes the files first: the pilot project 2026-11-10 07:35-08:10, a refused fast-forward left the live repo dirty and
blocked every land and plan round, twice).

Why (pilot2026-11-10): the phase-10 plan commit's stubs (invalid JSON schemas) failed proven tests, and main was red
for every land 03:30-05:20; plan commits ran only static checks before they landed.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
CALLER_HARNESS = bool(os.environ.get("OVERHAUL_HARNESS"))  # before land.py sets it: the guard judges the caller
import land  # noqa: E402
import memcap  # noqa: E402

STATIC_GATES = ("dup", "literals")
OK, RED = "PLAN LAND OK", "PLAN LAND RED"


def load_conf(tree: Path) -> dict:
    try:
        return tomllib.loads((tree / "overhaul.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def touched(tree: Path, main: str) -> list[str]:
    """Files the tree's HEAD changes against its merge base with main."""
    base = land.git(str(tree), "merge-base", main, "HEAD", check=False).stdout.strip() or main
    return [f for f in land.git(str(tree), "diff", "--name-only", base, "HEAD").stdout.splitlines() if f]


def plan_rows(arch: str, rows: dict, files: list[str], fpath: str) -> tuple[set[str], set[str], set[str]]:
    """(proven row ids to rerun, modules touched, rows whose own acceptance file is touched) for a plan commit."""
    proven = {r for r, row in rows.items() if row["status"] == "proven"}
    owned = {node.partition("::")[0]: set() for row in rows.values() for node in row["acceptance"]}
    for r, row in rows.items():
        for node in row["acceptance"]:
            owned[node.partition("::")[0]].add(r)
    paths = land.module_paths(arch)
    direct, mods, every = set(), set(), False
    for f in files:
        if f in owned:
            direct |= owned[f] & proven
            continue
        hit = {m for m, pats in paths.items() if land.under(f, pats)}
        if hit:
            mods |= hit
        elif f != fpath and not (f.startswith("spec/") and f.endswith(".md")) and not f.startswith(".overhaul/"):
            every = True
    ids = set(proven) if every else direct | land.footprint_rows(arch, rows, mods)
    return ids, mods, direct


def proven_check(tree: Path, main: str, conf: dict, timeout: float, out: Path) -> tuple[bool, str]:
    """Rerun the proven acceptance tests the tree's change can break (plan_rows). (green, one line)."""
    paths = conf.get("paths") or {}
    fpath = str(paths.get("functions") or "spec/FUNCTIONS.md")
    apath = str(paths.get("architecture") or "spec/ARCHITECTURE.md")
    rows = land.parse_functions(_read(tree / fpath))
    ids, mods, direct = plan_rows(_read(tree / apath), rows, touched(tree, main), fpath)
    commands = land.load_commands(conf)
    if not ids:
        return True, "no proven row in the commit's footprint"
    if not str(commands.get("acceptance") or "").strip():
        return True, f"{len(ids)} proven rows in the footprint, no [commands] acceptance to run"
    out.parent.mkdir(parents=True, exist_ok=True)
    hit = land.regression(rows, sorted(ids), commands, str(tree), str(out), timeout, tiers=land.tier_globs(conf),
                          near=mods, extra=direct)
    if hit:
        return False, f"{hit[2]} (output {out.as_posix()})"
    return True, f"{len(ids)} proven rows green: {' '.join(sorted(ids)[:12])}{' ...' if len(ids) > 12 else ''}"


def static_check(tree: Path, conf: dict, out_dir: Path) -> str:
    """'' when plan_validate says PLAN OK and the dup and literals gates pass in tree, else the first problem."""
    out_dir.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([sys.executable, str(HERE / "plan_validate.py"), "--repo", str(tree)], check=False,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    line = _last(p.stdout + p.stderr)
    if p.returncode or not line.startswith("PLAN OK"):
        return f"not PLAN OK: {line}"
    for name in STATIC_GATES:
        cmd = (conf.get("commands") or {}).get(name)
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        p = subprocess.run(cmd, shell=True, check=False, cwd=tree, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL)
        if p.returncode:
            first = next((ln for ln in (p.stdout + p.stderr).splitlines() if ln.strip()), "")
            return f"fails {name} (main would go red): {first[:300]}"
    return ""


def merged_tree(repo: Path, branch: str, main: str) -> tuple[Path, str]:
    """A detached worktree of branch with main merged in: (path, '' or why it could not be made)."""
    wt = repo / ".overhaul" / "plan-land" / re.sub(r"[^\w.-]", "_", branch)
    drop_tree(repo, wt)
    wt.parent.mkdir(parents=True, exist_ok=True)
    p = land.git(str(repo), "worktree", "add", "-q", "--detach", str(wt), branch, check=False)
    if p.returncode:
        return wt, f"cannot check out {branch}: {_last(p.stderr)}"
    if land.git(str(wt), "merge-base", "--is-ancestor", main, "HEAD", check=False).returncode:
        p = land.git(str(wt), "-c", "user.name=plan_land.py", "-c", "user.email=plan_land@localhost", "merge", "-q",
                     "--no-edit", main, check=False)
        if p.returncode:
            return wt, f"{branch} does not merge with {main}: {_last(p.stdout + p.stderr)}"
    return wt, ""


def drop_tree(repo: Path, wt: Path) -> None:
    land.git(str(repo), "worktree", "remove", "--force", str(wt), check=False)
    shutil.rmtree(wt, ignore_errors=True)
    land.git(str(repo), "worktree", "prune", check=False)


def check(repo: Path, branch: str, main: str, wt: Path | None, tests_only: bool, timeout: float) -> tuple[int, str]:
    out = repo / ".overhaul" / "plan-land" / "tests.out"
    tree, why = (wt, "") if wt else merged_tree(repo, branch, main)
    try:
        if why:
            return 1, f"{RED}: {why}"
        conf = load_conf(tree)
        if not tests_only and (why := static_check(tree, conf, out.parent)):
            return 1, f"{RED}: {branch} {why}"
        green, line = proven_check(tree, main, conf, timeout, out)
        return (0, f"{OK}: {branch}: {line}") if green else (1, f"{RED}: {branch}: {line}")
    finally:
        if not wt:
            drop_tree(repo, tree)


# ---- moving main ----------------------------------------------------------------------------------------------------

def _git_env(harness: bool) -> dict:
    """The env for a ref update: the main guard sees the caller's own standing, never the OVERHAUL_HARNESS that
    importing land.py set for this process."""
    env = dict(os.environ)
    if not harness:
        env.pop("OVERHAUL_HARNESS", None)
    return env


def _g(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def main_checkout(repo: Path, main: str) -> Path | None:
    """The worktree where main is checked out (the repo itself or a linked worktree), else None."""
    path = None
    for ln in _g(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if ln.startswith("worktree "):
            path = Path(ln[len("worktree "):])
        elif ln.strip() == f"branch refs/heads/{main}" and path is not None:
            return path
    return None


def fast_forward(repo: Path, branch: str, main: str = "main", harness: bool | None = None) -> tuple[bool, str]:
    """Move main to branch, and on any refusal leave the checkout exactly as it was. `git merge --ff-only` writes the
    new files into the checkout and index BEFORE the reference-transaction hook (the main guard) may refuse the ref
    update, which left the pilot project's live repo dirty and blocked every land and plan round (2026-11-10 07:35-08:10,
    twice). Here: (1) a dry run of the checkout's two-tree update (a local change it would overwrite refuses),
    (2) `git update-ref` main old -> new, the one step the guard judges, with no file touched, (3) only then the
    checkout's index and files move old -> new (`read-tree -m -u`, local changes kept). A failed step 3 puts the ref
    back. `git status --porcelain` is the same after a refusal as before, checked. harness: the caller runs under
    the harness (supervise.py); default: OVERHAUL_HARNESS was set before plan_land.py imported land.py."""
    harness = CALLER_HARNESS if harness is None else harness
    env = _git_env(harness)
    new = _g(repo, "rev-parse", "--verify", "-q", f"{branch}^{{commit}}").stdout.strip()
    old = _g(repo, "rev-parse", "--verify", "-q", f"refs/heads/{main}").stdout.strip()
    if not new or not old:
        return False, f"no {branch if not new else main} to fast-forward"
    if new == old:
        return True, f"{main} already at {branch}"
    if _g(repo, "merge-base", "--is-ancestor", old, new).returncode:
        return False, f"{branch} is not ahead of {main} (rebase it onto {main} first)"
    wt = main_checkout(repo, main)
    before = _g(wt, "status", "--porcelain").stdout if wt else ""
    if wt:
        p = _g(wt, "read-tree", "-m", "-u", "-n", old, new)
        if p.returncode:
            return False, f"the {main} checkout has local changes the fast-forward would overwrite: {_last(p.stderr)}"
    p = _g(repo, "update-ref", "-m", f"plan_land: fast-forward {main} to {branch}", f"refs/heads/{main}", new, old,
           env=env)
    if p.returncode:
        why = " / ".join(ln.strip() for ln in p.stderr.splitlines() if ln.strip())[:600] or f"exit {p.returncode}"
        return _unchanged(wt, before, False, f"refused, {main} and the checkout unchanged: {why}")
    if wt:
        p = _g(wt, "read-tree", "-m", "-u", old, new)
        if p.returncode:  # raced by a local edit after the dry run: main goes back, the checkout never moved
            _g(repo, "update-ref", "-m", "plan_land: undo", f"refs/heads/{main}", old, new, env=_git_env(True))
            return _unchanged(wt, before, False, f"checkout did not update, {main} put back: {_last(p.stderr)}")
    return True, f"{main} -> {branch} {new[:12]}"


def _unchanged(wt: Path | None, before: str, ok: bool, line: str) -> tuple[bool, str]:
    after = _g(wt, "status", "--porcelain").stdout if wt else ""
    if after != before:
        return False, f"{line}; CHECKOUT CHANGED (git status --porcelain differs): fix it by hand"
    return ok, line


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _last(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--main", default="main")
    ap.add_argument("--wt", default="", help="a worktree where branch is checked out on top of main (no new one)")
    ap.add_argument("--tests-only", action="store_true", help="only the proven tests (the caller ran the rest)")
    ap.add_argument("--timeout", type=float, default=1800.0, help="seconds per test command")
    ap.add_argument("--ff", action="store_true", help="after PLAN LAND OK, fast-forward main to the branch (safely)")
    a = ap.parse_args(argv)
    repo = Path(os.path.abspath(a.repo))
    os.environ["CARGO_TARGET_DIR"] = str(repo / ".overhaul" / "target" / "plan")
    if not os.environ.get(memcap.ENV):  # run by a planner by hand: the same [limits] proc_mem_gb cap as a land
        memcap.cap_self(land.proc_mem_gb(str(repo)))
    code, line = check(repo, a.branch, a.main, Path(os.path.abspath(a.wt)) if a.wt else None, a.tests_only,
                       a.timeout)
    if code == 0 and a.ff:
        ok, why = fast_forward(repo, a.branch, a.main)
        code, line = (0, f"{line}; {why}") if ok else (1, f"{RED}: {a.branch}: fast-forward {why}")
    print(line, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
