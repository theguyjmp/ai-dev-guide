"""Restart the gate bot when main's gate or check code is newer than the running bot (owner 2026-11-07).

The bot ran 3.5 hours on code from before R255 landed, so a landed gate fix did nothing until a hand restart.
Run by pull-main.cmd every 5 minutes. Restarts only while the bot is idle: gatebot.log and every gate job
quiet for 2 minutes. Stops the bot by its PID, never by process name, then reruns the scheduled task.
"""
import os, subprocess, time

REPO = r"Z:\Documents\AppDevelopment\VOIDCAD"
LOG = r"C:\vc\gatebot.log"
JOBS = r"Z:\vc-local\jobs"
QUIET = 120


def bot():
    """(pid, start epoch) of the `vc.py gate --loop` process, or None."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='py.exe' or Name='python.exe'\" | "
          "Where-Object { $_.CommandLine -like '*vc.py*gate*--loop*' } | "
          "ForEach-Object { \"$($_.ProcessId) $(([DateTimeOffset]$_.CreationDate).ToUnixTimeSeconds())\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         creationflags=0x08000000).stdout.split()
    return (int(out[0]), int(out[1])) if len(out) >= 2 else None


def code_time():
    out = subprocess.run(["git", "-C", REPO, "log", "-1", "--format=%ct", "origin/main", "--", "tools/gate", "tools/vc"],
                         capture_output=True, text=True, creationflags=0x08000000).stdout.strip()
    return int(out) if out.isdigit() else 0


def quiet(now):
    if now - os.path.getmtime(LOG) < QUIET:
        return False
    for d in os.listdir(JOBS):
        if d.startswith("gate--") or d.startswith("batch"):
            p = os.path.join(JOBS, d)
            if now - os.path.getmtime(p) < QUIET or any(
                    now - os.path.getmtime(os.path.join(p, f)) < QUIET for f in os.listdir(p)):
                return False
    return True


def main():
    b, now = bot(), time.time()
    if not b:
        return
    pid, started = b
    if code_time() <= started or not quiet(now):
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=0x08000000)
    time.sleep(5)
    subprocess.run(["schtasks", "/Run", "/TN", "VOIDCAD-gatebot"], capture_output=True, creationflags=0x08000000)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("gate-fresh: restarted the gate bot (pid %d) on newer tools/gate or tools/vc code at %s\n"
                % (pid, time.strftime("%Y-%m-%d %H:%M:%S")))


if __name__ == "__main__":
    main()
