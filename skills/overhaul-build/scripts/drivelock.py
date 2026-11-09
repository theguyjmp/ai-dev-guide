"""drivelock.py: one drive per repo, no orphans, one job per slot target dir.

Measured 2026-11-09 on the pilot project: two supervise.py + drive.py pairs ran on one repo at once (a relaunch while the old one
was alive). They overwrote each other's .overhaul/state.json (manager resets "did not stick") and handed the same
.overhaul/target/slot-<k> to two worktrees, so a test ran a app.exe built from another worktree and reported a false
"acceptance red" (R0422). Builders started by an earlier drive kept running after that drive was killed.

- acquire(): .overhaul/drive.lock, created exclusively, holds {"pid", "start", "owner", "since"}. A lock is live only
  while that pid runs with that start time (the live process table, so a reused pid does not count); a dead holder's
  lock is taken over. A second drive or supervise exits at once with Held naming the running pid. The holder sets
  OVERHAUL_DRIVE_LOCK="<pid> <start>" in its environment, so drive.py started by supervise.py (and supervise's own
  `drive.py --reset`) runs under its parent's lock instead of refusing.
- kill_orphans(): leftover processes whose command line names this repo's .overhaul/wt or .overhaul/target dirs, killed
  by pid (taskkill /T on Windows) once the lock is held, so they can only belong to a dead drive.
- Slot markers: .overhaul/target/slot-<k>/.overhaul-slot names the drive (pid start) and the job (fid) that holds the
  slot. A slot whose marker names another live drive is never handed out; a job whose slot marker names another job
  rebuilds before its acceptance runs, and a red whose binary came from another dir or whose slot changed hands during
  the run is a rebuild, never a red.
The process table and kill are module functions (proc_start, processes, kill_pid) so tests swap in a fake table.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

ENV = "OVERHAUL_DRIVE_LOCK"
LOCK = "drive.lock"
MARKER = ".overhaul-slot"
REQUESTS = "requests"  # live re-plan's .overhaul/requests/reset-<id> (branch live-replan), when drive.py has it


class Held(Exception):
    """Another live drive or supervise holds this repo's drive lock."""


# ---- the live process table ------------------------------------------------------------------------------------------

def _start_nt(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return "" if ctypes.get_last_error() == 5 else None  # access denied: it runs, start unknown
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *[ctypes.byref(t) for t in times]):
            return ""
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        k32.CloseHandle(h)


