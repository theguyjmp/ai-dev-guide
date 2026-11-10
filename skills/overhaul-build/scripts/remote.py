"""Remote builder slots: [harness.remote] in overhaul.toml runs build passes on a second machine over ssh.

drive.py fills its max_parallel local slots first, then gives a READY function to a free remote slot. A remote build
is one local process (this file, `remote.py build <job.json>`, spawned like a builder, so drive's timeout kills it):
  1. push the function's start sha to the host repo (refs/remote-builds/<id>) and main (fast-forward only);
  2. ssh to the host and run this same file there as `python -` (the job is prepended, so no remote shell
     quoting): it kills what a dropped session left in its slot, ships the harness scripts the brief's commands
     name, makes the worktree on fn/<id>, writes the brief (laptop paths mapped to host paths), runs the same
     builder command (model, caps) with the slot's CARGO_TARGET_DIR, and commits leftovers;
  3. fetch fn/<id> back into the laptop worktree. Review, the pre-review gate and land then run on the laptop.
The builder's JSON goes to stdout (drive's run file). Exit OFFLINE (75) means the host could not be used: ssh
dropped or the host is down, a git push or fetch failed, or the host has no Claude auth (one line says which). drive
then marks the host offline for offline_min, requeues the function without counting an attempt and kills what the
slot left on the host (`python -` with a kill job: the pid file of the slot, its whole tree).

[harness.remote] keys: host (ssh destination), name (label in events, default host), slots, repo (the host's git
dir the laptop pushes to), git (the laptop's remote name or URL for it, default name), work (host dir for worktrees,
briefs, slot targets and pid files, default <repo>.work), python (default "py -3"), claude (default "claude"),
ssh (command list), env (table), path_prefix (dirs put before PATH), path_map (laptop -> host path prefixes for the
brief), offline_min (default 10), remote_idle_min (default 5), heartbeat_s (default 30), timeout_min (default 45:
drive's deadline for a remote build instead of builder_timeout_min; the host shares its CPUs with heavy tests).

Host log and heartbeat: the host program appends its notes and one line per builder event to <work>/logs/<id>.log,
writes <work>/logs/<id>.hb every heartbeat_s and prints a `remote-hb` line on stderr; the builder's events go to stdout
as they come (drive's run file fills live). The laptop wrapper counts any line on the ssh stdout or stderr as life: none
for remote_idle_min means the host hung, so it kills the ssh session and exits OFFLINE (host offline, no attempt). On
any failure it fetches the host log into <runs>/<id>-host.log. A host the watchdog holds offline
(.overhaul/remote/<name>.offline, {"until": epoch, "why": ...}) is refused at once with OFFLINE.
Why (pilot2026-11-10 05:57-06:10): five desk builds in 13 min timed out with no output and no host-side trace, so the
hang could not be diagnosed; the builder's output came back only after it exited.
"""
import base64
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

HOST_JOB = None  # the job, in the program host_program sends to the host
HOST_LINE = "HOST_JOB = None  # the job, in the program host_program sends to the host"
OFFLINE = 75  # EX_TEMPFAIL: the host could not be used; not the function's attempt
SSH_DROP = 255  # ssh's own exit code for a connection it could not make or lost
OFFLINE_MIN = 10
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
       "-o", "ServerAliveCountMax=4"]
KILL_TIMEOUT_S = 60
AUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
# A builder that could not run on the host's account: no login, an expired token, a spent usage limit.
NO_ACCOUNT = re.compile(r"hit your (?:\w+ )?limit|usage limit|Invalid API key|Please run /login|OAuth token (?:has )?"
                        r"expired|authentication_error|Not logged in|Credit balance is too low", re.I)
SHIP_SKIP = ("tests", "legacy", "__pycache__")
HB_S = 30.0  # host heartbeat interval
IDLE_MIN = 5.0  # no line from the host for this long: the host hung
TIMEOUT_MIN = 45.0  # [harness.remote] timeout_min: a remote build's deadline (remote-hang-report: rows ran 23-30 min)
HB_TAG = "remote-hb"
LOG_FETCH_S = 60
HOST_LOG = []  # host side: the job's log path, once known
LIMITS_FILE = ".claude/write-limits.json"  # one pass's write limits in a builder worktree (write_guard.py)


