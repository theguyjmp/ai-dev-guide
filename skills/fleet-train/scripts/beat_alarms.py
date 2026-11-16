"""fleet_beat alarms as testable functions; each returns its alarm lines."""
from __future__ import annotations

import json
import sys

import quarantine

REPEAT_WINDOW = 6


def cloud_status(run) -> dict:
    """`cloud.py status` as a dict; {} when it is missing, crashes or prints anything else."""
    try:
        lines = run([sys.executable, "cloud.py", "status"]).stdout.strip().splitlines()
        cs = json.loads(lines[-1])
    except (IndexError, ValueError, OSError, AttributeError):
        return {}
    return cs if isinstance(cs, dict) else {}


def known_red(h) -> list:
    kr = h.known_red()
    if not kr:
        return []
    return [f"KNOWN-RED {len(kr)} proven tests red on main, skipped by the gate: "
            + "; ".join(f"{t.split('::')[-1]}: {quarantine.fix_status(h, t)}" for t in sorted(kr))
            + ". The train releases each once it passes alone on main"]


def repeat_test(h, ev: list) -> list:
    gates = [e for e in ev if e.get("event") == "gate_stage" and str(e.get("stage", "")).startswith("acceptance")]
    failed = [t for e in gates[-REPEAT_WINDOW:] for t in set(e.get("failed", []))]
    rep = sorted({t for t in failed if failed.count(t) >= 2} - set(h.known_red()))
    if not rep:
        return []
    return [f"REPEAT-TEST {rep[0]} failed {failed.count(rep[0])} of the last {REPEAT_WINDOW} gates but is green alone on "
            f"main (a row interaction, or not yet checked): {quarantine.fix_status(h, rep[0])}"]


def origin_behind(git_out) -> list:
    """The train pushes with check=False, so a failed push is silent."""
    out = git_out("rev-list", "--count", "origin/main..main").strip()
    if not out.isdigit() or int(out) == 0:
        return []
    return [f"ORIGIN-BEHIND origin main is {out} commits behind the train's main: `git push origin main` in the repo "
            "and read why the train's push failed"]
