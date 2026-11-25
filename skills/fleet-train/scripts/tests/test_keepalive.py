"""keepalive.tick: restarts only processes positively gone, never on an unknown probe; disk alarm lines.

Usage: py -3 tests/test_keepalive.py"""
from __future__ import annotations

import json, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import keepalive as ka  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))
    if not ok:
        FAILS.append(name)


with tempfile.TemporaryDirectory() as d:
    home = Path(d)
    (home / "keepalive.json").write_text(json.dumps({"processes": [
        {"name": "verifier", "cmd": ["x"]}, {"name": "collector", "cmd": ["y"]}, {"name": "native", "cmd": ["z"]}],
        "disk": {"alarm_below_gb": 10 ** 9}}), encoding="utf-8")
    cfg = ka.Config.read(home / "keepalive.json")
    (home / "state").mkdir()
    (home / "state" / "collector.pid").write_text("11")
    (home / "state" / "native.pid").write_text("12")
    started = []
    probe = {11: True, 12: None}.get
    lines = ka.tick(home, cfg, start=lambda p, h: started.append(p.name) or 99, probe=lambda pid: probe(pid, False))
    check("a process with no pid file starts", "verifier" in started, started)
    check("a running one is left alone", "collector" not in started, started)
    check("an unknown probe is no evidence: no restart", "native" not in started, started)
    check("the new pid is written", (home / "state" / "verifier.pid").read_text() == "99")
    check("a low disk is an owner alarm", any("OWNER ALARM" in l for l in lines), lines)
    check("the log records it", "verifier was down" in (home / "state" / "keepalive.log").read_text())

print("ALL PASS" if not FAILS else f"FAILED {FAILS}")
sys.exit(1 if FAILS else 0)
