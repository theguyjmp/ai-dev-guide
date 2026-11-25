"""A test whose alone re-run hits its time cap is no verdict, never red: run_one_alone marks it TIME_CAP and
quarantine.slow puts it on the known-red list with a fix request (class slow tests).

Usage: py -3 tests/test_time_cap.py"""
from __future__ import annotations

import os, subprocess, sys, tempfile
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))
    if not ok:
        FAILS.append(name)


with tempfile.TemporaryDirectory() as d:
    os.environ["HARNESS_HOME"] = d
    (Path(d) / "state").mkdir()
    sys.path.insert(0, str(HOME))
    import harness as h  # noqa: E402

    def capped(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    h.sh = capped
    tail = h.run_one_alone({"test_slow": "exe"}, "tests/property/test_slow.rs::big_case", {})
    check("a killed alone run is marked TIME_CAP", tail.startswith(h.TIME_CAP), tail)
    h.quarantine.slow(h, {"tests/property/test_slow.rs::big_case": tail}, "abc1234567")
    check("it joins the one known-red list (skipped, never red)",
          "tests/property/test_slow.rs::big_case" in h.known_red_tests(), h.known_red())
    check("with a fix request", h.quarantine.request_path(h, "tests/property/test_slow.rs::big_case").exists())
    check("the reason names the slow-tests class", "slow tests" in
          h.known_red()["tests/property/test_slow.rs::big_case"]["reason"])

print("ALL PASS" if not FAILS else f"FAILED {FAILS}")
sys.exit(1 if FAILS else 0)
