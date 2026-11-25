"""fleet_beat.py on a fresh project that follows the Run steps (state/train.off, no train ever started): no TRAIN-*
alarm and HEALTH OK. Each project created state/train.off as told and got TRAIN-PID ESCALATE on every beat.

Usage: py -3 tests/test_fleet_beat_fresh.py"""
from __future__ import annotations

import json, os, subprocess, sys, tempfile
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))
    if not ok:
        FAILS.append(name)


with tempfile.TemporaryDirectory() as d:
    home, repo = Path(d) / "home", Path(d) / "repo"
    (home / "state").mkdir(parents=True)
    (home / "state" / "train.off").touch()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    env = {**os.environ, "HARNESS_HOME": str(home), "HARNESS_REPO": str(repo), "HARNESS_PROJECT": ""}
    r = subprocess.run([sys.executable, str(HOME / "fleet_beat.py")], cwd=HOME, env=env, capture_output=True,
                       text=True, timeout=600)
    health = json.loads((home / "state" / "health.json").read_text(encoding="utf-8"))
    check("the beat runs", r.returncode == 0, r.stderr[-400:])
    check("no train alarm with state/train.off", not any(a.startswith("TRAIN") for a in health["detail"]),
          health["detail"])
    check("a fresh project is healthy", "HEALTH OK" in r.stdout, r.stdout[-400:])

print("ALL PASS" if not FAILS else f"FAILED {FAILS}")
sys.exit(1 if FAILS else 0)
