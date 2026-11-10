"""PreToolUse launch guard on Agent|Task|Workflow (overhaul-build; installed as FLEET/bin/burn-guard.py by
dev-setup permissions.py --guard). It is the only gate an agent launch passes, so these rules hold by mechanism:
  1 PAUSE.md or BURN-STOP (burn.py: tokens wasted) refuses every launch;
  2 the prompt is exactly "Read and do <brief path>." and the brief is a `ready` DISPATCH.md row or a standing brief
    in fleet.json (default briefs/heartbeat.md): no inline prompts, no master-written work;
  3 the same brief launches once (a standing brief at most once per 15 min): no two agents on one unit;
  4 while dash/verdict.json says brake (land backlog over the brake), only briefs matching fleet.json brake_allow
    (default land-|fix-|tool-|review-) launch.
Exit 2 blocks the call and shows the reason. Silent (exit 0) when FLEET/fleet.json does not exist."""
import datetime as dt
import json
import os
import re
import sys

FLEET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT = re.compile(r"^\s*Read and do\s+(\S+?\.md)\.?\s*$", re.I)


def block(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(2)


def tail(path):
    """Compare briefs by their briefs/<name>.md tail, whatever prefix the prompt or the row uses."""
    p = path.replace("\\", "/")
    i = p.rfind("briefs/")
    return p[i:] if i >= 0 else p


def dispatch_status(brief):
    try:
        text = open(os.path.join(FLEET, "DISPATCH.md"), encoding="utf-8", errors="ignore").read()
    except OSError:
        return None
    for line in text.splitlines():
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if len(cells) > 2 and any(tail(c) == brief for c in cells):
            return cells[-1].lower()
    return None


def main():
    try:
        call = json.load(sys.stdin)
    except ValueError:
        return 0
    if call.get("tool_name") not in ("Agent", "Task", "Workflow"):
        return 0
    cfg_path = os.path.join(FLEET, "fleet.json")
    if not os.path.exists(cfg_path):
        return 0
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    for name in ("PAUSE.md", "BURN-STOP"):
        if os.path.exists(os.path.join(FLEET, name)):
            why = open(os.path.join(FLEET, name), encoding="utf-8", errors="ignore").readline().strip()
            block(f"{name} is set ({why}). No launches until it is lifted (burn.py lifts BURN-STOP when waste clears).")
    prompt = str((call.get("tool_input") or {}).get("prompt", ""))
    m = PROMPT.match(prompt)
    if not m:
        block('Launch refused: the prompt must be exactly "Read and do <brief path>." for a ready DISPATCH.md line. '
              "Write work into a brief through the dispatcher; never type an inline task.")
    brief = tail(m.group(1))
    standing = [tail(b) for b in cfg.get("standing_briefs", ["briefs/heartbeat.md"])]
    status = dispatch_status(brief)
    if brief not in standing and status != "ready":
        block(f"Launch refused: {brief} is not a ready DISPATCH.md row (status: {status or 'no row'}). "
              "Run the dispatcher; it writes ready rows only for units whose dependencies have landed.")
    t = dt.datetime.now(dt.timezone.utc)
    log = os.path.join(FLEET, "launched.tsv")
    try:
        for line in open(log, encoding="utf-8"):
            when, b = (line.rstrip("\n").split("\t") + [""])[:2]
            age = (t - dt.datetime.fromisoformat(when)).total_seconds() / 60
            if b == brief and (brief not in standing or age < 15):
                block(f"Launch refused: {brief} was already launched {int(age)} min ago. A new pass gets a new brief "
                      "(wip hand-off, <unit>-<n>.md); two agents never build one unit.")
    except (OSError, ValueError):
        pass
    try:
        v = json.load(open(os.path.join(FLEET, "dash", "verdict.json"), encoding="utf-8"))
    except (OSError, ValueError):
        v = {}
    allow = re.compile(cfg.get("brake_allow", r"^(land|fix|tool|review)-"))
    if v.get("brake") and brief not in standing and not allow.search(os.path.basename(brief)):
        block("Launch refused: land backlog over the brake (dash/verdict.json). Only landing, fix, tool and review "
              "briefs launch until land-keeper brings the backlog under it.")
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{t.isoformat(timespec='seconds')}\t{brief}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
