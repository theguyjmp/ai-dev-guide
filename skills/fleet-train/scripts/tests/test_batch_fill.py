"""Batch fill: under MIN_BATCH clean picks with queued rows out for cloud rebase, the train idles ("filling: ...") for
at most FILL_WAIT, then gates what is clean; a CLEAN-* pick never waits. fill_hold's rule, fill_since's timer,
rebase_out's count, and fill_wait end to end on temp state with a fake clock.

  py -3 tests/test_batch_fill.py
"""
from __future__ import annotations

import os, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-fill-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt")})
sys.path.insert(0, str(HERE))
import harness as h  # noqa: E402

FAILS = []
T0 = 1_000_000.0
WAIT = h.FILL_WAIT
QUEUE = [{"row": f"R{i}", "sha": f"{i:040d}"} for i in range(6)]


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def idle(ts, why=h.FILLING + "1 clean, 5 out for rebase"):
    return {"event": "train_idle", "ts": ts, "reason": why}


def test_fill_hold_rule():
    hold = h.fill_hold
    check("1 clean, 5 out, no hold yet: hold", hold(1, 5, False, None, T0))
    check("still holding just before the timeout", hold(1, 5, False, T0, T0 + WAIT - 1))
    check(f"release on fill ({h.MIN_BATCH} clean)", not hold(h.MIN_BATCH, 5, False, T0, T0 + 60))
    check("release on timeout", not hold(1, 5, False, T0, T0 + WAIT))
    check("a CLEAN-* fix row is never held", not hold(1, 5, True, None, T0))
    check("nothing out for rebase: start at once", not hold(1, 0, False, None, T0))


def test_fill_since_timer():
    since = h.fill_since
    check("first fill idle of the run", since([idle(T0), idle(T0 + 30)], T0 + 60) == T0)
    check("a train_start ends the hold", since([idle(T0), {"event": "train_start", "ts": T0 + 40}], T0 + 60) is None)
    check("another idle ends the hold",
          since([idle(T0), idle(T0 + 30, "no row merges cleanly with main"), idle(T0 + 60)], T0 + 90) == T0 + 60)
    check("a gap over the wait starts a new hold", since([idle(T0), idle(T0 + WAIT + 1)], T0 + WAIT + 30) == T0 + WAIT + 1)
    check("an old hold is not the one running now", since([idle(T0)], T0 + WAIT + 1) is None)


def test_rebase_out_count():
    sent = {"R0": QUEUE[0]["sha"], "R1": QUEUE[1]["sha"], "R2": "f" * 40}  # R2: an older sha was exported
    n = h.rebase_out(QUEUE, QUEUE[:1], sent)
    check("exported current sha counts; picks and stale exports do not", n == 1, n)


def test_fill_wait_end_to_end():
    h.ST.mkdir(parents=True, exist_ok=True)
    h.save(h.CLOUD_STATE, {"rows": {"R1": {"rebase_sent": QUEUE[1]["sha"]}}})
    (h.ST / "events.jsonl").unlink(missing_ok=True)
    clock = [T0]
    h.now = lambda: clock[0]  # event() stamps ts with now(): the fake clock times the hold
    held = h.fill_wait(QUEUE, QUEUE[:1])
    last = h.read_events()[-1]
    check("holds and logs the filling idle", held and last["event"] == "train_idle"
          and last["reason"] == h.FILLING + "1 clean, 1 out for rebase", held, last)
    check("a CLEAN-* pick starts at once", not h.fill_wait(QUEUE, [{"row": "CLEAN-x", "sha": "c" * 40}]))
    clock[0] = T0 + WAIT - 30
    check("still holding inside the wait", h.fill_wait(QUEUE, QUEUE[:1]))
    clock[0] = T0 + WAIT
    check("starts after FILL_WAIT", not h.fill_wait(QUEUE, QUEUE[:1]))
    (h.ST / h.CLOUD_STATE).unlink()
    check("no cloud fleet: never holds", not h.fill_wait(QUEUE, QUEUE[:1]))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"{len(FAILS)} failed" if FAILS else "ALL PASS")
    sys.exit(1 if FAILS else 0)
