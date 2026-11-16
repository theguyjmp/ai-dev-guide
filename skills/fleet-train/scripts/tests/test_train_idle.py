"""An empty disjoint batch must not spin train_start rows=[] (2026-11-16: ~150 empty trains): the unmergeable row gets
conflict handling (rejected AND dequeued) and the train goes idle (event train_idle). reject() dequeues on every path.
Temp state and a fixture git repo; the pick check, quarantine and spec reader are fakes.

  py -3 tests/test_train_idle.py
"""
from __future__ import annotations

import os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-idle-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt")})
sys.path.insert(0, str(HERE))
import harness as h  # noqa: E402

FAILS = []


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def git(*a):
    return subprocess.run(["git", *a], cwd=str(h.REPO), capture_output=True, text=True, check=True).stdout.strip()


def branch(name, text, base="main"):
    git("checkout", "-q", "-B", name, base)
    (h.REPO / "a.txt").write_text(text, encoding="utf-8")
    git("commit", "-qam", name)
    sha = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    return sha


def events(kind):
    return [e for e in h.read_events() if e["event"] == kind]


def setup():
    h.ST.mkdir(parents=True, exist_ok=True)
    h.REPO.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (h.REPO / "a.txt").write_text("base\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    h.main_spec = lambda: {}
    h.pick_screen = lambda picks, budget=0: set()
    h.quarantine.paused = lambda mod: False
    h.MIN_BATCH = 1


def test_empty_batch_goes_idle_and_dequeues():
    setup()
    sha = branch("R1", "row\n")
    (h.REPO / "a.txt").write_text("main moved\n", encoding="utf-8")  # main now conflicts with R1
    git("commit", "-qam", "main moves")
    h.save("queue.json", [{"row": "R1", "sha": sha, "ts": 1}])
    for _ in range(3):
        check("train_once returns False (idle)", h.train_once() is False)
    check("no empty train_start", not events("train_start"), events("train_start"))
    check("train_idle emitted", bool(events("train_idle")))
    check("conflicting row dequeued", h.load("queue.json", []) == [], h.load("queue.json", []))
    check("conflicting row rejected once", len(h.load("rejects.json", {}).get("R1", [])) == 1, h.load("rejects.json", {}))


def test_reject_dequeues_only_that_row():
    h.save("queue.json", [{"row": "A", "sha": "s1", "ts": 1}, {"row": "B", "sha": "s2", "ts": 2}])
    h.reject({"row": "A", "sha": "s1"}, "pick check red")
    check("rejected row left the queue", [q["row"] for q in h.load("queue.json", [])] == ["B"])
    check("reject recorded", h.load("rejects.json", {}).get("A", [{}])[-1].get("reason") == "pick check red")
    with h.lock("state"):  # a caller already holding the state lock must not deadlock
        h.reject({"row": "B", "sha": "s2"}, "ruling", locked=True)
    check("locked reject dequeues too", h.load("queue.json", []) == [])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"{len(FAILS)} failed" if FAILS else "ALL PASS")
    sys.exit(1 if FAILS else 0)