# ---- host side: runs on the second machine as `python -`, stdlib only ----------------------------------------------

def say(msg):
    print(f"remote: {msg}", file=sys.stderr, flush=True)
    host_log(f"remote: {msg}")


def host_log(line):
    """Append one timestamped line to the host log (host side; a no-op before the job names it)."""
    if not HOST_LOG:
        return
    try:
        with open(HOST_LOG[0], "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def event_line(raw):
    """One short log line for a builder stream-json event."""
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        ev = json.loads(text)
    except ValueError:
        return f"builder: {text[:200]}"
    if not isinstance(ev, dict):
        return f"builder: {text[:200]}"
    kind = "/".join(str(ev[k]) for k in ("type", "subtype") if ev.get(k))
    msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
    tools = [c.get("name", "") for c in msg.get("content") or [] if isinstance(c, dict) and c.get("type") == "tool_use"]
    return f"builder: {kind}" + (f" tools={','.join(tools)}" if tools else "")


def heartbeat(hb_path, every, stats, stop):
    """Host side: every `every` s write the heartbeat file and a `remote-hb` stderr line, until stop is set."""
    while not stop.wait(every):
        line = f"{HB_TAG} {time.strftime('%Y-%m-%dT%H:%M:%S')} events={stats['events']} pid={stats['pid']}"
        try:
            Path(hb_path).write_text(line + "\n", encoding="utf-8")
        except OSError:
            pass
        print(line, file=sys.stderr, flush=True)


def hgit(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL)


def kill_slot(pidfile):
    """Kill the tree whose root pid the slot's pid file names (by PID, never by name), then drop the file."""
    p = Path(pidfile)
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, stdin=subprocess.DEVNULL)
    else:
        import signal
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    p.unlink(missing_ok=True)
    return True


def unship(work, ship):
    """Unpack the shipped harness dirs ({host_dir: base64 zip}) where their content hash differs."""
    for host_dir, blob in ship.items():
        dest, raw = Path(host_dir), base64.b64decode(blob)
        mark, digest = dest / ".shipped", hashlib.sha256(raw).hexdigest()
        if mark.is_file() and mark.read_text(encoding="utf-8").strip() == digest:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        zipfile.ZipFile(io.BytesIO(raw)).extractall(dest)
        mark.write_text(digest, encoding="utf-8")


def fresh_worktree(repo, wt, branch, sha):
    """A clean worktree at wt on branch at sha in the host repo; '' or why not."""
    hgit("-C", repo, "worktree", "remove", "-f", wt)
    if Path(wt).exists():
        shutil.rmtree(wt, ignore_errors=True)
    hgit("-C", repo, "worktree", "prune")
    p = hgit("-C", repo, "worktree", "add", "-q", "-f", "-B", branch, wt, sha)
    return "" if p.returncode == 0 else f"git worktree add {wt} failed: {(p.stderr or p.stdout).strip()[:300]}"


def lint_hook(wt, script, guard=""):
    """The hooks drive.py gives a builder worktree (local or on the host): the PostToolUse lint hook (lint_hook.py)
    and, when guard names write_guard.py, the PreToolUse write limits hook. Kept out of git through info/exclude;
    skipped when the project tracks its own settings.json. A stale write-limits.json is removed (set_limits)."""
    tracked = hgit("-C", wt, "ls-files", "--error-unmatch", ".claude/settings.json").returncode == 0
    if not Path(script).is_file() or tracked:
        return
    python = Path(sys.executable).as_posix()
    hooks = {"PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [
        {"type": "command", "command": f'"{python}" "{Path(script).as_posix()}"', "timeout": 90}]}]}
    if guard and Path(guard).is_file():
        hooks["PreToolUse"] = [{"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [
            {"type": "command", "command": f'"{python}" "{Path(guard).as_posix()}"', "timeout": 30}]}]
    target = Path(wt) / ".claude" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"hooks": hooks}, indent=2), encoding="utf-8")
    set_limits(wt, None)
    exclude = Path(hgit("-C", wt, "rev-parse", "--git-path", "info/exclude").stdout.strip())
    exclude = exclude if exclude.is_absolute() else Path(wt) / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    missing = [p for p in ("/.claude/settings.json", f"/{LIMITS_FILE}") if p not in text]
    if missing:
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "\n".join(missing) + "\n")


