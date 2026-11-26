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
g.h.git = lambda *a, **k: type("R", (), {"stdout": "abc"})()
g.h.event = lambda *a, **k: None


def boom(sha, base):
    raise RuntimeError("desk unreachable")


real_judge = g.judge
g.h.desk.offload = lambda h: True
g.h.desk.lease_refusal = lambda h: "desk lease: a higher priority holds the desk"
real_checkout = g.checkout
g.checkout = lambda sha: FAILS.append("checked out on a taken desk")
check("a taken shared desk is no verdict before any review", real_judge("abc", "def")[0] == g.NO_VERDICT)
g.h.desk.offload = lambda h: False  # cargo: units fall back to this machine, so the gate still runs
g.checkout = lambda sha: (_ for _ in ()).throw(RuntimeError("reached the gate"))
try:
    real_judge("abc", "def")
    check("a desk that only splits units never stops the gate early", False)
except RuntimeError as e:
    check("a desk that only splits units never stops the gate early", "reached the gate" in str(e), e)
g.judge = boom
check("a crash in the gate is no verdict, never red", g.main(["abc"]) == g.NO_VERDICT)
print("ALL PASS" if not FAILS else f"FAILED {FAILS}")
sys.exit(1 if FAILS else 0)
