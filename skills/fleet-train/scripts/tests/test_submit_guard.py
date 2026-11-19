"""A submit never silently replaces another lane's queued packet. Pilot: slot 1 queued a fix row; slot 2 clean-submitted
an unrelated sha for the same row (a merge of main into its own branch), which replaced the queue entry and moved h/<row>,
dropping a gated packet. The resubmit is refused, naming the queued sha and slot, unless it contains the queued sha (a
restack's requeue-in-place merges it), the same slot queued it, or --replace. Temp repo and state only.

  py -3 tests/test_submit_guard.py
"""
from __future__ import annotations

import argparse, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def g(cwd, *a) -> str:
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text, msg) -> str:
    (repo / name).write_text(text)
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", msg)
    return g(repo, "rev-parse", "HEAD")


def run(fn, ns):
    try:
        fn(ns)
        return None
    except SystemExit as e:
        return str(e.code)


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    repo, wt = root / "repo", root / "slot"
    repo.mkdir()
    os.environ.update(HARNESS_HOME=str(root / "home"), HARNESS_WT=str(root / "wt"), HARNESS_REPO=str(repo))
    (root / "home" / "state").mkdir(parents=True)
    g(repo, "init", "-q", "-b", "main")
    g(repo, "config", "user.email", "t@t")
    g(repo, "config", "user.name", "t")
    base = commit(repo, "a.txt", "x", "base")
    g(repo, "checkout", "-qb", "clean/fix1-s1")
    gated = commit(repo, "b.txt", "y", "fix: slot 1's gated packet")
    g(repo, "checkout", "-q", "main")
    commit(repo, "m.txt", "m", "main moves on")
    g(repo, "checkout", "-qb", "clean/fix1-s2", base)
    commit(repo, "c.txt", "z", "another lane's work on the same row")
    g(repo, "merge", "-q", "--no-edit", "main")
    unrelated = g(repo, "rev-parse", "HEAD")
    g(repo, "checkout", "-qb", "restack", gated)
    g(repo, "merge", "-q", "--no-edit", "main")
    restacked = g(repo, "rev-parse", "HEAD")  # the restack: the queued sha merged with the new main
    g(repo, "checkout", "-q", "main")
    g(repo, "worktree", "add", "-q", "--detach", str(wt), base)

    sys.path.insert(0, str(HERE))
    import harness as h  # noqa: E402
    h.slot_wt = lambda slot: wt
    h.submit_dup = lambda cwd: ""
    row = "CLEAN-fix1"

    def setup(r=row):
        h.save("queue.json", [{"row": r, "sha": gated, "ts": 1, "slot": 1}])
        g(repo, "branch", "-f", f"h/{r}", gated)

    def clean(slot, sha, replace=False):
        g(wt, "checkout", "-q", "--detach", sha)
        return run(h.cmd_clean_submit, argparse.Namespace(slot=slot, name="fix1", front=False, absorbs="", replace=replace))

    def queued(r=row):
        return [(q["sha"], q["slot"]) for q in h.load("queue.json", []) if q["row"] == r]

    setup()
    msg = clean(2, unrelated)
    check("another slot's unrelated sha is refused", bool(msg) and gated[:12] in msg and "slot 1" in msg and "--replace" in msg, msg)
    check("refused submit keeps the queued packet and h/<row>", queued() == [(gated, 1)] and g(repo, "rev-parse", f"h/{row}") == gated)
    setup()
    msg = clean(2, restacked)
    check("requeue-in-place: a sha that contains the queued one replaces it", msg is None and queued() == [(restacked, 2)], msg)
    setup()
    msg = clean(1, unrelated)
    check("the same slot may replace its own packet", msg is None and queued() == [(unrelated, 1)], msg)
    setup()
    msg = clean(2, unrelated, replace=True)
    check("--replace drops the queued packet on purpose", msg is None and queued() == [(unrelated, 2)], msg)
    setup(r="R1")  # the builder submit path shares the rule
    h.save("claims.json", {"2": {"row": "R1", "ts": 1}})
    g(wt, "checkout", "-q", "--detach", unrelated)
    msg = run(h.cmd_submit, argparse.Namespace(slot=2, row="R1", replace=False))
    check("builder submit: another slot's queued sha is refused too", bool(msg) and gated[:12] in msg and queued("R1") == [(gated, 1)], msg)
print(f"{len(FAILS)} failed" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
