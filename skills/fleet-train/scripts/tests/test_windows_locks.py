"""Windows lock and reject-reason fixes from a node pilot: a lock is released only by the process that holds it (a
train_error cleanup had deleted another process's live state lock), a PermissionError on a lock file being deleted is
contention, not a crash, a lock timeout names the holder, CargoSlots retries os.replace while a reader holds the file,
and a unit-red reject reason ends with the failing tests' names (a 6000-char tail had cut them off).
Temp state only.

  py -3 tests/test_windows_locks.py
"""
from __future__ import annotations

import os, sys, tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-locks-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt"),
                   "HARNESS_STACK": "node"})
sys.path.insert(0, str(HERE))
import harness as h  # noqa: E402
import cargo_slots  # noqa: E402

FAILS = []


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *info)
    if not ok:
        FAILS.append(name)


def test_release_only_own():
    h.ST.mkdir(parents=True, exist_ok=True)
    p = h.ST / "state.lock"
    p.write_text("999999", encoding="utf-8")  # another process's live lock
    h.release_lock("state")
    check("another process's lock survives release_lock", p.exists())
    p.write_text(str(os.getpid()), encoding="utf-8")
    h.release_lock("state")
    check("own lock is released", not p.exists())
    with h.lock("state"):
        check("lock() writes this pid", h.lock_holder("state") == str(os.getpid()))
    check("lock() releases on exit", not p.exists())


def test_timeout_names_holder():
    p = h.ST / "train.lock"
    p.write_text("4242", encoding="utf-8")
    try:
        with h.lock("train", wait=0):
            check("held lock times out", False)
    except TimeoutError as e:
        check("timeout names the holder pid", "4242" in str(e), str(e))
    p.unlink()


def test_permission_error_is_contention():
    real, calls = os.open, []

    def flaky(path, flags, *a):
        if str(path).endswith("state.lock") and not calls:
            calls.append(1)
            raise PermissionError(13, "pending delete")
        return real(path, flags, *a)

    with mock.patch.object(h.os, "open", flaky):
        with h.lock("state", wait=5):
            check("PermissionError on open is retried", calls == [1])


def test_cargo_replace_retry():
    slots = cargo_slots.CargoSlots(lambda pid, start: True, path=TMP / "slots.json")
    real, calls = os.replace, []

    def flaky(src, dst):
        if len(calls) < 2:
            calls.append(1)
            raise PermissionError(13, "open by a reader")
        return real(src, dst)

    with mock.patch.object(cargo_slots.os, "replace", flaky):
        slots._write([])
    check("os.replace retried until the reader lets go", len(calls) == 2 and (TMP / "slots.json").exists())


def test_unit_red_log_names_tests():
    out = "x" * 9000 + "\nnot ok 3 - kit loads its manifest\n  ---\n  error: boom\n" + "y" * 7000
    r = h.unit_red_log(out)
    check("reason ends with the failing test names", "unit failures (1): kit loads its manifest" in r[-1500:])
    check("no failures: plain tail", h.unit_red_log("plain output") == "plain output")


for t in (test_release_only_own, test_timeout_names_holder, test_permission_error_is_contention,
          test_cargo_replace_retry, test_unit_red_log_names_tests):
    t()
print(f"{len(FAILS)} failed" if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
