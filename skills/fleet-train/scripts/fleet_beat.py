"""One-line manager heartbeat: cloud supply vs sessions, train health, plan branches; says how many builder runs to fire.
Laptop scripts cannot start cloud sessions; only the manager chat can (routine run). Run from the scripts dir.
It is a health check, not a liveness check: a train that is alive but landing nothing for hours looked healthy to a
liveness-only beat for 6 h. Every unhealthy signal prints as an ALARM naming its fix; the manager fixes each one in the
same beat. Add a check here whenever a new failure mode is found."""
import shutil
from pathlib import Path
import json, subprocess, sys, time
import harness as h
cs = json.loads(subprocess.run([sys.executable, "cloud.py", "status"], capture_output=True, text=True).stdout.strip().splitlines()[-1])
ev = h.read_events()
now = time.time()
landed1 = sum(len(e.get("rows", [])) for e in ev if e.get("event") == "land" and now - e.get("ts", 0) < 3600)
last = max((e["ts"] for e in ev if e.get("event") in ("train_start", "train_end", "gate_stage", "resolve", "land")), default=0)
alive = subprocess.run(["powershell", "-NoProfile", "-Command",
    "(Get-CimInstance Win32_Process | ? { $_.CommandLine -match 'harness.py.+train.+--loop' }).Count"],
    capture_output=True, text=True).stdout.strip()
plans = subprocess.run(["git", "ls-remote", "origin", "refs/heads/plan/cloud-*"], cwd=str(h.REPO),
    capture_output=True, text=True).stdout.count("\n")
landed2 = sum(len(e.get("rows", [])) for e in ev if e.get("event") == "land" and now - e.get("ts", 0) < 7200)
queue = h.load("queue.json", [])
alarms = []
if landed2 == 0 and queue:
    alarms.append(f"STALL 0 landed in 2h with {len(queue)} queued (last-gate.txt names the failing test)")
starts = [e for e in ev if e.get("event") == "train_start"]
ends = [e for e in ev if e.get("event") == "train_end"]
if starts and (not ends or ends[-1]["ts"] < starts[-1]["ts"]) and now - starts[-1]["ts"] > 3600:
    alarms.append(f"LONG-BATCH open {round((now - starts[-1]['ts']) / 60)} min (bisecting: find the bad row, reject it, restart)")
gates = [e for e in ev if e.get("event") == "gate_stage" and e.get("stage") == "acceptance" and now - e["ts"] < 7200]
if sum(1 for e in gates[-4:] if e.get("fails")) >= 3:
    alarms.append("REPEAT-RED acceptance red 3 of the last 4 gates (one row or main breaks a test)")
try:
    base = json.load(open(h.ST / "unit-baseline.json", encoding="utf-8")).get("fails", [])
except Exception:
    base = []
if base:
    alarms.append(f"MAIN-RED {len(base)} unit tests red on main (unit-baseline.json): brief a fix agent (clean-start/clean-submit)")
rj = [e for e in ev if e.get("event") == "reject" and now - e["ts"] < 7200]
if len(rj) >= 6 and len(rj) > 2 * landed2:
    alarms.append(f"REJECTS {len(rj)} rejects vs {landed2} landed in 2h: read the reasons, fix the shared cause")
count = {}
for e in ev:
    if e.get("event") == "reject":
        count[e["row"]] = count.get(e["row"], 0) + 1
loopers = [r for r, n in count.items() if n >= 4 and r in {q["row"] for q in queue}]
if loopers:
    alarms.append(f"REWORK rows rejected 4+ times still queued: {loopers[:5]} (needs a ruling, not another pass)")
# 2026-11-13: builders ran 2 min and quit with NO ROW for hours: 4 assignable rows, the rest waiting on 106 queued
# rows the stalled train had not landed. Supply = assigned + claimed; low supply with a big queue = land faster.
supply = (cs.get("assigned", 0) or 0) + (cs.get("claimed", 0) or 0)
if supply < 8 and len(queue) > 40:
    alarms.append(f"SUPPLY only {supply} rows for builders while {len(queue)} wait in the train queue: builders starve "
                  f"until the train lands (fix any gate red first; check batch time)")
stale = [c["row"] for c in h.load("claims.json", {}).values() if now - c.get("ts", now) > 3 * 3600]
if stale:
    alarms.append(f"STALE-CLAIMS {stale[:6]} held over 3h: release them")
if queue and now - min(q["ts"] for q in queue) > 4 * 3600:
    alarms.append(f"OLD-QUEUE oldest queued row waited {round((now - min(q['ts'] for q in queue)) / 3600, 1)} h")
if [e for e in ev if e.get("event") == "train_error" and now - e["ts"] < 3600]:
    alarms.append("TRAIN-ERROR in the last hour (events.jsonl train_error, train.err)")
free = shutil.disk_usage(h.ST).free / 1e9
if free < 60:
    alarms.append(f"DISK {round(free)} GB free: clear old target dirs and stale worktrees")
ping = Path.home() / ".claude/devping/devping.py"  # optional owner notification
project = __import__("os").environ.get("HARNESS_PROJECT", "")
if alarms and ping.exists() and project:
    subprocess.run([sys.executable, str(ping), "alert", project, "HEALTH: " + "; ".join(a.split(" (")[0] for a in alarms)],
                   capture_output=True)
waiting = cs.get("waiting_for_a_session", 0)
# one run = one session. Land first: with 20+ rows queued, builders only starve (their deps are queued, not landed),
# so cap a beat's fires at 8; once the queue drains, scale up to build every planned row.
fire = min(waiting, 8 if len(queue) >= 20 else 30)
print(f"cloud claimed {cs.get('claimed')} waiting {waiting} | landed 1h {landed1} | train procs {alive or 0}, "
      f"last event {round((now - last) / 60)} min ago | plan branches {plans} | FIRE {fire}"
      + (" | ALARMS: " + " || ".join(alarms) + " -> fix each this beat" if alarms else " | HEALTH OK"))
