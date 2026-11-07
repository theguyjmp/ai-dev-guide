"""tick.py: the one scheduled loop of an overhaul fleet (overhaul-build). Runs every 10 minutes from the scheduler
(wscript //B hidden.vbs py -3 FLEET/bin/tick.py). It replaces the master's routine turns: every routine job is a
check in FLEET/fleet.json, run here on its cadence, never by an agent remembering it.

For each check {name, cmd, every_min, timeout_min, expect, harm, fix} that is due (or failed last time):
  run it hidden, write one CHECKS.tsv row `name started ended ok|fail|timeout detail`;
  a check that fails twice in a row files FLEET/inbox/tool-<name>.md (once a day): a tool-fix unit with a test.
Built in: `recurrence` groups `Blocker: <category>` lines of today's hand-backs; a category seen twice files
FLEET/inbox/process-<category>.md and fails (R10). Then it appends proven to dash/proven.tsv and runs verdict.py.
Safe by default: one instance (PID lock, stale only when that PID is dead), never kills anything, PAUSE.md = no-op.
  py -3 tick.py [--fleet <FLEET>] [--only <check>] [--dry-run]
"""
import collections
import datetime as dt
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def now():
    return dt.datetime.now(dt.timezone.utc)


def pid_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes  # os.kill(pid, 0) on Windows terminates the process, so ask the kernel instead
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259


def lock(fleet):
    path = os.path.join(fleet, ".tick.lock")
    try:
        with open(path) as f:
            pid = int(f.read().split()[0])
        if pid != os.getpid() and pid_alive(pid):
            return False
    except (OSError, ValueError, IndexError):
        pass
    with open(path, "w") as f:
        f.write(f"{os.getpid()} {now().isoformat()}")
    return True


def history(fleet):
    rows = collections.defaultdict(list)
    try:
        with open(os.path.join(fleet, "CHECKS.tsv"), encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 5:
                    rows[p[0]].append(p)
    except OSError:
        pass
    return rows


def due(check, past):
    if not past:
        return True
    last = past[-1]
    if last[3] != "ok":
        return True
    try:
        ended = dt.datetime.fromisoformat(last[2])
    except ValueError:
        return True
    return (now() - ended).total_seconds() / 60 >= float(check.get("every_min", 20)) - 1


def run(check, fleet):
    started = now()
    try:
        p = subprocess.run(check["cmd"], shell=True, cwd=fleet, capture_output=True, text=True, errors="replace",
                           timeout=60 * float(check.get("timeout_min", 8)), creationflags=NO_WINDOW)
        out = (p.stdout or "") + (p.stderr or "")
        ok = p.returncode == 0 and (not check.get("expect") or re.search(check["expect"], out))
        status = "ok" if ok else "fail"
    except subprocess.TimeoutExpired as e:
        out, status = str(e.stdout or ""), "timeout"
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    detail = (lines[-1] if lines else "no output").replace("\t", " ")[:200]
    return [check["name"], started.isoformat(timespec="seconds"), now().isoformat(timespec="seconds"), status, detail], out


def file_inbox(fleet, name, body):
    d = os.path.join(fleet, "inbox")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{name}-{now():%Y-%m-%d}.md")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)


def recurrence(fleet):
    d, seen = os.path.join(fleet, "handbacks"), collections.Counter()
    day = now().date()
    for n in os.listdir(d) if os.path.isdir(d) else []:
        p = os.path.join(d, n)
        if dt.datetime.fromtimestamp(os.path.getmtime(p), dt.timezone.utc).date() != day:
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for m in re.finditer(r"^Blocker:\s*([a-z0-9-]+)", f.read(), re.M | re.I):
                seen[m.group(1).lower()] += 1
    twice = sorted(c for c, k in seen.items() if k >= 2 and c != "none")
    for c in twice:
        file_inbox(fleet, f"process-{c}", f"# Process fix: {c}\nBlocker category `{c}` hit {seen[c]} times today. "
                   "The dispatcher turns this into one tooling unit with a test before more units of this category launch.\n")
    return twice


def main():
    a = sys.argv[1:]
    fleet = a[a.index("--fleet") + 1] if "--fleet" in a else os.path.dirname(HERE)
    only = a[a.index("--only") + 1] if "--only" in a else None
    if os.path.exists(os.path.join(fleet, "PAUSE.md")) and not only:
        print("PAUSED: tick does nothing while FLEET/PAUSE.md exists")
        return 0
    if not lock(fleet):
        print("tick already running")
        return 0
    with open(os.path.join(fleet, "fleet.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    past, new = history(fleet), []
    for c in cfg.get("checks", []):
        if (only and c["name"] != only) or (not only and not due(c, past.get(c["name"]))):
            continue
        if "--dry-run" in a:
            print("DUE", c["name"], c["cmd"])
            continue
        if c["cmd"] == "builtin:recurrence":
            twice = recurrence(fleet)
            row = [c["name"], now().isoformat(timespec="seconds"), now().isoformat(timespec="seconds"),
                   "fail" if twice else "ok", ("repeated: " + ", ".join(twice)) if twice else "no repeated blocker"]
            out = row[4]
        else:
            row, out = run(c, fleet)
        new.append(row)
        prev = past.get(c["name"], [])
        if row[3] != "ok" and prev and prev[-1][3] != "ok" and c["cmd"] != "builtin:recurrence":
            file_inbox(fleet, f"tool-{c['name']}", f"# Tool fix: {c['name']}\nFailed twice in a row.\nCommand: `{c['cmd']}`\n"
                       "Output tail:\n```\n" + "\n".join(out.splitlines()[-30:]) + "\n```\nFix it as a unit with a test "
                       "that reproduces this failure (M0 rule: a tool bug found once becomes a unit, never a note).\n")
        print(f"{row[0]}: {row[3]} - {row[4]}")
    if new:
        with open(os.path.join(fleet, "CHECKS.tsv"), "a", encoding="utf-8") as f:
            f.writelines("\t".join(r) + "\n" for r in new)
    if "--dry-run" in a:
        return 0
    try:
        rows = json.load(open(os.path.join(fleet, "dash", "pipeline.json"), encoding="utf-8"))
        n = next(r["count"] for r in rows if r.get("bar") == "functions" and r.get("stage") == "proven")
        with open(os.path.join(fleet, "dash", "proven.tsv"), "a", encoding="utf-8") as f:
            f.write(f"{now().isoformat(timespec='seconds')}\t{n}\n")
    except (OSError, ValueError, StopIteration, KeyError):
        pass
    v = subprocess.run([sys.executable, os.path.join(HERE, "verdict.py"), "--fleet", fleet],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    print(v.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
