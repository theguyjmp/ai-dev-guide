"""PreToolUse guard: refuse new Agent launches while FLEET/BURN-STOP exists (written by burn.py while tokens are
wasted: master overhead, agents reading without building, or nothing landing; it deletes it when that clears) or FLEET/PAUSE.md exists. Exit 2 blocks the tool call and shows the reason to the model."""
import json, os, sys

FLEET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # installed as FLEET/bin/burn-guard.py

try:
    call = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
if call.get("tool_name") not in ("Agent", "Task", "Workflow"):
    sys.exit(0)
for name in ("BURN-STOP", "PAUSE.md"):
    path = os.path.join(FLEET, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="ignore") as f:
            why = f.readline().strip()
        sys.stderr.write(f"{name} is set ({why}). No new agents: fix the waste cause (stop readers and spinners, land first, "
                         f"hand off the master context); burn.py lifts BURN-STOP when the waste rows go green.\n")
        sys.exit(2)
sys.exit(0)
