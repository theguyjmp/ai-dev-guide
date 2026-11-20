"""The train never picks a row whose sha carries (git ancestry, unlanded) the sha of a row the train rejected or of a
row still queued outside the batch: it stays queued and one pick_skip_parent event names the parent (pilot: a batch
started with rows stacked on rows the pick check and gate_rows had just rejected). Temp state and a fixture git repo;
the pick check, gate_rows' verdicts and the batch run are fakes.

  py -3 tests/test_pick_skip_parent.py
"""
from __future__ import annotations

import os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-skip-parent-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt")})
sys.path.insert(0, str(HERE))
import harness as h  # noqa: E402

FAILS = []
STARTED: list[list[str]] = []


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def git(*a):
    return subprocess.run(["git", *a], cwd=str(h.REPO), capture_output=True, text=True, check=True).stdout.strip()


def branch(name, path, base="main"):
    git("checkout", "-q", "-B", name, base)
    (h.REPO / path).write_text(name + "\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", f"{name}: work")
    sha = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    return sha


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
    h.quarantine.paused = lambda mod: False
    h.MIN_BATCH = 1
    h.fill_wait = lambda queue, picks: False
    h.try_batch = lambda batch, rows, **k: STARTED.append([q["row"] for q in batch]) or []


def skips():
    return {e["row"]: e["parent"] for e in h.read_events() if e["event"] == "pick_skip_parent"}


def run(queue, screen_bad=(), gate_bad=()):
    def screen(picks, budget=0):
        bad = [q for q in picks if q["row"] in screen_bad]
        for q in bad:
            h.reject(q, "pick check red")
        return {q["sha"] for q in bad}
    h.pick_screen = screen
    h.gate_rows = lambda entries, rows: [(q, "no tests") for q in entries if q["row"] in gate_bad]
    h.save("queue.json", queue)
    STARTED.clear()
    h.train_once()
    return STARTED[0] if STARTED else []


def test_children_of_rejected_and_held_parents_wait():
    setup()
    p = branch("P", "p.txt")
    c = branch("C", "c.txt", base="P")
    d = branch("D", "d.txt")
    g = branch("G", "g.txt")
    k = branch("K", "k.txt", base="G")
    q = [{"row": r, "sha": s, "ts": i} for i, (r, s) in enumerate([("P", p), ("C", c), ("D", d), ("G", g), ("K", k)])]
    got = run(q, screen_bad={"P"}, gate_bad={"G"})
    check("batch holds only the rows on no rejected parent", got == ["D"], got)
    check("pick_skip_parent names each parent", skips() == {"C": "P", "K": "G"}, skips())
    check("children stay queued", {x["row"] for x in h.load("queue.json", [])} >= {"C", "K"}, h.load("queue.json", []))
    run([x for x in h.load("queue.json", []) if x["row"] in ("C", "K")])
    check("next pick: still skipped, logged once", STARTED == [] and len(skips()) == 2 and
          sum(e["event"] == "pick_skip_parent" for e in h.read_events()) == 2, STARTED)


def test_exemptions():
    p1 = branch("E", "e.txt")
    p2 = branch("E2", "e2.txt", base="E")  # row E's fix on top of its own rejected sha
    child = branch("F", "f.txt", base="E2")
    e, f = {"row": "E", "sha": p2, "ts": 1}, {"row": "F", "sha": child, "ts": 2}
    got = h.stacked_on([e, f], {p1: ("E", True)})
    check("own rejected sha exempt; its child rides with the fix", got == [], got)
    check("a parent in the batch is no blocker", h.stacked_on([e, f], {p2: ("E", False)}) == [])
    e3 = branch("E3", "e3.txt", base="E")  # E's fix on top of its rejected sha, and a child stacked on it
    g = branch("G2", "g2.txt", base="E3")
    fix, kid = {"row": "E", "sha": e3, "ts": 3}, {"row": "G", "sha": g, "ts": 4}
    got = h.stacked_on([fix, kid], {p1: ("E", True)})
    check("a child rides with its parent's fix in the batch", got == [], got)
    got = h.stacked_on([kid], {p1: ("E", True)})
    check("without the parent's fix it waits", [(x["row"], s) for x, s in got] == [("G", p1)], got)
    git("update-ref", "refs/heads/main", child)
    check("a parent already on main never blocks", h.stacked_on([f], {p1: ("E", True), p2: ("E", False)}) == [])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AttributeError as e:  # the old harness has no stacked_on: red, not a crash
                check(name, False, repr(e))
    print(f"{len(FAILS)} failed" if FAILS else "ALL PASS")
    sys.exit(1 if FAILS else 0)
