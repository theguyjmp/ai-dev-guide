"""cmd_submit runs the pick-time duplicate review while the builder's claim is held: a duplicate is refused with the
pick-time text and nothing is queued; a clean row queues. Temp repo and state only; the reviewer is stubbed.

  py -3 tests/test_submit_review.py
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


def g(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    repo = root / "repo"
    repo.mkdir()
    os.environ.update(HARNESS_HOME=str(root / "home"), HARNESS_WT=str(root / "wt"), HARNESS_REPO=str(repo))
    (root / "home" / "state").mkdir(parents=True)
    g(repo, "init", "-q", "-b", "main")
    g(repo, "config", "user.email", "t@t")
    g(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x")
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", "base")
    g(repo, "checkout", "-qb", "work")
    (repo / "b.txt").write_text("y")
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", "row")
    sys.path.insert(0, str(HERE))
    import harness as h  # noqa: E402
    h.slot_wt = lambda slot: repo
    h.save("claims.json", {"1": {"row": "R1", "ts": 1}})
    dup = [{"file": "crates/x.rs", "fn": "modify", "existing": "crates/y.rs::modify", "why": "same job"}]
    h.batch_review = lambda *a, **k: dup
    ns = argparse.Namespace(slot=1, row="R1", replace=False)
    try:
        h.cmd_submit(ns)
        msg = None
    except SystemExit as e:
        msg = str(e.code)
    check("duplicate refused with the pick-time text", bool(msg) and msg.startswith("duplicate capability (STANDARDS S1/S6)")
          and "crates/x.rs::modify duplicates crates/y.rs::modify" in msg, msg)
    check("refused submit queues nothing and keeps the claim", h.load("queue.json", []) == [] and "1" in h.load("claims.json", {}))
    h.batch_review = lambda *a, **k: []
    h.cmd_submit(ns)
    check("clean row queued, claim dropped", [q["row"] for q in h.load("queue.json", [])] == ["R1"] and h.load("claims.json", {}) == {})
    h.save("claims.json", {"1": {"row": "R1", "ts": 1}})
    def down(*a, **k):
        raise h.ReviewUnavailable("x")
    h.batch_review = down
    h.cmd_submit(ns)
    check("reviewer down: queued (pick-time review still guards)", [q["row"] for q in h.load("queue.json", [])] == ["R1"])
print(f"{len(FAILS)} failed" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
