#!/usr/bin/env python3
"""Overhaul v2 harness: build, review and land every function in spec/FUNCTIONS.md.

A READY function (planned, deps proven, retries left) gets a worktree at .overhaul/wt/<id>
on fn/<id> from main, a brief from templates/, and a `claude -p` builder. A finished build
gets a blind reviewer; a PASS goes to land.py. A failure retries from main with its reason
in the brief, then parks the function as needs-plan for the manager.

    py -3 drive.py --repo R                      run until done (0) or manager needed (3)
    py -3 drive.py --repo R --reset ID           planned, attempts 0
    py -3 drive.py --repo R --mark-proven ID --evidence FILE

Exit 0 = every function proven (or a command done), 3 = manager needed, 1 = error.

state.json is shared with land.py under the lock file .overhaul/lock (land.py's acquire_lock and
release_lock, imported). land.py holds it for a whole land, so drive.py holds it only around its own state.json write, never
while land.py runs, and defers a write (retried every loop pass) while land.py has it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import tomllib
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import land as land_mod  # noqa: E402  the lock protocol is land.py's

HERE = Path(os.path.abspath(__file__)).parent
TEMPLATES = HERE / "templates"
COLUMNS = ("id", "phase", "module", "deps", "entry", "acceptance", "status")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
PATHS_LINE = re.compile(r"\W*(?:paths?|files)\W*?[:=]\s*(.+)", re.I)
IN_FLIGHT = ("building", "review", "landing")
GREEN_BEFORE = "acceptance already green before change"
FENCE = {".py": "python", ".rs": "rust", ".js": "javascript", ".ts": "typescript"}
REVIEW_MAX_CALLS = 20
REASON_MAX = 1500
POLL_S = 0.25
DASH_EVERY_S = 15
LOCK_WAIT_S = 120.0  # a write that must happen now waits this long for land.py's lock
DEFAULTS = {
    "limits": {"max_parallel": 4, "builder_max_calls": 60, "brief_max_tokens": 40000,
               "retries_per_function": 1, "builder_timeout_min": 30},
    "paths": {"functions": "spec/FUNCTIONS.md", "architecture": "spec/ARCHITECTURE.md",
              "state": ".overhaul/state.json"},
}


class DriveError(Exception):
    """A config, setup or git failure: drive exits 1."""


@dataclass
class Fn:
    id: str
    phase: str
    module: str
    deps: list[str]
    entry: str
    acceptance: list[str]
    status: str
    header: str
    row: str
    order: int


@dataclass
class Job:
    fn: str
    kind: str  # build | review | land
    proc: subprocess.Popen
    out: Path
    started: float
    deadline: float
    attempt: int
    head: str = ""
    timed_out: bool = False
    log_at: int = 0  # size of log.jsonl when a land job started


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").lstrip("﻿")
    except OSError:
        return ""


def run(cmd: list[str], cwd: Path, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def git(cwd: Path, *args: str) -> str:
    r = run(["git", *args], cwd)
    if r.returncode != 0:
        raise DriveError(f"git {' '.join(args)}: {last_line(r.stderr) or last_line(r.stdout)}")
    return r.stdout


def force_remove(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def render(name: str, values: dict[str, str]) -> str:
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), text)


def spawn(cmd: list[str], cwd: Path, out: Path) -> subprocess.Popen:
    """Start a worker in its own process group; stdout to out, stderr beside it."""
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    with open(out, "wb") as fo, open(out.with_suffix(".err"), "wb") as fe:
        return subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, **group)


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill this process and its children, by PID, never by name."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


@lru_cache(maxsize=None)
def supports_max_turns(exe: str) -> bool:
    try:
        return "--max-turns" in run([exe, "--help"], Path.cwd(), timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def read_result(path: Path) -> tuple[int | None, int | None, str]:
    """(tokens, turns, final text) from a `claude -p --output-format json` result."""
    raw = read_text(path)
    data: object = None
    for chunk in (raw, *reversed(raw.splitlines())):
        try:
            data = json.loads(chunk)
            break
        except ValueError:
            continue
    if isinstance(data, list):
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), None)
    if not isinstance(data, dict):
        return None, None, ""
    usage = data.get("usage") or {}
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    tokens = sum(int(usage.get(k) or 0) for k in keys) if usage else None
    turns = data.get("num_turns")
    return tokens, turns if isinstance(turns, int) else None, str(data.get("result") or "")


def cells(line: str) -> list[str]:
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") and not s.endswith("\\|") else s
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def bare(text: str) -> str:
    return text.strip().strip("`").strip()


def parse_functions(text: str) -> list[Fn]:
    """Rows of every markdown table whose header has the FUNCTIONS.md columns."""
    fns: list[Fn] = []
    idx: dict[str, int] | None = None
    header = ""
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            idx = None
            continue
        row = cells(line)
        names = [c.lower() for c in row]
        if all(c in names for c in COLUMNS):
            idx, header = {c: names.index(c) for c in COLUMNS}, line.strip()
            continue
        if idx is None or all(set(c) <= set(":- ") for c in row):
            continue
        get = {c: (row[i] if i < len(row) else "") for c, i in idx.items()}
        fns.append(Fn(
            id=bare(get["id"]), phase=bare(get["phase"]), module=bare(get["module"]),
            deps=[bare(d) for d in get["deps"].split(",") if bare(d) not in ("", "-")],
            entry=get["entry"], acceptance=[bare(a) for a in get["acceptance"].split(";") if bare(a)],
            status=bare(get["status"]).lower(), header=header, row=line.strip(), order=len(fns)))
    return fns


def phase_key(f: Fn) -> tuple[int, int]:
    m = re.search(r"\d+", f.phase)
    return (int(m.group()) if m else 0, f.order)


def module_section(arch: str, module: str) -> str:
    """The ARCHITECTURE.md heading section that names this module, or ''."""
    if not module:
        return ""
    name = re.compile(rf"(?<![\w./-]){re.escape(module)}(?![\w-])")
    lines = arch.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"(#{1,6})\s+(.*)", line)
        if m and name.search(m.group(2).replace("`", "")):
            level = len(m.group(1))
            end = next((j for j in range(i + 1, len(lines))
                        if (h := re.match(r"(#{1,6})\s", lines[j])) and len(h.group(1)) <= level), len(lines))
            return "\n".join(lines[i:end]).strip()
    return ""


def module_paths(section: str, module: str) -> list[str]:
    """Paths from a `paths:` line in the module's section, else derived from its name."""
    for line in section.splitlines():
        m = PATHS_LINE.match(line)
        if m:
            items = re.findall(r"`([^`]+)`", m.group(1)) or [p.strip() for p in m.group(1).split(",")]
            if any(items):
                return [p for p in items if p]
    return [module] if "/" in module else [f"src/{module.replace('.', '/')}/"]


