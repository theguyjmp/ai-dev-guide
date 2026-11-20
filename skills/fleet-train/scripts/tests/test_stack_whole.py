"""The picker takes a stack whole or not at all: a parent held only for a row its own child absorbs is picked with that
child, and when the child cannot go (absent, conflicting, rejected by gate_rows) the parent is held, never picked
alone. Pilot: a parent carried a spec row's commits that its stacked child absorbs; the parent was held (nothing ahead
of it landed the row) and the child was skipped as stacked on the unpicked parent, so neither could ever be picked.
Temp state and a fixture git repo.

  py -3 tests/test_stack_whole.py
"""
from __future__ import annotations

import os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-stack-whole-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt")})
sys.path.insert(0, str(HERE))
import harness as h  # noqa: E402

FAILS: list[str] = []
STARTED: list[list[str]] = []


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def git(*a):
    return subprocess.run(["git", *a], cwd=str(h.REPO), capture_output=True, text=True, check=True).stdout.strip()


def branch(name, parent, *commits):
    git("checkout", "-q", "-B", name, parent)
    for msg, f, text in commits:
        (h.REPO / f).write_text(text)
        git("add", "-A")
        git("commit", "-q", "-m", msg)
    return git("rev-parse", "HEAD")


rows_of = lambda picks: [x["row"] for x in picks]  # noqa: E731
h.REPO.mkdir(parents=True, exist_ok=True)
h.ST.mkdir(parents=True, exist_ok=True)
git("init", "-q", "-b", "main")
git("config", "user.email", "t@t")
git("config", "user.name", "t")
(h.REPO / "a.txt").write_text("0")
git("add", "-A")
git("commit", "-q", "-m", "base")
a = branch("a", "main", ("R7f (a): one owner", "a7.txt", "a"))  # the parent's own commit names the spec row
b = branch("b", "a", ("R7f (b): the child folds onto it", "b7.txt", "b"))  # the child absorbs it
a2 = branch("a2", "main", ("R8f (a2): one owner", "a8.txt", "a"), ("CLEAN-a2: tidy", "a8b.txt", "a"))
b2 = branch("b2", "a2", ("CLEAN-b2: on top", "b8.txt", "b"))  # mutual: also carries the parent's row by subject
c = branch("c", "main", ("CLEAN-c: clashes with the child", "b7.txt", "c"))
d = branch("d", "main", ("CLEAN-d: independent", "d.txt", "d"))
git("checkout", "-q", "main")
spec = {"R7f": {"status": "planned"}, "R8f": {"status": "planned"}}
h.main_spec = lambda: spec
q = lambda row, sha, ts, *ab: {"row": row, "sha": sha, "ts": ts, "slot": 1, **({"absorbs": list(ab)} if ab else {})}  # noqa: E731
QA, QB, QA2, QB2 = q("CLEAN-a", a, 1), q("CLEAN-b", b, 2, "R7f"), q("CLEAN-a2", a2, 3), q("CLEAN-b2", b2, 4, "R8f")
QC, QD = q("CLEAN-c", c, 1.5), q("CLEAN-d", d, 5)

h.save("queue.json", [QA, QB, QD])
picks = h.disjoint([QA, QB, QD])
check("parent held only for a row its child absorbs is picked with the child", rows_of(picks) == ["CLEAN-a", "CLEAN-b", "CLEAN-d"],
      rows_of(picks))
check("nothing recorded held", h.load("pick-held.json", None) == {}, h.load("pick-held.json", None))
check("the child is not skipped as stacked on an unpicked parent", h.skip_stacked(picks) == [])
h.save("queue.json", [QA2, QB2, QD])
check("mutual: both picked", rows_of(h.disjoint([QA2, QB2, QD])) == ["CLEAN-a2", "CLEAN-b2", "CLEAN-d"])
h.save("queue.json", [QA, QD])
check("child absent: the parent is held", rows_of(h.disjoint([QA, QD])) == ["CLEAN-d"]
      and h.load("pick-held.json", {}) == {"CLEAN-a": ["R7f"]}, h.load("pick-held.json", {}))
h.save("queue.json", [QA, QC, QB, QD])
check("child conflicts with an earlier pick: the parent is held, not picked alone",
      rows_of(h.disjoint([QA, QC, QB, QD])) == ["CLEAN-c", "CLEAN-d"] and h.load("pick-held.json", {}) == {"CLEAN-a": ["R7f"]},
      h.load("pick-held.json", {}))

# through train_once: the stack goes whole; a gate_rows reject of the child keeps the parent out too
h.quarantine.paused = lambda mod: False
h.MIN_BATCH = 1
h.fill_wait = lambda queue, picks: False
h.pick_screen = lambda picks, budget=0: set()
h.try_batch = lambda batch, rows, **k: STARTED.append(rows_of(batch)) or []
gate_bad: set = set()
h.gate_rows = lambda entries, rows: [(x, "no tests") for x in entries if x["row"] in gate_bad]
h.save("claims.json", {})
h.save("queue.json", [dict(QA), dict(QB), dict(QD)])
h.train_once()
check("train_once: the stack starts whole", STARTED == [["CLEAN-a", "CLEAN-b", "CLEAN-d"]], STARTED)
STARTED.clear()
gate_bad.add("CLEAN-b")
h.save("queue.json", [dict(QA), dict(QB), dict(QD)])
h.train_once()
check("train_once: a gate_rows reject of the child keeps its parent out of the batch", STARTED == [["CLEAN-d"]], STARTED)
check("the parent stays queued and held", "CLEAN-a" in rows_of(h.load("queue.json", []))
      and h.load("pick-held.json", {}).get("CLEAN-a") == ["R7f"], h.load("pick-held.json", {}))

print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
