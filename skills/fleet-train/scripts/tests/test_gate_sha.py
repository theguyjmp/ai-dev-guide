"""gate_sha.verdict: the verifier gate command's exit code (green, red, no verdict) from one gate result.

Usage: py -3 tests/test_gate_sha.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gate_sha as g  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))
    if not ok:
        FAILS.append(name)


g.h.known_red_tests = lambda: {"tests/a.rs::known"}
g.h.P["env_fail_re"] = r"linker not found"
check("green is green", g.verdict(True, [], "") == g.GREEN)
check("a red test is red", g.verdict(False, ["tests/a.rs::x"], "") == g.RED)
check("only known-red failures are green", g.verdict(False, ["tests/a.rs::known"], "") == g.GREEN)
check("an environment failure is no verdict, never red", g.verdict(False, ["tests/a.rs::x"], "linker not found") ==
      g.NO_VERDICT)
check("a red naming nothing is no verdict", g.verdict(False, [], "killed") == g.NO_VERDICT)
slow = "tests/p.rs::slow"
check("a time-capped test alone is no verdict, never a pass", g.verdict(False, [slow], "", {slow}) == g.NO_VERDICT)
check("capped at the base too: pre-existing slow, the rest stands", g.verdict(False, [slow], "", {slow}, {slow}) ==
      g.GREEN)
check("a real red beside a capped test is red", g.verdict(False, [slow, "tests/a.rs::x"], "", {slow}) == g.RED)
print("ALL PASS" if not FAILS else f"FAILED {FAILS}")
sys.exit(1 if FAILS else 0)
