"""One-line manager heartbeat: cloud supply vs sessions, train health, plan branches; says how many builder runs to fire.
Laptop scripts cannot start cloud sessions; only the manager chat can (routine run). Run from the scripts dir.
It is a health check, not a liveness check: a train that is alive but landing nothing for hours looked healthy to a
liveness-only beat for 6 h. Every unhealthy signal prints as an ALARM naming its fix; the manager fixes each one in the
same beat. Add a check here whenever a new failure mode is found.
Every subprocess call has a timeout: one hung call must not hang the beat."""
import shutil
from pathlib import Path
import json, os, re, subprocess, sys, time
import harness as h

TIMEOUTS = []


def run(cmd, timeout=120, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        TIMEOUTS.append(" ".join(map(str, cmd))[:60])
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


def ps(script, timeout=60):
    return run(["powershell", "-NoProfile", "-Command", script], timeout=timeout).stdout.strip()


def train_cmdline():
    """Command line of this project's train (the pid in state/train.pid), '' when it is not running."""
    try:
        pid = int((h.ST / "train.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return ""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return Path(f"/proc/{pid}/cmdline").read_text(errors="replace").replace("\0", " ") or "train"
        except OSError:
            return ""
    out = ps(f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine")
    return out if re.search(r"(harness|run_train)\.py.*train", out) else ""  # run_train.py = optional hidden pythonw wrapper


def start_train():
    """Start the train detached through WMI (Win32_Process Create). Never Start-Process or Popen from an agent shell: the
    child stays in the shell's job and dies with it (a train died twice that way, silently). WMI does not pass this
    process's environment, so the HARNESS_* settings go on the command line. Returns the new cmd.exe pid or ''."""
    sets = "".join(f'set "{k}={v}"&& ' for k, v in os.environ.items() if k.startswith("HARNESS_"))
    log = h.ST / "train.log"
    # quoted full path: a restart script that kills by a 'harness.py train' pattern cannot match it
    cmd = f'cmd /c "{sets}"{sys.executable}" -u "{Path(h.__file__).resolve()}" train --loop >> "{log}" 2>&1"'
    wrap = Path(h.__file__).resolve().parent / "run_train.py"  # optional project wrapper: pythonw + CREATE_NO_WINDOW, no console popups
    if wrap.exists():
        pyw = Path(sys.executable).with_name("pythonw.exe")
        cmd = f'cmd /c "{sets}"{pyw if pyw.exists() else sys.executable}" -u "{wrap}" train --loop >> "{log}" 2>&1"'
    q = lambda s: s.replace("'", "''")
    return ps(f"([wmiclass]'Win32_Process').Create('{q(cmd)}', '{q(str(Path(h.__file__).resolve().parent))}').ProcessId")


try:
    cs = json.loads(run([sys.executable, "cloud.py", "status"]).stdout.strip().splitlines()[-1])
except (IndexError, ValueError):
    cs = {}
ev = h.read_events()
now = time.time()
landed1 = sum(len(e.get("rows", [])) for e in ev if e.get("event") == "land" and now - e.get("ts", 0) < 3600)
last = max((e["ts"] for e in ev if e.get("event") in ("train_start", "train_end", "gate_stage", "resolve", "land")), default=0)
train = train_cmdline()
plans = run(["git", "ls-remote", "origin", "refs/heads/plan/cloud-*"], cwd=str(h.REPO)).stdout.count("\n")
landed2 = sum(len(e.get("rows", [])) for e in ev if e.get("event") == "land" and now - e.get("ts", 0) < 7200)
queue = h.load("queue.json", [])
alarms = []
if not train and not (h.ST / "train.pid").exists():  # a train older than train.pid: no safe way to tell it is ours
    alarms.append("TRAIN-PID no state/train.pid: stop this project's train once (its own pid only) and let this beat start it")
elif not train and not (h.ST / "train.off").exists():  # state/train.off = stopped on purpose
    pid = start_train() if os.name == "nt" else ""
    alarms.append(f"TRAIN-DOWN no train process: restarted detached (pid {pid})" if pid else
                  "TRAIN-DOWN no train process: start it detached (Win32_Process Create, never Start-Process from an agent shell)")
elif re.search(r"harness\.py\s+train", train):
    alarms.append("TRAIN-KILLABLE the train's command line matches 'harness.py train', which a pattern-killing restart script "
                  "kills: stop it (taskkill /T /PID <state/train.pid>) and let this beat restart it with the quoted full path")
if landed2 == 0 and queue:
    alarms.append(f"STALL 0 landed in 2h with {len(queue)} queued (last-gate.txt names the failing test)")
starts = [e for e in ev if e.get("event") == "train_start"]
ends = [e for e in ev if e.get("event") == "train_end"]
if starts and (not ends or ends[-1]["ts"] < starts[-1]["ts"]) and now - starts[-1]["ts"] > 3600:
    alarms.append(f"LONG-BATCH open {round((now - starts[-1]['ts']) / 60)} min (bisecting: find the bad row, reject it, restart)")
gates = [e for e in ev if e.get("event") == "gate_stage" and e.get("stage") == "acceptance" and now - e["ts"] < 7200]
if sum(1 for e in gates[-4:] if e.get("fails")) >= 3:
    alarms.append("REPEAT-RED acceptance red 3 of the last 4 gates (one row or main breaks a test)")
lg = h.ST / "last-gate.txt"
# a gate older than the running train (or its current batch) was already handled by that restart: ignore it
born = max((p.stat().st_mtime for p in (h.ST / "train.pid", h.ST / "train.lock") if p.exists()), default=0)
if h.P["env_fail_re"] and lg.exists() and now - lg.stat().st_mtime < 7200 and lg.stat().st_mtime > born and \
        re.search(h.P["env_fail_re"], lg.read_text(encoding="utf-8", errors="replace")):
    alarms.append("TRAIN-ENV the last gate failed on the toolchain environment, not a row (train started from Git Bash?): "
                  "restart the train from PowerShell or cmd and un-reject the rows that gate rejected")
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
if TIMEOUTS:
    alarms.append(f"BEAT-TIMEOUT {TIMEOUTS[:3]} timed out: the numbers above may be partial")
ping = Path.home() / ".claude/devping/devping.py"  # optional owner notification
project = os.environ.get("HARNESS_PROJECT", "")
if alarms and ping.exists() and project:
    run([sys.executable, str(ping), "alert", project, "HEALTH: " + "; ".join(a.split(" (")[0] for a in alarms)], timeout=60)
waiting = cs.get("waiting_for_a_session", 0)
fire = min(waiting, 30)  # scale builders to every assigned row; landing keeps up via the trial-merge batch pick
print(f"cloud claimed {cs.get('claimed')} waiting {waiting} | landed 1h {landed1} | train {'up' if train else 'down'}, "
      f"last event {round((now - last) / 60)} min ago | plan branches {plans} | FIRE {fire}"
      + (" | ALARMS: " + " || ".join(alarms) + " -> fix each this beat" if alarms else " | HEALTH OK"))
