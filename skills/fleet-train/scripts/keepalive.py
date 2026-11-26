"""Watchdog of the cycle's long-running processes; config and scheduling: CYCLE.md "Operations".

    py -3 keepalive.py --home <HARNESS_HOME>
"""
from __future__ import annotations

import argparse, json, os, shutil, subprocess, sys, time
from dataclasses import dataclass, field
from pathlib import Path

NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class Proc:
    name: str
    cmd: tuple
    cwd: str = "."


@dataclass(frozen=True)
class Config:
    procs: tuple = ()
    disk: dict = field(default_factory=dict)

    @classmethod
    def read(cls, p: Path) -> "Config":
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls(tuple(Proc(x["name"], tuple(x["cmd"]), x.get("cwd", ".")) for x in d.get("processes", [])),
                   d.get("disk", {}))


def alive(pid: int) -> bool | None:
    """True runs, False gone, None unknown (a failed probe is no evidence)."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return True
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                       creationflags=NO_WINDOW)
    return None if r.returncode else f'"{pid}"' in r.stdout


def run_file(p: Proc, home: Path) -> Path:
    """<state>/<name>.run.cmd: p's command, quoted for cmd.exe, appending to <name>.out."""
    f = home / "state" / f"{p.name}.run.cmd"
    line = subprocess.list2cmdline(list(p.cmd)).replace("%", "%%")
    out = home / "state" / f"{p.name}.out"
    f.write_text(f'@echo off\r\n{line} >> "{out}" 2>&1\r\n', encoding="utf-8", newline="")
    return f


def start_detached(p: Proc, home: Path) -> int:
    """Starts p outside the caller's job, with no console window; returns its pid."""
    out = home / "state" / f"{p.name}.out"
    cwd = str((home / p.cwd).resolve())
    if os.name == "nt":
        line = f'cmd /c "{run_file(p, home)}"'  # nesting the command in `cmd /c` mangled any argument with a space
        q = lambda s: s.replace("'", "''")  # noqa: E731
        ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
              f"(Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine='{q(line)}'; "
              f"CurrentDirectory='{q(cwd)}'; ProcessStartupInformation=$si}}).ProcessId")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                           creationflags=NO_WINDOW, timeout=60)
        return int(r.stdout.strip() or 0)
    with open(out, "ab") as f:
        return subprocess.Popen(list(p.cmd), cwd=cwd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True).pid


def tick(home: Path, cfg: Config, start=start_detached, probe=alive) -> list:
    """Restarts what is positively gone or never started; returns the log lines."""
    st, lines = home / "state", []
    st.mkdir(parents=True, exist_ok=True)
    for p in cfg.procs:
        pf = st / f"{p.name}.pid"
        pid = int(pf.read_text().strip() or 0) if pf.exists() else 0
        if pid and probe(pid) is not False:
            continue
        new = start(p, home)
        pf.write_text(str(new), encoding="utf-8")
        lines.append(f"{p.name} was down: restarted pid {new}")
    d = cfg.disk
    if d:
        free = shutil.disk_usage(home / d.get("path", ".")).free / 1e9
        if free < d.get("prune_below_gb", 0) and d.get("prune_cmd"):
            subprocess.run(d["prune_cmd"], cwd=home, timeout=1800)
            lines.append(f"disk {free:.0f} GB free: pruned")
        if free < d.get("alarm_below_gb", 0):
            lines.append(f"OWNER ALARM disk {free:.0f} GB free")
    with open(st / "keepalive.log", "a", encoding="utf-8") as f:
        f.writelines(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {l}\n" for l in lines)
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", default=os.environ.get("HARNESS_HOME", "."))
    home = Path(ap.parse_args(argv).home)
    for line in tick(home, Config.read(home / "keepalive.json")):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