def unit_dirs(cmd: str) -> list[str]:
    return [t.rstrip("/") + "/" for t in cmd.split()
            if "/" in t and not t.startswith(("-", "<")) and not Path(t).suffix]


def acc_file(acceptance: str) -> str:
    return acceptance.split("::")[0]


def lock_path(repo: Path) -> Path:
    return Path(land_mod.lock_path(str(repo)))


def lock_holder(repo: Path) -> str:
    return read_text(lock_path(repo)).strip()


def acquire_lock(repo: Path, wait: float = 0.0, owner: str = "drive.py") -> bool:
    """land.py's lock: .overhaul/lock, exclusive create; a dead holder's lock is broken. False if kept past `wait` s."""
    return land_mod.acquire_lock(str(repo), wait, owner)


def release_lock(repo: Path) -> None:
    land_mod.release_lock(str(repo))


def load_config(repo: Path) -> dict:
    path = repo / "overhaul.toml"
    if not path.is_file():
        raise DriveError(f"no overhaul.toml in {repo}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise DriveError(f"overhaul.toml: {e}") from e
    cfg = {name: {**base, **raw.get(name, {})} for name, base in DEFAULTS.items()}
    cfg["commands"] = {k: str(v) for k, v in raw.get("commands", {}).items()}
    cfg["harness"] = raw.get("harness", {})
    return cfg


class Driver:
    """One run of the harness over one repo."""

    def __init__(self, repo: Path, cfg: dict):
        lim, har = cfg["limits"], cfg["harness"]
        self.repo, self.cfg = repo, cfg
        self.max_parallel = max(1, int(lim["max_parallel"]))
        self.max_calls = int(lim["builder_max_calls"])
        self.max_tokens = int(lim["brief_max_tokens"])
        self.retries = int(lim["retries_per_function"])
        self.timeout_s = float(lim["builder_timeout_min"]) * 60
        self.heartbeat_s = float(har.get("heartbeat_min", 10)) * 60
        self.main = str(har.get("main_branch", "main"))
        scripts = Path(str(har.get("scripts_dir") or HERE))
        self.scripts = scripts if scripts.is_absolute() else repo / scripts
        self.ov = repo / ".overhaul"
        self.state_path = repo / cfg["paths"]["state"]
        self.jobs: dict[str, Job] = {}
        self.land_queue: list[str] = []
        self.land_job: Job | None = None
        self.fns: dict[str, Fn] = {}
        self.order: list[Fn] = []
        self.arch = ""
        self.on_main: set[str] = set()
        self.protected: tuple[str, ...] = ()
        self.st: dict = {"cycle": 0, "functions": {}, "proven_history": []}
        self.last_cycle = self.last_dash = 0.0
        self.dash_due = False
        self.warned: set[str] = set()
        self.dirty = False  # state.json is behind self.st (land.py held the lock)

    # ---- state, shared with land.py under .overhaul/lock (see acquire_lock) ----
    def disk_state(self) -> dict:
        try:
            data = json.loads(read_text(self.state_path))
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        for key, empty in (("cycle", 0), ("functions", {}), ("proven_history", [])):
            data.setdefault(key, empty)
        return data

    def absorb(self, disk: dict) -> None:
        """Adopt proven marks and history that land.py or verdict.py wrote."""
        for fid, rec in disk["functions"].items():
            if rec.get("status") == "proven" and fid in self.st["functions"]:
                self.st["functions"][fid]["status"] = "proven"
        if len(disk["proven_history"]) > len(self.st["proven_history"]):
            self.st["proven_history"] = disk["proven_history"]
        self.st["cycle"] = max(self.st["cycle"], int(disk["cycle"] or 0))

    def save(self, wait: float = 0.0) -> bool:
        """Write state.json under .overhaul/lock. False, with `dirty` set, if land.py keeps the lock past `wait`."""
        if not acquire_lock(self.repo, wait):
            self.dirty = True
            return False
        try:  # only our own read-merge-write happens under the lock, never a subprocess
            self.absorb(self.disk_state())
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(f".{self.state_path.name}.drive.tmp")
            tmp.write_text(json.dumps(self.st, indent=1), encoding="utf-8")
            for _ in range(50):
                try:
                    os.replace(tmp, self.state_path)
                    self.dirty = False
                    return True
                except PermissionError:  # a reader holds it open on Windows
                    time.sleep(0.1)
            raise DriveError(f"cannot replace {self.state_path}")
        finally:
            release_lock(self.repo)

    def flush(self) -> None:
        """Write state.json now, waiting for land.py to release the lock."""
        if not self.save(LOCK_WAIT_S):
            raise DriveError(f"cannot write {self.state_path.name}: {lock_path(self.repo).as_posix()} is held by "
                             f"{lock_holder(self.repo) or '?'} (delete it if that process is gone)")

    def set(self, fid: str, **fields) -> None:
        self.st["functions"][fid].update(fields, updated=now_iso())
        self.save()

    def log(self, fn: str | None, event: str, detail: str = "", seconds: float = 0.0,
            tokens: int | None = None) -> None:
        line = {"t": now_iso(), "fn": fn, "event": event, "detail": detail,
                "seconds": round(seconds, 1), "tokens": tokens}
        with (self.ov / "log.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")

    @staticmethod
    def say(msg: str) -> None:
        print(f"drive: {msg}"[:240], flush=True)

    def read_main(self, rel: str) -> str | None:
        r = subprocess.run(["git", "show", f"{self.main}:{rel}"], cwd=self.repo,
                           stdin=subprocess.DEVNULL, capture_output=True)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None

    # ---- setup ----
    def load(self) -> None:
        if not (self.scripts / "land.py").is_file():
            raise DriveError(f"land.py not found in {self.scripts}")
        for d in ("wt", "briefs", "reviews", "runs"):
            (self.ov / d).mkdir(parents=True, exist_ok=True)
        rel, arch = self.cfg["paths"]["functions"], self.cfg["paths"]["architecture"]
        text = self.read_main(rel)
        if text is None:
            raise DriveError(f"{rel} is not committed on {self.main}")
        fns = parse_functions(text)
        if not fns:
            raise DriveError(f"{rel} has no function rows")
        ids = [f.id for f in fns]
        if len(set(ids)) != len(ids):
            raise DriveError(f"{rel} repeats ids: {sorted({i for i in ids if ids.count(i) > 1})}")
        self.order = sorted(fns, key=phase_key)
        self.fns = {f.id: f for f in fns}
        self.arch = self.read_main(arch) or ""
        self.on_main = set(git(self.repo, "ls-tree", "-r", "--name-only", self.main).splitlines())
        prot = {"tests/acceptance/", "overhaul.toml", rel, arch}
        prot |= {f"{Path(p).parent.as_posix()}/" for p in [rel, arch] + [acc_file(a) for f in fns for a in f.acceptance]}
        self.protected = tuple(sorted(p for p in prot if p != "./"))
        self.st = self.disk_state()
        recs = self.st["functions"]
        for fid in [k for k in recs if k not in self.fns]:
            del recs[fid]
        for f in self.order:
            rec = recs.setdefault(f.id, {"status": "planned", "attempts": 0, "last_reason": "",
                                         "branch": f"fn/{f.id}", "updated": now_iso()})
            if f.status == "proven":
                rec["status"] = "proven"
            elif rec["status"] in IN_FLIGHT:
                rec["status"] = "planned"  # a previous drive stopped mid-flight
        self.flush()

    def plan_problem(self, f: Fn) -> str:
        if not ID_RE.fullmatch(f.id):
            return f"id {f.id!r} cannot name a branch; use letters, digits, . _ -"
        unknown = [d for d in f.deps if d not in self.fns]
        if unknown:
            return f"deps not in FUNCTIONS.md: {', '.join(unknown)}"
        bad = [a for a in f.acceptance if "::" not in a or acc_file(a) not in self.on_main]
        if bad or not f.acceptance:
            return f"acceptance must be <file>::<test> committed on {self.main}: {', '.join(bad) or 'none'}"
        return ""

    def record_proven(self) -> None:
        n = sum(self.st["functions"][fid]["status"] == "proven" for fid in self.fns)
        hist = self.st["proven_history"]
        if not hist or hist[-1][1] != n:
            hist.append([now_iso(), n])
            self.save()

    # ---- the loop ----
    def run(self) -> int:
        for f in self.order:
            rec = self.st["functions"][f.id]
            if rec["status"] == "planned" and (problem := self.plan_problem(f)):
                self.park(f.id, problem)
        self.record_proven()
        try:
            self.cycle("start")
            while True:
                changed = self.reap()
                changed = self.pump_land() or changed
                changed = self.launch() or changed
                if self.dirty:
                    self.save()  # a write that had to wait for land.py's lock
                if not self.busy():
                    return self.finish()
                self.dash_due = self.dash_due or changed
                if time.monotonic() - self.last_cycle >= self.heartbeat_s:
                    self.cycle("heartbeat")
                elif self.dash_due and time.monotonic() - self.last_dash >= DASH_EVERY_S:
                    self.dash()
                time.sleep(POLL_S)
        except BaseException:
            self.abort()
            raise

    def busy(self) -> set[str]:
        return set(self.jobs) | set(self.land_queue) | ({self.land_job.fn} if self.land_job else set())

    def ready(self) -> list[Fn]:
        recs = self.st["functions"]
        proven = {fid for fid, r in recs.items() if r["status"] == "proven"}
        busy = self.busy()
        return [f for f in self.order if recs[f.id]["status"] == "planned" and f.id not in busy
                and recs[f.id]["attempts"] <= self.retries and all(d in proven for d in f.deps)]

    def launch(self) -> bool:
        launched = False
        for f in self.ready():
            if len(self.jobs) >= self.max_parallel:
                break
            self.start_build(f)
            launched = True
        return launched

    def reap(self) -> bool:
        """Handle finished or timed-out jobs; True if any finished."""
        now, done = time.monotonic(), False
        for job in [*self.jobs.values(), *([self.land_job] if self.land_job else [])]:
            if job.proc.poll() is None:
                if now < job.deadline:
                    continue
                kill_tree(job.proc)
                job.timed_out = True
            done = True
            if job.kind == "land":
                self.land_job = None
                self.after_land(job)
            else:
                del self.jobs[job.fn]
                (self.after_build if job.kind == "build" else self.after_review)(job)
        return done

    # ---- build ----
    def worker_cmd(self, kind: str) -> list[str]:
        har = self.cfg["harness"]
        custom = (har.get("reviewer_cmd") if kind == "review" else None) or har.get("worker_cmd")
        if custom:
            return [str(c) for c in custom] if isinstance(custom, list) else shlex.split(custom)
        exe = shutil.which("claude")
        if not exe:
            raise DriveError("claude CLI not on PATH")
        cmd = [exe, "-p", "--dangerously-skip-permissions", "--output-format", "json"]
        if supports_max_turns(exe):
            cmd += ["--max-turns", str(self.max_calls if kind == "build" else REVIEW_MAX_CALLS)]
        model = har.get("builder_model" if kind == "build" else "reviewer_model")
        return cmd + (["--model", str(model)] if model else [])

    def start_job(self, fid: str, kind: str, attempt: int, brief: Path, cwd: Path) -> None:
        out = self.ov / "runs" / f"{fid}-a{attempt}-{kind}.json"
        cmd = [*self.worker_cmd(kind), f"Read and do {brief.as_posix()}"]
        try:
            proc = spawn(cmd, cwd, out)
        except OSError as e:
            raise DriveError(f"cannot start the {kind} worker {cmd[0]}: {e}") from e
        start = time.monotonic()
        self.jobs[fid] = Job(fid, kind, proc, out, start, start + self.timeout_s, attempt)

    def prepare_worktree(self, fid: str) -> Path:
        """A clean worktree on fn/<id> at main (reset on a retry)."""
        wt, branch = self.ov / "wt" / fid, f"fn/{fid}"
        if (wt / ".git").is_file():
            try:
                top = git(wt, "rev-parse", "--show-toplevel").strip()
                if os.path.normcase(os.path.abspath(top)) == os.path.normcase(os.path.abspath(wt)):
                    git(wt, "checkout", "-q", "-f", "-B", branch, self.main)
                    git(wt, "clean", "-fdq")
                    return wt
            except DriveError:
                pass
        if wt.exists():
            shutil.rmtree(wt, onexc=force_remove)
        git(self.repo, "worktree", "prune")
        git(self.repo, "worktree", "add", "-q", "-f", "-B", branch, str(wt), self.main)
        return wt

    def allowed(self, f: Fn) -> str:
        paths = module_paths(module_section(self.arch, f.module), f.module)
        tests = unit_dirs(self.cfg["commands"].get("test", "")) or ["tests/unit/"]
        return "\n".join([f"- `{p}`" for p in paths] + [f"- `{t}` (unit tests for this module only)" for t in tests])

    def acceptance_text(self, f: Fn) -> str:
        parts = []
        for file in dict.fromkeys(acc_file(a) for a in f.acceptance):
            src = (self.read_main(file) or "(missing on main)").rstrip()
            parts.append(f"`{file}`\n`````{FENCE.get(Path(file).suffix, '')}\n{src}\n`````")
        return "\n\n".join(parts)

    def commands_text(self, f: Fn) -> str:
        lines = []
        for name, cmd in self.cfg["commands"].items():
            if "{test}" in cmd:
                lines += [f"- {name}: `{cmd.replace('{test}', a.split('::')[-1])}`" for a in f.acceptance]
            else:
                lines.append(f"- {name}: `{cmd}`")
        return "\n".join(lines) or "(overhaul.toml has no [commands])"

    def row_table(self, f: Fn) -> str:
        return f"{f.header}\n|{'---|' * len(cells(f.header))}\n{f.row}"

    def start_build(self, f: Fn) -> None:
        rec = self.st["functions"][f.id]
        attempt = rec["attempts"] + 1
        previous = (f"Attempt {attempt - 1} failed: {rec['last_reason']}\n"
                    "The worktree was reset to main. Fix that cause first."
                    if rec["last_reason"] else "None. This is the first attempt.")
        brief = render("build-brief.md", {
            "id": f.id, "attempt": str(attempt), "branch": f"fn/{f.id}",
            "worktree": (self.ov / "wt" / f.id).as_posix(), "row": self.row_table(f), "entry": f.entry,
            "module": f.module, "module_section": module_section(self.arch, f.module)
            or "(spec/ARCHITECTURE.md has no section naming this module)",
            "allowed_paths": self.allowed(f), "acceptance_source": self.acceptance_text(f),
            "commands": self.commands_text(f), "max_calls": str(self.max_calls), "previous": previous})
        tokens = len(brief) // 4
        if tokens > self.max_tokens:
            self.park(f.id, f"brief is {tokens} tokens, over brief_max_tokens {self.max_tokens}; "
                            "split the function or trim its acceptance test or ARCHITECTURE section")
            return
        wt = self.prepare_worktree(f.id)
        path = self.ov / "briefs" / f"{f.id}.md"
        path.write_text(brief, encoding="utf-8")
        self.start_job(f.id, "build", attempt, path, wt)
        self.set(f.id, status="building")
        self.log(f.id, "build_start", f"attempt {attempt}")
        self.say(f"build {f.id} attempt {attempt}")

    def after_build(self, job: Job) -> None:
        tokens, turns, text = read_result(job.out)
        flags = (" over-budget" if turns is not None and turns > self.max_calls else "") + \
                (" timeout" if job.timed_out else "")
        self.log(job.fn, "build_end", f"rc={job.proc.returncode} turns={turns}/{self.max_calls}{flags}",
                 time.monotonic() - job.started, tokens)
        problem = self.build_problem(job, text)
        if problem:
            self.fail(job.fn, problem)
        else:
            self.start_review(job.fn, job.attempt)

    def build_problem(self, job: Job, text: str) -> str:
        """Why this build cannot go to review, or ''. Commits leftover changes first."""
        if job.timed_out:
            return (f"builder timed out after {self.timeout_s / 60:g} min and was killed; "
                    "work in smaller steps and commit as you go")
        if job.proc.returncode != 0:
            why = last_line(text) or last_line(read_text(job.out.with_suffix(".err"))) or "no output"
            return f"builder exited rc={job.proc.returncode}: {why}"
        wt, branch = self.ov / "wt" / job.fn, f"fn/{job.fn}"
        try:
            if git(wt, "status", "--porcelain").strip():
                git(wt, "add", "-A")
                git(wt, "commit", "-q", "-m", f"{job.fn}: build attempt {job.attempt} (committed by drive.py)")
        except DriveError as e:
            return f"auto-commit of the builder's leftover changes failed: {e}"
        changed = git(self.repo, "diff", "--name-only", f"{self.main}...{branch}").splitlines()
        if not changed:
            return f"builder left no committed change on {branch}"
        hits = [p for p in changed if p.startswith(self.protected)]
        if hits:
            return f"changed protected files ({', '.join(hits[:5])}); never edit tests/acceptance, spec or overhaul.toml"
        return ""

    # ---- review ----
    def start_review(self, fid: str, attempt: int) -> None:
        f, branch = self.fns[fid], f"fn/{fid}"
        review_file = self.ov / "reviews" / f"{fid}.txt"
        review_file.unlink(missing_ok=True)
        brief = render("review-brief.md", {
            "id": fid, "branch": branch, "row": self.row_table(f), "allowed_paths": self.allowed(f),
            "acceptance_source": self.acceptance_text(f), "review_file": review_file.as_posix(),
            "diffstat": git(self.repo, "diff", "--stat", f"{self.main}...{branch}").rstrip(),
            "diff": git(self.repo, "diff", f"{self.main}...{branch}").rstrip(),
            "max_calls": str(REVIEW_MAX_CALLS)})
        tokens = len(brief) // 4
        if tokens > self.max_tokens:
            self.fail(fid, f"change too large to review ({tokens} tokens > brief_max_tokens "
                           f"{self.max_tokens}); keep the diff smaller")
            return
        path = self.ov / "briefs" / f"review-{fid}.md"
        path.write_text(brief, encoding="utf-8")
        head = git(self.repo, "rev-parse", branch).strip()
        self.start_job(fid, "review", attempt, path, self.ov / "wt" / fid)
        self.jobs[fid].head = head
        self.set(fid, status="review")

    def after_review(self, job: Job) -> None:
        fid = job.fn
        tokens, _, _ = read_result(job.out)
        git(self.ov / "wt" / fid, "checkout", "-q", "-f", "-B", f"fn/{fid}", job.head)  # drop reviewer edits
        git(self.ov / "wt" / fid, "clean", "-fdq")
        lines = read_text(self.ov / "reviews" / f"{fid}.txt").splitlines()
        verdict = lines[0].strip() if lines else ""
        notes = " / ".join(ln.strip() for ln in lines[1:] if ln.strip())
        if job.timed_out:
            verdict, notes = "FAIL", "reviewer timed out"
        elif verdict not in ("PASS", "FAIL"):
            verdict, notes = "FAIL", "review file missing or line 1 not exactly PASS or FAIL"
        secs = time.monotonic() - job.started
        if verdict == "PASS":
            self.log(fid, "review_pass", notes, secs, tokens)
            self.land_queue.append(fid)
            self.set(fid, status="landing")
        else:
            self.log(fid, "review_fail", notes, secs, tokens)
            self.fail(fid, f"review FAIL: {notes or 'no reason given'}")

    # ---- land ----
    def pump_land(self) -> bool:
        if self.land_job or not self.land_queue:
            return False
        fid = self.land_queue.pop(0)
        out = self.ov / "runs" / f"{fid}-a{self.st['functions'][fid]['attempts'] + 1}-land.out"
        # land.py takes .overhaul/lock itself for the whole land, so drive.py must hold nothing while it runs.
        cmd = [sys.executable, str(self.scripts / "land.py"), "--repo", str(self.repo), "--fn", fid,
               "--main", self.main]
        log = self.ov / "log.jsonl"
        log_at = log.stat().st_size if log.exists() else 0
        start = time.monotonic()
        self.land_job = Job(fid, "land", spawn(cmd, self.repo, out), out, start,
                            start + 2 * self.timeout_s, 0, log_at=log_at)
        return True

    def after_land(self, job: Job) -> None:
        fid, secs = job.fn, time.monotonic() - job.started
        out, err = read_text(job.out), read_text(job.out.with_suffix(".err"))
        if job.proc.returncode == 0 and not job.timed_out:
            row = next((f for f in parse_functions(self.read_main(self.cfg["paths"]["functions"]) or "")
                        if f.id == fid), None)
            if row and row.status != "proven":
                self.say(f"land.py exited 0 but {fid} is not proven in FUNCTIONS.md on {self.main}")
            self.set(fid, status="proven", last_reason="")
            self.say(f"landed {fid}")
            self.record_proven()
            self.cycle(f"landed {fid}")
            return
        reason = "land.py timed out" if job.timed_out else (
            last_line(out) or last_line(err) or f"land.py exit {job.proc.returncode}")
        if not self.land_logged(fid, job.log_at):  # killed or crashed before land.py wrote its own land_fail line
            self.log(fid, "land_fail", reason, secs)
        if GREEN_BEFORE in reason:
            self.park(fid, f"land: {reason}")
        else:
            self.fail(fid, f"land: {reason}")

    def land_logged(self, fid: str, since: int) -> bool:
        """Whether land.py itself appended a land_ok or land_fail line for `fid` after byte offset `since`."""
        try:
            with (self.ov / "log.jsonl").open("rb") as fh:
                fh.seek(since)
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return False
        for line in tail.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("fn") == fid and rec.get("event") in ("land_ok", "land_fail"):
                return True
        return False

    # ---- outcomes ----
    def fail(self, fid: str, reason: str) -> None:
        attempts = self.st["functions"][fid]["attempts"] + 1
        self.set(fid, status="planned", attempts=attempts, last_reason=reason[:REASON_MAX])
        self.say(f"{fid} attempt {attempts} failed: {reason}")
        if attempts > self.retries:
            self.park(fid, reason)

    def park(self, fid: str, reason: str) -> None:
        self.set(fid, status="needs-plan", last_reason=reason[:REASON_MAX])
        self.log(fid, "needs_plan", reason)
        self.say(f"needs-plan {fid}: {reason}")

    def summary(self) -> str:
        n = Counter(self.st["functions"][fid]["status"] for fid in self.fns)
        rest = " ".join(f"{s} {n[s]}" for s in (*IN_FLIGHT, "needs-plan") if n[s])
        return f"proven {n['proven']}/{len(self.fns)}" + (f" {rest}" if rest else "")

    def script(self, name: str) -> str:
        """Run verdict.py or dash.py; return its last output line ('' if absent)."""
        path = self.scripts / name
        if not path.is_file():
            if name not in self.warned:
                self.warned.add(name)
                self.say(f"{name} not in {self.scripts}; skipped")
            return ""
        try:
            r = run([sys.executable, str(path), "--repo", str(self.repo)], self.repo, timeout=300)
        except subprocess.TimeoutExpired:
            return f"{name} timed out"
        return last_line(r.stdout) or (f"{name} rc={r.returncode}: {last_line(r.stderr)}" if r.returncode else "")

    def dash(self) -> None:
        self.script("dash.py")
        self.last_dash, self.dash_due = time.monotonic(), False

    def cycle(self, why: str) -> None:
        """A heartbeat: count it, log it, run verdict.py and dash.py."""
        self.st["cycle"] += 1
        self.save()
        self.log(None, "cycle", f"{self.st['cycle']} {why}: {self.summary()}")
        line = self.script("verdict.py")
        if line:
            self.save()
            self.log(None, "verdict", line)
            self.say(line)
        self.dash()
        self.last_cycle = time.monotonic()

    def finish(self) -> int:
        recs = self.st["functions"]
        parked = [fid for fid in self.fns if recs[fid]["status"] == "needs-plan"]
        blocked = [fid for fid in self.fns if recs[fid]["status"] not in ("proven", "needs-plan")]
        code = 3 if parked or blocked else 0
        self.cycle("exit")
        if self.dirty:
            self.flush()
        for fid in parked:
            self.say(f"needs-plan {fid}: {recs[fid]['last_reason']}")
        if blocked:
            self.say(f"blocked on unproven deps: {', '.join(blocked)}")
        print(f"DRIVE EXIT {code} {self.summary()}", flush=True)
        return code

    def abort(self) -> None:
        """Kill every worker tree this run started; their functions go back to planned."""
        jobs = [*self.jobs.values(), *([self.land_job] if self.land_job else [])]
        for job in jobs:
            kill_tree(job.proc)
        for fid in [*(j.fn for j in jobs), *self.land_queue]:
            rec = self.st["functions"].get(fid)
            if rec and rec["status"] in IN_FLIGHT:
                rec["status"] = "planned"
        self.jobs.clear()
        self.land_queue.clear()
        self.land_job = None
        try:
            self.save(5.0)
        except (OSError, DriveError):
            pass
        if jobs:
            self.say(f"stopped; killed {len(jobs)} worker tree(s)")

    # ---- manager commands ----
    def reset(self, fid: str) -> None:
        self.set(fid, status="planned", attempts=0, last_reason="")
        if self.dirty:
            self.flush()
        self.log(fid, "cycle", "reset to planned, attempts 0")
        self.say(f"reset {fid}")

    def mark_proven(self, fid: str, evidence: Path) -> None:
        if not evidence.is_file() or not read_text(evidence).strip():
            raise DriveError(f"evidence file missing or empty: {evidence}")
        self.set(fid, status="proven", last_reason="")
        self.log(fid, "land_ok", f"marked proven by the manager; evidence {evidence.as_posix()}")
        self.record_proven()
        if self.dirty:
            self.flush()
        self.say(f"marked proven {fid}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Overhaul v2 build harness.")
    ap.add_argument("--repo", default=".", help="rebuild repo root (default: cwd)")
    ap.add_argument("--reset", metavar="ID", help="set ID to planned with attempts 0, then exit")
    ap.add_argument("--mark-proven", metavar="ID", help="mark ID proven (needs --evidence), then exit")
    ap.add_argument("--evidence", metavar="FILE", help="blind review file backing --mark-proven")
    args = ap.parse_args(argv)
    repo = Path(os.path.abspath(args.repo))
    try:
        drv = Driver(repo, load_config(repo))
        drv.load()
        for fid in filter(None, (args.reset, args.mark_proven)):
            if fid not in drv.fns:
                raise DriveError(f"{fid} is not in {drv.cfg['paths']['functions']}")
        if args.reset:
            drv.reset(args.reset)
            return 0
        if args.mark_proven:
            if not args.evidence:
                raise DriveError("--mark-proven needs --evidence FILE")
            drv.mark_proven(args.mark_proven, Path(os.path.abspath(args.evidence)))
            return 0
        return drv.run()
    except DriveError as e:
        print(f"drive: error: {e}", flush=True)
    except KeyboardInterrupt:
        print("drive: interrupted", flush=True)
    except Exception:  # an unexpected bug: show where
        traceback.print_exc()
    return 1


if __name__ == "__main__":
    sys.exit(main())
