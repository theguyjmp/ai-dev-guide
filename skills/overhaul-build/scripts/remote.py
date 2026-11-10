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
brief), offline_min (default 10).
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


# ---- host side: runs on the second machine as `python -`, stdlib only ----------------------------------------------

def say(msg):
    print(f"remote: {msg}", file=sys.stderr, flush=True)


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


def lint_hook(wt, script):
    """The PostToolUse lint hook drive.py gives a local worktree, pointed at the shipped lint_hook.py."""
    tracked = hgit("-C", wt, "ls-files", "--error-unmatch", ".claude/settings.json").returncode == 0
    if not Path(script).is_file() or tracked:
        return
    hook = {"type": "command", "command": f'"{Path(sys.executable).as_posix()}" "{Path(script).as_posix()}"',
            "timeout": 90}
    target = Path(wt) / ".claude" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [hook]}]}},
                                 indent=2), encoding="utf-8")
    exclude = Path(hgit("-C", wt, "rev-parse", "--git-path", "info/exclude").stdout.strip())
    exclude = exclude if exclude.is_absolute() else Path(wt) / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    if "/.claude/settings.json" not in (exclude.read_text(encoding="utf-8") if exclude.is_file() else ""):
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write("\n/.claude/settings.json\n")


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
    unship(work, job["ship"])
    if why := fresh_worktree(job["repo"], wt, job["branch"], job["sha"]):
        say(why)
        return OFFLINE
    if job.get("lint_hook"):
        lint_hook(wt, job["lint_hook"])
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
    out = proc.communicate()[0]
    code = proc.returncode
    Path(pidfile).unlink(missing_ok=True)
    sys.stdout.buffer.write(out)  # the builder's JSON: drive's run file
    sys.stdout.flush()
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
               "pidfile": self.pidfile(k),
               "env": {**ident, **(lean_env or {}), **self.env, "CARGO_TARGET_DIR": f"{self.work}/target/slot-{k}"},
               "path_prefix": self.path_prefix, "auth": not custom, "cmd": host_cmd,
               "brief": self.map_text(Path(brief).read_text(encoding="utf-8"), pairs),
               "ship": self.ship(dirs),
               "lint_hook": f"{self.work}/skill/scripts/lint_hook.py" if lint.is_file() else "",
               # laptop side only
               "local": {"repo": str(repo), "wt": str(wt), "main": main, "git": self.git, "ssh": self.ssh,
                         "python": self.python}}
        path = Path(out_dir) / f"{fid}-a{attempt}-remote.json"
        path.write_text(json.dumps(job), encoding="utf-8")
        proc = spawn([sys.executable, str(Path(__file__).resolve()), "build", str(path)])
        self.busy[k] = fid
        return proc

    # ---- finish ----
    def after(self, fid, code, timed_out, err_path, now=None):
        """Free fid's slot. True when the host was unusable (exit OFFLINE): the host goes offline for offline_min,
        its slot is cleaned on the host, and drive requeues fid without counting an attempt."""
        k = self.slot_of(fid)
        if k is not None:
            del self.busy[k]
        if timed_out and k is not None:
            self.kill(k)
        if timed_out or code != OFFLINE:
            return False
        now = now if now is not None else time.monotonic()
        self.offline_until = now + self.offline_s
        why = last_line(err_path) or "no output"
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


def run_build(job_path):
    """The wrapper drive spawns: push, run the build on the host, fetch. Builder JSON to stdout, notes to stderr."""
    import land
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    loc = job.pop("local")
    if why := push(loc, job["sha"], job["fid"]):
        say(why)
        return OFFLINE
    p = subprocess.run([*loc["ssh"], job["host"], *loc["python"], "-"], input=host_program(job),
                       stderr=subprocess.PIPE)
    err = p.stderr.decode("utf-8", errors="replace")
    sys.stderr.write(err)
    if p.returncode == SSH_DROP or (p.returncode not in (0, OFFLINE) and land.UNREACHABLE.search(err)):
        say(f"{job['name']} unreachable or the session dropped (ssh exit {p.returncode}); build requeued")
        return OFFLINE
    if p.returncode:
        return p.returncode
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