def proc_start(pid: int) -> str | None:
    """pid's start time as a string ('' when it runs but the start cannot be read), None when it is not running."""
    if pid <= 0:
        return None
    if os.name == "nt":
        return _start_nt(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def processes() -> list[tuple[int, str]]:
    """(pid, command line) of every running process: psutil when installed, else CIM (Windows) or /proc."""
    try:
        import psutil
        out = []
        for p in psutil.process_iter(["pid", "cmdline"]):
            cmd = p.info.get("cmdline") or []
            out.append((p.info["pid"], " ".join(cmd)))
        return out
    except ImportError:
        pass
    if os.name == "nt":
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-CimInstance Win32_Process | "
              "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=120, check=False)
        try:
            rows = json.loads(r.stdout or "[]")
        except ValueError:
            return []
        return [(int(x["ProcessId"]), x.get("CommandLine") or "") for x in (rows if isinstance(rows, list) else [rows])]
    out = []
    for d in Path("/proc").glob("[0-9]*"):
        try:
            out.append((int(d.name), (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")))
        except OSError:
            pass
    return out


def kill_pid(pid: int) -> bool:
    """Kill pid and its children, by pid, never by name."""
    if os.name == "nt":
        return subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0
    try:
        os.kill(pid, 9)
        return True
    except OSError:
        return False


def me() -> str:
    """This process as '<pid> <start>'."""
    return f"{os.getpid()} {proc_start(os.getpid()) or ''}".strip()


def live(pid: int, start: str) -> bool:
    """pid runs and, when both start times are known, it is the same process that took the lock."""
    now = proc_start(pid)
    return now is not None and (not start or not now or now == start)


# ---- the drive lock -------------------------------------------------------------------------------------------------

def lock_path(repo: Path) -> Path:
    return Path(repo) / ".overhaul" / LOCK


def holder(repo: Path) -> dict | None:
    """The live holder's record, or None when the lock is free or its holder is gone."""
    try:
        rec = json.loads(lock_path(repo).read_text(encoding="utf-8"))
        pid, start = int(rec["pid"]), str(rec.get("start") or "")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return rec if live(pid, start) else None


def inherited(repo: Path) -> dict | None:
    """The holder's record when this process runs under it (started by it, OVERHAUL_DRIVE_LOCK names it)."""
    rec = holder(repo)
    mine = os.environ.get(ENV, "").split()
    if rec and mine and mine[0] == str(rec["pid"]) and (len(mine) < 2 or mine[1] == str(rec.get("start") or "")):
        return rec
    return None


def describe(rec: dict) -> str:
    return f"{rec.get('owner', '?')} pid {rec.get('pid')} (since {rec.get('since', '?')})"


def _take_stale(path: Path, seen: str) -> None:
    """Move a dead holder's lock aside; restore it if another process replaced it after we read it."""
    aside = path.with_name(f"{LOCK}.stale-{os.getpid()}")
    try:
        os.replace(path, aside)
    except OSError:
        return
    try:
        if aside.read_text(encoding="utf-8") != seen:  # a live taker won the race: give its lock back
            os.rename(aside, path)
            return
    except OSError:
        pass
    aside.unlink(missing_ok=True)


def acquire(repo: Path, owner: str) -> str:
    """Take the drive lock, or run under the holder that started this process. Returns 'taken' or 'inherited';
    raises Held when another live drive or supervise holds it."""
    if inherited(repo):
        return "inherited"
    path = lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(20):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                seen = path.read_text(encoding="utf-8")
            except OSError:
                time.sleep(0.05)
                continue
            rec = holder(repo)
            if rec:
                raise Held(f"{path.as_posix()} is held by {describe(rec)}: one drive per repo")
            if not seen.strip():  # being written by a taker right now
                time.sleep(0.05)
                continue
            _take_stale(path, seen)
            continue
        pid, start = os.getpid(), proc_start(os.getpid()) or ""
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"pid": pid, "start": start, "owner": owner,
                                "since": datetime.now().isoformat(timespec="seconds")}))
        os.environ[ENV] = f"{pid} {start}".strip()
        return "taken"
    raise Held(f"{path.as_posix()}: could not take the lock")


def release(repo: Path) -> None:
    """Remove the lock if this process holds it."""
    path = lock_path(repo)
    try:
        if int(json.loads(path.read_text(encoding="utf-8"))["pid"]) == os.getpid():
            path.unlink()
            os.environ.pop(ENV, None)
    except (OSError, ValueError, KeyError, TypeError):
        pass


def refuse_admin(repo: Path, flag: str, live_replan: bool) -> str:
    """The error for --reset/--mark-proven/--repolish while another drive holds the lock."""
    rec = holder(repo)
    how = (f"write {(Path(repo) / '.overhaul' / REQUESTS).as_posix()}/reset-<id> and the running drive applies it "
           "at its next cycle (live re-plan)" if live_replan and flag == "--reset" else
           f"stop it (pid {rec['pid'] if rec else '?'}, which also stops its builders), then run {flag} again")
    return f"{flag} refused: {describe(rec) if rec else 'another drive'} holds {lock_path(repo).as_posix()}; {how}"


# ---- orphans --------------------------------------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"/+", "/", text.replace("\\", "/")).lower()


def orphan_pattern(repo: Path) -> re.Pattern:
    root = _norm(str(Path(repo).resolve())).rstrip("/") + "/.overhaul/"
    return re.compile(re.escape(root) + r"(?:wt|target)(?![\w.-])")


def kill_orphans(repo: Path, log=lambda pid, cmd: None) -> list[int]:
    """Kill leftover processes whose command line names this repo's .overhaul/wt or .overhaul/target, except this
    process and the lock holder. Call only while holding the lock: every such process then belongs to a dead drive."""
    pat = orphan_pattern(repo)
    rec = holder(repo) or {}
    keep = {os.getpid(), os.getppid(), int(rec.get("pid") or 0)}
    killed = []
    for pid, cmd in processes():
        if pid in keep or not cmd or not pat.search(_norm(cmd)):
            continue
        if kill_pid(pid):
            killed.append(pid)
            log(pid, cmd)
    return killed


# ---- slot target dirs -----------------------------------------------------------------------------------------------

def read_marker(slot_dir: Path) -> dict:
    try:
        rec = json.loads((Path(slot_dir) / MARKER).read_text(encoding="utf-8"))
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def held_by_other(slot_dir: Path, drive_id: str) -> bool:
    """The slot's marker names another drive that still runs."""
    rec = read_marker(slot_dir)
    other = str(rec.get("drive") or "")
    if not other or other == drive_id:
        return False
    parts = other.split()
    return live(int(parts[0]), parts[1] if len(parts) > 1 else "")


def claim(slot_dir: Path, drive_id: str, fid: str) -> None:
    Path(slot_dir).mkdir(parents=True, exist_ok=True)
    tmp = Path(slot_dir) / f"{MARKER}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({"drive": drive_id, "fid": fid,
                               "since": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
    os.replace(tmp, Path(slot_dir) / MARKER)


def owns(slot_dir: Path, drive_id: str, fid: str) -> bool:
    rec = read_marker(slot_dir)
    return rec.get("drive") == drive_id and rec.get("fid") == fid


def check_slots(slots: dict, target: Path, drive_id: str) -> tuple[dict[str, int], list[str]]:
    """The persisted fid -> k map, checked at start: one fid per slot, and no slot another live drive holds.
    Returns (kept, dropped notes)."""
    kept: dict[str, int] = {}
    notes = []
    for fid, k in (slots or {}).items():
        try:
            k = int(k)
        except (TypeError, ValueError):
            notes.append(f"{fid}: bad slot {k!r}")
            continue
        if k in kept.values():
            notes.append(f"{fid}: slot-{k} already given to another job")
        elif held_by_other(Path(target) / f"slot-{k}", drive_id):
            notes.append(f"{fid}: slot-{k} is held by live drive {read_marker(Path(target) / f'slot-{k}').get('drive')}")
        else:
            kept[str(fid)] = k
    return kept, notes


BIN_RUN = re.compile(r"^\s*Running (?:unittests )?\S+ \(([^)]+)\)", re.M)


def foreign_binaries(output: str, slot_dir: Path, wt: Path) -> list[str]:
    """Test binaries the output says ran from outside this job's slot target dir (and outside its worktree)."""
    roots = [_norm(str(Path(slot_dir).resolve())), _norm(str(Path(wt).resolve()))]
    bad = []
    for path in BIN_RUN.findall(output or ""):
        p = _norm(path.strip())
        if (os.path.isabs(path.strip()) or re.match(r"^[a-z]:/", p)) and not any(p.startswith(r) for r in roots):
            bad.append(path.strip())
    return bad
