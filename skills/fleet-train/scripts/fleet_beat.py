"""One-line manager heartbeat: cloud supply vs sessions, train health, plan branches; says how many builder runs to fire.
Laptop scripts cannot start cloud sessions; only the manager chat can (routine run). Run from the scripts dir."""
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
waiting = cs.get("waiting_for_a_session", 0)
fire = min(waiting, 8)  # one run = one session; cap a heartbeat's fires
print(f"cloud claimed {cs.get('claimed')} waiting {waiting} | landed 1h {landed1} | train procs {alive or 0}, "
      f"last event {round((now - last) / 60)} min ago | plan branches {plans} | FIRE {fire}")
