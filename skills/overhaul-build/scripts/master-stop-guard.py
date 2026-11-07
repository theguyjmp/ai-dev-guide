"""Stop hook for the overhaul-build master chat (installed as FLEET/bin/master-stop-guard.py by permissions.py
--stop-guard). Before the master ends a turn it runs the master-status checks and blocks the stop, once, with the
to-do lines when the fleet is under 3/4 of FLEET-TARGET with launchable work, wait.sh is unarmed, or the dashboard
upload, the health check, the heartbeat or the management review is over 25 min old. Silent for subagents (transcript path contains "subagents"), for chats
that never loaded overhaul-build, when stop_hook_active is set (never blocks twice in a row), and when FLEET/PAUSE.md
or FLEET/FINISHED.md exists. Any error is silent: a hook must never wedge the chat.
  py -3 master-stop-guard.py [--fleet <FLEET>]   (reads the Stop hook JSON on stdin)
"""
import importlib.util, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MARKERS = (b"overhaul-build", b"MASTER-CARD")


def load_status():
    spec = importlib.util.spec_from_file_location("master_status", os.path.join(HERE, "master-status.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def is_master(path):
    """True when the transcript shows this chat runs overhaul-build (scanned in 4 MB chunks)."""
    if not path:
        return True
    try:
        with open(path, "rb") as f:
            tail = b""
            while True:
                chunk = f.read(4 << 20)
                if not chunk:
                    return False
                if any(m in tail + chunk for m in MARKERS):
                    return True
                tail = chunk[-32:]
    except OSError:
        return True


def decide(hook, fleet):
    path = str(hook.get("transcript_path") or "")
    if hook.get("stop_hook_active") or "subagents" in path:
        return None
    if not os.path.exists(os.path.join(fleet, "BUDGET.json")) or not is_master(path):
        return None
    ms = load_status()
    s = ms.status(fleet)
    if s["paused"] or s["finished"]:
        return None
    if (s["under"] and s["launchable"]) or not s["wait_armed"] or s["dash_stale"] or s["health_stale"] or s.get("beat_stale") or s.get("stall") or s.get("managing_bad"):
        todo = ms.lines(s, fleet, mark=False)
        return {"decision": "block",
                "reason": "overhaul-build loop not done. Act on every line, then end the turn:\n" + "\n".join(todo)}
    return None


def main():
    args = sys.argv[1:]
    fleet = args[args.index("--fleet") + 1] if "--fleet" in args else os.path.dirname(HERE)
    try:
        hook = json.load(sys.stdin)
        out = decide(hook if isinstance(hook, dict) else {}, fleet)
    except Exception:  # a broken guard must never wedge the chat
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