def read_limits(wt):
    """The write limits drive.py set in a laptop worktree for this pass, or None."""
    try:
        return json.loads((Path(wt) / LIMITS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def set_limits(wt, limits):
    """Write one pass's write limits ({"job", "allowed", "visibility"}, repo-relative) for write_guard.py into the
    worktree, or remove them (None). Only where lint_hook installed the guard: elsewhere the file is not
    excluded from git and a builder's `git add -A` would commit it."""
    path, settings = Path(wt) / LIMITS_FILE, Path(wt) / ".claude" / "settings.json"
    if limits is None or "write_guard.py" not in (settings.read_text(encoding="utf-8") if settings.is_file() else ""):
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(limits, indent=2), encoding="utf-8")


def account_problem(code, out):
    """The host's Claude account refused the builder (see NO_ACCOUNT): its message, else ''."""
    text = out.decode("utf-8", errors="replace")
    try:
        res = json.loads(text.strip().splitlines()[-1]) if text.strip() else {}
    except ValueError:
        res = {}
    if isinstance(res, dict) and res.get("is_error"):
        msg = str(res.get("result") or "")
    else:
        msg = text[-2000:] if code else ""
    hit = NO_ACCOUNT.search(msg)
    return " ".join(msg.split())[:200] if hit else ""


def host_build(job):
    """Steps 2 of the module docstring, on the host. Returns the builder's exit code, or OFFLINE."""
    work, wt, pidfile = Path(job["work"]), job["wt"], job["pidfile"]
    os.environ.update(job["env"])  # the git identity too: the helper's own commit uses it
    if kill_slot(pidfile):
        say(f"killed what a dropped session left in slot {job['slot']}")
    env = {**os.environ, **job["env"]}
    if job["path_prefix"]:
        env["PATH"] = os.pathsep.join([*job["path_prefix"], env.get("PATH", "")])
    if job["auth"] and not env.get(AUTH_ENV):
        say(f"no Claude auth on {job['name']}: set {AUTH_ENV} there; build requeued")
        return OFFLINE
    (work / "logs").mkdir(parents=True, exist_ok=True)
    HOST_LOG[:] = [str(work / "logs" / f"{job['fid']}.log")]
    host_log(f"start {job['fid']} attempt {job['attempt']} slot {job['slot']} sha {job['sha'][:12]}")
    stats, stop = {"events": 0, "pid": 0}, threading.Event()
    threading.Thread(target=heartbeat, args=(work / "logs" / f"{job['fid']}.hb", float(job.get("hb_s") or HB_S),
                                             stats, stop), daemon=True).start()
    try:
        return host_run(job, work, wt, pidfile, env, stats)
    finally:
        stop.set()
        host_log("end")


def host_run(job, work, wt, pidfile, env, stats):
    """host_build after the auth check: worktree, brief, the builder (its events streamed), leftovers committed."""
    unship(work, job["ship"])
    if why := fresh_worktree(job["repo"], wt, job["branch"], job["sha"]):
        say(why)
        return OFFLINE
    if job.get("lint_hook"):
        lint_hook(wt, job["lint_hook"], job.get("write_guard") or "")
        if job.get("write_limits"):
            set_limits(wt, job["write_limits"])
    brief = work / "briefs" / f"{job['fid']}.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(job["brief"], encoding="utf-8")
    cmd = [*job["cmd"], f"Read and do {brief.as_posix()}"]
    cmd[0] = shutil.which(cmd[0], path=env.get("PATH")) or cmd[0]
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    try:
        proc = subprocess.Popen(cmd, cwd=wt, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, **group)
    except OSError as e:
        say(f"cannot start the builder {cmd[0]} on {job['name']}: {e}")
        return OFFLINE
    Path(pidfile).parent.mkdir(parents=True, exist_ok=True)
    Path(pidfile).write_text(str(proc.pid), encoding="utf-8")
    stats["pid"] = proc.pid
    host_log(f"builder pid {proc.pid}: {' '.join(cmd[:3])} ...")
    tail = b""
    for raw in proc.stdout:  # each event at once: drive's run file fills live, the log gets one line per event
        sys.stdout.buffer.write(raw)
        sys.stdout.flush()
        stats["events"] += 1
        host_log(event_line(raw))
        tail = (tail + raw)[-65536:]
    code = proc.wait()
    out = tail
    host_log(f"builder exit {code} after {stats['events']} events")
    Path(pidfile).unlink(missing_ok=True)
    if job["auth"] and (why := account_problem(code, out)):
        say(f"Claude account on {job['name']} refused the builder: {why}; build requeued")
        return OFFLINE
    if hgit("-C", wt, "status", "--porcelain").stdout.strip():
        hgit("-C", wt, "add", "-A")
        msg = f"{job['fid']}: build attempt {job['attempt']} (committed by drive.py on {job['name']})"
        if hgit("-C", wt, "commit", "-q", "-m", msg).returncode:
            hgit("-C", wt, "-c", "user.name=drive.py", "-c", "user.email=drive@localhost", "commit", "-q", "-m", msg)
    return code


def host_main(job):
    if job["action"] == "kill":
        kill_slot(job["pidfile"])
        return 0
    if job["action"] == "log":  # the laptop fetches a failed job's host log
        try:
            sys.stdout.buffer.write(Path(job["log"]).read_bytes())
        except OSError as e:
            print(f"no host log {job['log']}: {e}")
        return 0
    return host_build(job)


# ---- laptop side ----------------------------------------------------------------------------------------------------

def as_list(v, default):
    if v is None or v == "" or v == []:
        return list(default)
    return [str(x) for x in v] if isinstance(v, list) else shlex.split(str(v), posix=False)


class Pool:
    """The remote slots of one host: which are busy, whether the host is offline, and the jobs that start them."""

    def __init__(self, conf, log=None, say_=None):
        self.host = str(conf["host"])
        self.name = str(conf.get("name") or self.host)
        self.slots = max(0, int(conf.get("slots", 1)))
        self.repo = str(conf["repo"]).replace("\\", "/")
        self.git = str(conf.get("git") or self.name)
        self.work = str(conf.get("work") or f"{self.repo}.work").replace("\\", "/").rstrip("/")
        self.python = as_list(conf.get("python"), ["py", "-3"])
        self.claude = str(conf.get("claude") or "claude")
        self.ssh = as_list(conf.get("ssh"), SSH)
        self.env = {str(k): str(v) for k, v in (conf.get("env") or {}).items()}
        self.path_prefix = [str(p) for p in as_list(conf.get("path_prefix"), [])]
        self.path_map = {str(k).replace("\\", "/"): str(v).replace("\\", "/")
                         for k, v in (conf.get("path_map") or {}).items()}
        self.offline_s = float(conf.get("offline_min", OFFLINE_MIN)) * 60
        self.idle_s = float(conf.get("remote_idle_min", IDLE_MIN)) * 60
        self.hb_s = float(conf.get("heartbeat_s", HB_S))
        self.timeout_s = float(conf.get("timeout_min", TIMEOUT_MIN)) * 60  # drive's deadline for a remote build
        self.offline_until = 0.0
        self.busy: dict[int, str] = {}  # slot -> fid
        self.killers: list[subprocess.Popen] = []
        self.log = log or (lambda *a, **k: None)
        self.say = say_ or (lambda msg: None)

    @classmethod
    def from_harness(cls, har, log=None, say_=None):
        """The pool [harness.remote] configures, or None (no table, slots = 0, or no host)."""
        conf = har.get("remote") if isinstance(har.get("remote"), dict) else None
        if not conf or not conf.get("host") or not conf.get("repo") or int(conf.get("slots", 1)) <= 0:
            return None
        return cls(conf, log, say_)

    # ---- slots ----
    def free_slot(self, now=None):
        """A free slot number, or None when all are busy or the host is offline."""
        if (now if now is not None else time.monotonic()) < self.offline_until:
            return None
        return next((k for k in range(self.slots) if k not in self.busy), None)

    def slot_of(self, fid):
        return next((k for k, f in self.busy.items() if f == fid), None)

    def pidfile(self, k):
        return f"{self.work}/slot-{k}.pid"

    def wt(self, fid):
        return f"{self.work}/wt/{fid}"

    # ---- start ----
    def ship(self, dirs):
        """{host_dir: base64 zip} of the laptop harness dirs ({laptop_dir: host_dir}), tests and caches left out."""
        out = {}
        for local, host in dirs.items():
            buf, root = io.BytesIO(), Path(local)
            if not root.is_dir():
                continue
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for p in sorted(root.rglob("*")):
                    rel = p.relative_to(root)
                    if p.is_file() and not any(part in SHIP_SKIP for part in rel.parts):
                        z.write(p, rel.as_posix())
            out[host] = base64.b64encode(buf.getvalue()).decode("ascii")
        return out

    def host_dirs(self, scripts, references):
        """{laptop dir: host dir} for the harness scripts and references the brief's commands and rules name."""
        return {Path(scripts).as_posix(): f"{self.work}/skill/scripts",
                Path(references).as_posix(): f"{self.work}/skill/references"}

    def map_text(self, text, pairs):
        """The brief with every laptop path prefix in pairs replaced by its host path (case-insensitive on
        Windows-style drive paths, longest first)."""
        import re
        for local, host in sorted(pairs.items(), key=lambda kv: -len(kv[0])):
            text = re.sub(re.escape(local), lambda _m, h=host: h, text, flags=re.I)
        return text

    def start(self, k, fid, attempt, cmd, custom, brief, wt, repo, main, out_dir, scripts, references, spawn,
              lean_env=None):
        """Write the job file and spawn the wrapper (`remote.py build <job>`) through drive's spawn(cmd); lean_env
        (worker.LEAN_ENV for a lean claude command) goes into the builder's environment on the host."""
        sha = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL).stdout.strip()
        dirs = self.host_dirs(scripts, references)
        ident = {}  # the laptop repo's git identity, for commits on a host that may have none
        for key, names in (("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
                           ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"))):
            val = subprocess.run(["git", "-C", str(repo), "config", key], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL).stdout.strip()
            ident.update({n: val for n in names} if val else {})
        pairs = {Path(wt).as_posix(): self.wt(fid), **dirs, **self.path_map}
        lint = Path(scripts) / "lint_hook.py"
        host_cmd = list(cmd) if custom else [self.claude, *cmd[1:]]
        job = {"action": "build", "fid": fid, "attempt": attempt, "slot": k, "name": self.name, "host": self.host,
               "repo": self.repo, "work": self.work, "wt": self.wt(fid), "branch": f"fn/{fid}", "sha": sha,
               "pidfile": self.pidfile(k), "hb_s": self.hb_s,
               "env": {**ident, **(lean_env or {}), **self.env, "CARGO_TARGET_DIR": f"{self.work}/target/slot-{k}"},
               "path_prefix": self.path_prefix, "auth": not custom, "cmd": host_cmd,
               "brief": self.map_text(Path(brief).read_text(encoding="utf-8"), pairs),
               "ship": self.ship(dirs),
               "lint_hook": f"{self.work}/skill/scripts/lint_hook.py" if lint.is_file() else "",
               "write_guard": f"{self.work}/skill/scripts/write_guard.py",
               "write_limits": read_limits(wt),
               # laptop side only
               "local": {"repo": str(repo), "wt": str(wt), "main": main, "git": self.git, "ssh": self.ssh,
                         "python": self.python, "idle_s": self.idle_s}}
        path = Path(out_dir) / f"{fid}-a{attempt}-remote.json"
        path.write_text(json.dumps(job), encoding="utf-8")
        proc = spawn([sys.executable, str(Path(__file__).resolve()), "build", str(path)])
        self.busy[k] = fid
        return proc

    # ---- finish ----
    def after(self, fid, code, timed_out, err_path, now=None, silent=False):
        """Free fid's slot. True when the host was unusable (exit OFFLINE, or `silent`: timed out with a zero-byte
        stream, which with line forwarding means a dead host; the pilot project 2026-11-10 04:05-04:35, 14 of 22 desk builds, which the router had sent to plan fix as
        oversized): the host goes offline for offline_min, its slot is cleaned on the host, and drive requeues fid
        without counting an attempt."""
        k = self.slot_of(fid)
        if k is not None:
            del self.busy[k]
        if timed_out and k is not None:
            self.kill(k)
        if not silent and (timed_out or code != OFFLINE):
            return False
        now = now if now is not None else time.monotonic()
        self.offline_until = now + self.offline_s
        why = "timed out with no builder output (host stalled or throttled)" if silent else             (last_line(err_path) or "no output")
        self.log(fid, "remote_offline", f"host={self.name} for {self.offline_s / 60:g} min: {why}"[:600])
        self.say(f"remote {self.name} offline for {self.offline_s / 60:g} min: {why}; {fid} requeued")
        if k is not None:
            self.kill(k)
        return True

    def kill_cmd(self, k):
        return [*self.ssh, self.host, *self.python, "-"]

    def kill(self, k, wait=False):
        """Kill what slot k left on the host (its pid file's tree). Fire-and-forget unless wait."""
        program = host_program({"action": "kill", "pidfile": self.pidfile(k)})
        self.killers = [p for p in self.killers if p.poll() is None]
        try:
            p = subprocess.Popen(self.kill_cmd(k), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            p.stdin.write(program)
            p.stdin.close()
        except OSError:
            return
        self.killers.append(p)
        if wait:
            try:
                p.wait(timeout=KILL_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                p.kill()

    def abort(self):
        """drive is stopping: kill every busy slot's tree on the host."""
        for k in list(self.busy):
            self.kill(k, wait=True)
        self.busy.clear()


def last_line(path):
    try:
        lines = [ln.strip() for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip()]
    except OSError:
        return ""
    tagged = [ln for ln in lines if ln.startswith("remote:")]
    return (tagged or lines or [""])[-1]


def host_program(job):
    """This file's text with the job prepended: what `python -` runs on the host."""
    src = Path(__file__).read_text(encoding="utf-8")
    return src.replace(HOST_LINE, f"HOST_JOB = {json.dumps(job)!r}", 1).encode("utf-8")


def push(loc, sha, fid):
    """sha as refs/remote-builds/<id> (forced; a branch checked out in a host worktree refuses a push, so the host
    program makes fn/<id> from it) and main (fast-forward only, a refusal is fine) to the host repo; '' or why not."""
    import land
    p = land.git(loc["repo"], "push", "-q", "-f", loc["git"], f"{sha}:refs/remote-builds/{fid}", check=False)
    if p.returncode:
        return f"git push of fn/{fid} to {loc['git']} failed: {(p.stderr or p.stdout).strip()[:300]}"
    p = land.git(loc["repo"], "push", "-q", loc["git"], f"{loc['main']}:refs/heads/{loc['main']}", check=False)
    if p.returncode and land.UNREACHABLE.search(p.stderr or ""):
        return f"git push of {loc['main']} to {loc['git']} failed: {p.stderr.strip()[:300]}"
    return ""


def fetch(loc, fid):
    """fn/<id> from the host repo into the laptop worktree (its branch reset to it); '' or why not."""
    import land
    ref = f"refs/remote-builds/{fid}"
    p = land.git(loc["repo"], "fetch", "-q", loc["git"], f"+refs/heads/fn/{fid}:{ref}", check=False)
    if p.returncode:
        return f"git fetch of fn/{fid} from {loc['git']} failed: {(p.stderr or p.stdout).strip()[:300]}"
    p = land.git(loc["wt"], "reset", "-q", "--hard", ref, check=False)
    land.git(loc["repo"], "update-ref", "-d", ref, check=False)
    return "" if p.returncode == 0 else f"reset of the worktree to fetched fn/{fid} failed: {p.stderr.strip()[:300]}"


def hold_path(repo, name):
    """The file the watchdog writes to hold a host offline: <repo>/.overhaul/remote/<name>.offline."""
    return Path(repo) / ".overhaul" / "remote" / f"{re.sub(r'[^\w.-]', '_', name)}.offline"


def held(repo, name, now=None):
    """Why the watchdog holds host `name` offline right now, else ''."""
    try:
        doc = json.loads(hold_path(repo, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    until = float(doc.get("until") or 0) if isinstance(doc, dict) else 0
    if until <= (now if now is not None else time.time()):
        return ""
    return f"held offline by the watchdog until {time.strftime('%H:%M', time.localtime(until))}: {doc.get('why', '')}"


def relay(cmd, program, idle_s, poll_s=1.0):
    """Run cmd with program on stdin; its stdout goes to ours as it comes, its stderr to ours minus heartbeat lines.
    Any line on either stream is life; none for idle_s kills cmd. (exit code, stderr text, hung)."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    last, err = [time.monotonic()], []

    def pump(stream, out, keep):
        for raw in iter(stream.readline, b""):
            last[0] = time.monotonic()
            if keep and raw.startswith(HB_TAG.encode()):
                continue
            out.write(raw)
            out.flush()
            if keep:
                err.append(raw)
    readers = [threading.Thread(target=pump, args=(proc.stdout, sys.stdout.buffer, False), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, sys.stderr.buffer, True), daemon=True)]
    for t in readers:
        t.start()
    try:
        proc.stdin.write(program)
        proc.stdin.close()
    except OSError:
        pass
    hung = False
    while True:
        try:
            proc.wait(timeout=poll_s)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() - last[0] > idle_s:
                hung = True
                proc.kill()
                proc.wait()
                break
    for t in readers:
        t.join(timeout=5)
    return proc.returncode, b"".join(err).decode("utf-8", errors="replace"), hung


def fetch_log(loc, job, dest):
    """Copy the job's host log to dest (laptop) over ssh; True when something came back."""
    log = f"{job['work']}/logs/{job['fid']}.log"
    try:
        p = subprocess.run([*loc["ssh"], job["host"], *loc["python"], "-"], input=host_program(
            {"action": "log", "log": log}), capture_output=True, timeout=LOG_FETCH_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if not p.stdout:
        return False
    Path(dest).write_bytes(p.stdout)
    return True


def run_build(job_path):
    """The wrapper drive spawns: push, run the build on the host, fetch. Builder events to stdout as they come,
    notes to stderr; on any failure the host log is fetched into <runs>/<id>-host.log."""
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    loc = job.pop("local")
    if why := held(loc["repo"], job["name"]):
        say(why)
        return OFFLINE
    code = build_on_host(job, loc)
    if code:
        dest = Path(job_path).parent / f"{job['fid']}-host.log"
        if fetch_log(loc, job, dest):  # untagged: the `remote:` line naming the cause stays the last one
            print(f"host log: {dest.as_posix()}", file=sys.stderr, flush=True)
    return code


def build_on_host(job, loc):
    import land
    if why := push(loc, job["sha"], job["fid"]):
        say(why)
        return OFFLINE
    idle_s = float(loc.get("idle_s") or IDLE_MIN * 60)
    code, err, hung = relay([*loc["ssh"], job["host"], *loc["python"], "-"], host_program(job), idle_s)
    if hung:
        say(f"{job['name']} sent nothing (no event, no heartbeat) for {idle_s / 60:g} min: host hung; build requeued")
        return OFFLINE
    if code == SSH_DROP or (code not in (0, OFFLINE) and land.UNREACHABLE.search(err)):
        say(f"{job['name']} unreachable or the session dropped (ssh exit {code}); build requeued")
        return OFFLINE
    if code:
        return code
    if why := fetch(loc, job["fid"]):
        say(why)
        return OFFLINE
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "build":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        return run_build(argv[1])
    print("usage: remote.py build <job.json>   (drive.py runs it)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(host_main(json.loads(HOST_JOB)) if HOST_JOB else main())
