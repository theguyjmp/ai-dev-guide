#!/usr/bin/env python3
"""Overhaul v2 harness: build, review and land every function in spec/FUNCTIONS.md.

A READY function (planned, deps proven, retries left, no in-flight function whose files overlap its own) gets a
worktree at .overhaul/wt/<id> on fn/<id> from main, a brief from templates/, and a `claude -p`
builder. A finished build gets a blind reviewer; a PASS goes to land.py. A failure retries from main
with its reason in the brief, then parks the function as needs-plan for the manager. A land.py
"merge conflict" is not an attempt: a conflict-fix builder pass merges main into the same worktree
and branch (templates/conflict-brief.md, [limits] conflict_retries per attempt), then the function
is reviewed and landed again.

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
import fnmatch
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
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import land as land_mod  # noqa: E402  the lock protocol is land.py's

HERE = Path(os.path.abspath(__file__)).parent
TEMPLATES = HERE / "templates"
STANDARD = HERE.parent / "references" / "quality-standard.md"
AUDIT_BRIEF = TEMPLATES / "audit-brief.md"
CHECKLIST = HERE.parent / "references" / "checklist.md"
ITEM_LINE = re.compile(r"- ([A-Z]\d+) \[(P0|P1|nit)\] ([^|]*)\|([^|]+)\|(.+)")
BLOCKING = ("P0", "P1")
NITS_MAX = 5  # nits per pass: logged, never dispatched
POLISH_ROUNDS_MAX = 2  # v3: at most 2 checklist fix rounds, whatever [limits] polish_rounds says
BASE_RULES = (2, 3, 8, 10, 13, 16, 17, 18, 19, 20, 21, 24, 25, 26, 27)
RULE_TRIGGERS = (  # words in a function's row, module section or acceptance test -> the rules they bring in
    (r"http|server|web|socket|request|port|url", (33, 35)),
    (r"store|json|file|save|write|persist|database|\bdb\b|record", (23, 29, 30, 31, 32, 33, 34)),
    (r"lock", (34,)),
    (r"csv|export", (31, 36)),
    (r"\bcli\b|argparse|command|argv", (5, 6)),
    (r"total|tax|price|sum|report", (9, 11)),
)
COLUMNS = ("id", "phase", "module", "deps", "entry", "acceptance", "status")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
PATHS_LINE = re.compile(r"\W*(?:paths?|files)\W*?[:=]\s*(.+)", re.I)
SHARED_LINE = re.compile(r"\W*shared\W*?[:=]\s*(.+)", re.I)  # files every function may touch; never a lock
FILES_COLUMN = "files"  # optional FUNCTIONS.md column: the function's own files, the unit of the build lock
IN_FLIGHT = ("building", "review", "landing")
POLISH_KINDS = ("polish_audit", "polish_verify", "polish_fix", "polish_review", "polish_land")
GREEN_BEFORE = "acceptance already green before change"
CONFLICT = "merge conflict"  # land.py: "merge conflict merging <main> into fn/<id>"
# A review fix the builder cannot make: the reviewer's PLAN-GAP line, or "outside (the) allowed paths" on a diff
# that stayed inside them. Such a FAIL goes to needs-plan; another builder pass would only re-learn it.
PLAN_GAP = re.compile(r"\bPLAN-GAP\b")
OUTSIDE = re.compile(r"\boutside\s+(?:the\s+|my\s+|its\s+|your\s+)?allowed\s+paths\b", re.I)
FENCE = {".py": "python", ".rs": "rust", ".js": "javascript", ".ts": "typescript"}
REVIEW_MAX_CALLS = 20
REASON_MAX = 1500
POLL_S = 0.25
DASH_EVERY_S = 15
LOCK_WAIT_S = 120.0  # a write that must happen now waits this long for land.py's lock
DEFAULTS = {
    "limits": {"builder_max_calls": 60, "brief_max_tokens": 40000,
               "retries_per_function": 1, "conflict_retries": 2, "builder_timeout_min": 30,
               "polish_rounds": 3},
    "paths": {"functions": "spec/FUNCTIONS.md", "architecture": "spec/ARCHITECTURE.md",
              "state": ".overhaul/state.json"},
    "harness": {"builder_model": "opus", "review_model": "sonnet"},
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
    files: list[str] = field(default_factory=list)


@dataclass
class Job:
    fn: str
    kind: str  # build | conflict | review | land, or one of POLISH_KINDS
    proc: subprocess.Popen
    out: Path
    started: float
    deadline: float
    attempt: int
    head: str = ""
    timed_out: bool = False
    log_at: int = 0  # size of log.jsonl when a land job started
    base: str = ""  # a conflict pass: the sha of main when it started (the branch must contain it)
    model: str = ""  # the --model a claude worker got; "custom" for a worker_cmd without one


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


@lru_cache(maxsize=1)
def standard_rules() -> dict[int, str]:
    """Rule number -> one-line text, from references/quality-standard.md ({} if the file is missing)."""
    rules: dict[int, str] = {}
    num = 0
    for line in read_text(STANDARD).splitlines():
        m = re.match(r"- Q(\d+)\. ", line)
        if m:
            num = int(m.group(1))
            rules[num] = line[2:].strip()
        elif num and line.startswith("  ") and line.strip():
            rules[num] += " " + line.strip()
        else:
            num = 0
    return rules


def wanted_rules(text: str) -> set[int]:
    """The quality-standard rule numbers that apply to a function: a base set plus keyword triggers."""
    want = set(BASE_RULES)
    for pattern, nums in RULE_TRIGGERS:
        if re.search(pattern, text, re.I):
            want.update(nums)
    return want


def relevant_rules(text: str) -> str:
    """The quality-standard rules a function's builder and reviewer must cite."""
    rules = standard_rules()
    if not rules:
        return f"(quality standard missing: {STANDARD.as_posix()})"
    return "\n".join(f"- {rules[n]}" for n in sorted(wanted_rules(text)) if n in rules)


@dataclass(frozen=True)
class Item:
    id: str
    severity: str  # P0 | P1 | nit
    rules: tuple[int, ...]
    question: str
    decide: str


@lru_cache(maxsize=1)
def checklist() -> tuple[str, dict[str, Item]]:
    """(version, id -> Item) from references/checklist.md."""
    text = read_text(CHECKLIST)
    m = re.search(r"^checklist-version:\s*(\S+)", text, re.M)
    items = {}
    for line in text.splitlines():
        it = ITEM_LINE.match(line)
        if it:
            nums = tuple(int(n) for n in re.findall(r"Q(\d+)", it[3]))
            items[it[1]] = Item(it[1], it[2], nums, it[4].strip(), it[5].strip())
    return (m[1] if m else "v?"), items


def applicable_items(text: str | None) -> list[Item]:
    """Contract items always, plus the items whose rules apply to the text; None means the whole checklist."""
    items = list(checklist()[1].values())
    if text is None:
        return items
    want = wanted_rules(text)
    return [i for i in items if not i.rules or want.intersection(i.rules)]


def checklist_text(items: list[Item]) -> str:
    lines = [f"- {i.id} [{i.severity}] {i.question} Decide: {i.decide}" for i in items]
    return "\n".join(lines) or f"(checklist missing: {CHECKLIST.as_posix()})"


def read_json(path: Path) -> object:
    try:
        return json.loads(read_text(path))
    except ValueError:
        return None


def checklist_nos(raw: object) -> list[dict] | None:
    """The NO answers of a review or polish result, each with its checklist severity; None if malformed.

    A review result is {"items": [...]}; a polish result is the list itself (an entry is a NO unless it says
    otherwise). An unknown id is P1: blocking, and verified like any other. NEW-RULE entries are not NOs."""
    entries = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        return None
    known, nos = checklist()[1], []
    for e in entries:
        cid = str(e.get("id") or e.get("rule") or "?").strip()
        if str(e.get("answer", "no")).strip().lower() != "no" or cid == "NEW-RULE":
            continue
        nos.append({**e, "id": cid, "severity": known[cid].severity if cid in known else "P1"})
    return nos


def where(n: dict) -> str:
    return f"{n.get('file', '?')}:{n.get('line', '?')}"


def format_nos(nos: list[dict]) -> str:
    return "\n".join(f"- [{n['id']} {n['severity']}] `{where(n)}` {n.get('evidence') or n.get('issue') or ''}"
                     + (f" -> {n['fix']}" if n.get("fix") else "") for n in nos)


def changed_lines(cwd: Path, old: str, new: str) -> dict[str, set[int]] | None:
    """New-side line numbers each file changed between two commits; None when git cannot diff them."""
    try:
        diff = git(cwd, "diff", "-U0", "--no-color", old, new)
    except DriveError:
        return None
    out: dict[str, set[int]] = {}
    name = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            name = line[6:] if line.startswith("+++ b/") else ""
            out.setdefault(name, set())
        elif line.startswith("@@") and name:
            m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start, count = int(m[1]), int(m[2] if m[2] is not None else 1)
                out[name].update(range(start, start + max(count, 1)))
    return out


def in_changed(n: dict, changed: dict[str, set[int]]) -> bool:
    """Whether a NO points at code that changed: its file changed and its line (when given) is a changed one."""
    lines = changed.get(str(n.get("file", "")).replace("\\", "/").removeprefix("./"))
    if lines is None:
        return False
    try:
        return int(n.get("line")) in lines
    except (TypeError, ValueError):
        return True


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
    names_files: int | None = None
    header = ""
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            idx = None
            continue
        row = cells(line)
        names = [c.lower() for c in row]
        if all(c in names for c in COLUMNS):
            idx, header = {c: names.index(c) for c in COLUMNS}, line.strip()
            names_files = names.index(FILES_COLUMN) if FILES_COLUMN in names else None
            continue
        if idx is None or all(set(c) <= set(":- ") for c in row):
            continue
        get = {c: (row[i] if i < len(row) else "") for c, i in idx.items()}
        files = row[names_files] if names_files is not None and names_files < len(row) else ""
        fns.append(Fn(
            id=bare(get["id"]), phase=bare(get["phase"]), module=bare(get["module"]),
            deps=[bare(d) for d in get["deps"].split(",") if bare(d) not in ("", "-")],
            entry=get["entry"], acceptance=[bare(a) for a in get["acceptance"].split(";") if bare(a)],
            status=bare(get["status"]).lower(), header=header, row=line.strip(), order=len(fns),
            files=[bare(x) for x in re.split(r"[,;]", files) if bare(x) not in ("", "-")]))
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


def section_items(section: str, pattern: re.Pattern) -> list[str]:
    for line in section.splitlines():
        m = pattern.match(line)
        if m:
            items = re.findall(r"`([^`]+)`", m.group(1)) or [p.strip() for p in m.group(1).split(",")]
            return [p for p in items if p]
    return []


def module_shared(section: str) -> list[str]:
    """Files from a `shared:` line in the module's section: an export list or registry every function touches."""
    return section_items(section, SHARED_LINE)


def _stem(p: str) -> str:
    """The literal part of a path pattern before its first glob character."""
    return re.split(r"[*?\[]", p, maxsplit=1)[0]


def paths_overlap(a: list[str], b: list[str]) -> bool:
    """Whether two path sets (files, dirs ending in /, globs) can name one file. A glob is compared by its literal
    stem, so the answer errs towards overlap."""
    def one(p: str, q: str) -> bool:
        if p == q:
            return True
        if any(c in p + q for c in "*?["):
            sp, sq = _stem(p), _stem(q)
            return sp.startswith(sq) or sq.startswith(sp) or fnmatch.fnmatchcase(q, p) or fnmatch.fnmatchcase(p, q)
        return (p.endswith("/") and q.startswith(p)) or (q.endswith("/") and p.startswith(q))
    return any(one(p, q) for p in a for q in b)


def in_paths(path: str, allowed: list[str]) -> bool:
    """Whether a repo-relative file is under one of the allowed paths (a dir ends in /, * ? [ make a glob)."""
    return any(fnmatch.fnmatchcase(path, p) if any(c in p for c in "*?[") else
               path.startswith(p) if p.endswith("/") else path == p for p in allowed)


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
    return cfg


class Driver:
    """One run of the harness over one repo."""

    def __init__(self, repo: Path, cfg: dict):
        lim, har = cfg["limits"], cfg["harness"]
        self.repo, self.cfg = repo, cfg
        # [harness] max_parallel, else the older [limits] key, else 6
        self.max_parallel = max(1, int(har.get("max_parallel") or lim.get("max_parallel") or 6))
        self.max_calls = int(lim["builder_max_calls"])
        self.max_tokens = int(lim["brief_max_tokens"])
        self.retries = int(lim["retries_per_function"])
        self.conflict_retries = max(0, int(lim["conflict_retries"]))
        self.timeout_s = float(lim["builder_timeout_min"]) * 60
        self.heartbeat_s = float(har.get("heartbeat_min", 10)) * 60
        self.main = str(har.get("main_branch", "main"))
        self.builder_model = str(har.get("builder_model") or "")  # also used by conflict passes
        self.review_model = str(har.get("review_model") or "")
        self.audit_model = str(har.get("audit_model") or "opus")  # the final judge's model: one bar
        self.polish_rounds = min(int(lim["polish_rounds"]), POLISH_ROUNDS_MAX)
        self.verify_model = str(har.get("verify_model") or self.review_model)  # a cheap model is fine
        self.verify_meta: dict[str, dict] = {}  # fid -> the findings a verifier is checking, and their context
        self.polish_queue: list[tuple[str, list[dict]]] = []  # (module, issues) fix passes waiting for a slot
        self.polish_lands: list[str] = []
        self.polish_meta: dict[str, tuple[str, str]] = {}  # fix fid -> (module, issues text)
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
        self.cycle_due = ""  # a heartbeat a land asked for, run once the loop has launched what it unblocked

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
            rec = recs.setdefault(f.id, {"status": "planned", "attempts": 0, "conflicts": 0, "last_reason": "",
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
                changed = self.polish_step() or changed
                if self.cycle_due:
                    why, self.cycle_due = self.cycle_due, ""
                    self.cycle(why)
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

    def claims(self, f: Fn) -> list[str]:
        """The files a function's build locks: its FUNCTIONS.md `files` cell, else its whole module's paths.
        The module's `shared:` files are left out: a clash there goes to the conflict-fix pass."""
        section = module_section(self.arch, f.module)
        shared = set(module_shared(section))
        return [p for p in (f.files or module_paths(section, f.module)) if p not in shared]

    def ready(self) -> list[Fn]:
        """Functions to launch now: planned, deps proven, retries left, and no file overlap with a function in
        flight (building, in review or landing) or with one launched before it in this pass.

        Two builders that edit one file fail to land with a merge conflict, so a function waits while an in-flight
        function's claimed files overlap its own. A plan with a `files` column runs one module's functions at once;
        without it a function claims its module's paths, one function per module at a time."""
        recs = self.st["functions"]
        proven = {fid for fid, r in recs.items() if r["status"] == "proven"}
        busy = self.busy()
        taken = [self.claims(self.fns[fid]) for fid in busy if fid in self.fns]
        ready: list[Fn] = []
        for f in self.order:
            if (recs[f.id]["status"] != "planned" or f.id in busy or recs[f.id]["attempts"] > self.retries
                    or not all(d in proven for d in f.deps)):
                continue
            mine = self.claims(f)
            if any(paths_overlap(mine, other) for other in taken):
                continue
            taken.append(mine)
            ready.append(f)
        return ready

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
            if job.kind in POLISH_KINDS:
                self.reap_polish(job)
            elif job.kind == "land":
                self.land_job = None
                self.after_land(job)
            else:
                del self.jobs[job.fn]
                {"build": self.after_build, "conflict": self.after_conflict,
                 "verify": self.after_verify}.get(job.kind, self.after_review)(job)
        return done

    # ---- build ----
    def custom_cmd(self, kind: str) -> list[str]:
        har = self.cfg["harness"]
        custom = (har.get("reviewer_cmd") if kind in ("review", "verify") else None) or har.get("worker_cmd")
        if not custom:
            return []
        return [str(c) for c in custom] if isinstance(custom, list) else shlex.split(custom)

    def worker_cmd(self, kind: str) -> list[str]:
        """The worker command for kind build | conflict | review, without the prompt."""
        if custom := self.custom_cmd(kind):
            return custom
        exe = shutil.which("claude")
        if not exe:
            raise DriveError("claude CLI not on PATH")
        cmd = [exe, "-p", "--dangerously-skip-permissions", "--output-format", "json"]
        if supports_max_turns(exe):
            cmd += ["--max-turns", str(REVIEW_MAX_CALLS if kind in ("review", "verify") else self.max_calls)]
        model = {"review": self.review_model, "verify": self.verify_model}.get(kind, self.builder_model)
        return cmd + (["--model", model] if model else [])

    def start_job(self, fid: str, kind: str, attempt: int, brief: Path, cwd: Path, tag: str = "") -> None:
        out = self.ov / "runs" / f"{fid}-a{attempt}-{tag or kind}.json"
        base = self.worker_cmd(kind)
        cmd = [*base, f"Read and do {brief.as_posix()}"]
        try:
            proc = spawn(cmd, cwd, out)
        except OSError as e:
            raise DriveError(f"cannot start the {kind} worker {cmd[0]}: {e}") from e
        model = base[base.index("--model") + 1] if "--model" in base[:-1] else (
            "custom" if self.custom_cmd(kind) else "default")
        start = time.monotonic()
        self.jobs[fid] = Job(fid, kind, proc, out, start, start + self.timeout_s, attempt, model=model)

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

    def allowed_lists(self, f: Fn) -> tuple[list[str], list[str]]:
        section = module_section(self.arch, f.module)
        paths = f.files + module_shared(section) if f.files else module_paths(section, f.module)
        return list(dict.fromkeys(paths)), unit_dirs(self.cfg["commands"].get("test", "")) or ["tests/unit/"]

    def allowed(self, f: Fn) -> str:
        paths, tests = self.allowed_lists(f)
        return "\n".join([f"- `{p}`" for p in paths] + [f"- `{t}` (unit tests for this module only)" for t in tests])

    def plan_gap(self, fid: str, notes: str) -> bool:
        """Whether a review FAIL asks for a change only the plan can allow (see PLAN_GAP)."""
        if PLAN_GAP.search(notes):
            return True
        if not OUTSIDE.search(notes):
            return False
        allowed = [p for group in self.allowed_lists(self.fns[fid]) for p in group]
        changed = git(self.repo, "diff", "--name-only", f"{self.main}...fn/{fid}").splitlines()
        return all(in_paths(p, allowed) for p in changed)  # else the builder strayed: an ordinary FAIL

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
            "policies": self.policies(), "commands": self.commands_text(f), "max_calls": str(self.max_calls),
            "previous": previous, **self.standard_values(f)})
        tokens = len(brief) // 4
        if tokens > self.max_tokens:
            self.park(f.id, f"brief is {tokens} tokens, over brief_max_tokens {self.max_tokens}; "
                            "split the function or trim its acceptance test or ARCHITECTURE section")
            return
        wt = self.prepare_worktree(f.id)
        path = self.ov / "briefs" / f"{f.id}.md"
        path.write_text(brief, encoding="utf-8")
        self.start_job(f.id, "build", attempt, path, wt)
        self.set(f.id, status="building", conflicts=0)  # a fresh branch from main: a fresh conflict budget
        self.log(f.id, "build_start", f"attempt {attempt} model={self.jobs[f.id].model}")
        self.say(f"build {f.id} attempt {attempt}")

    def after_build(self, job: Job) -> None:
        tokens, turns, text = read_result(job.out)
        flags = (" over-budget" if turns is not None and turns > self.max_calls else "") + \
                (" timeout" if job.timed_out else "")
        self.log(job.fn, "build_end", f"rc={job.proc.returncode} turns={turns}/{self.max_calls}{flags} "
                 f"model={job.model}", time.monotonic() - job.started, tokens)
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
                git(wt, "commit", "-q", "-m", f"{job.fn}: {job.kind} attempt {job.attempt} (committed by drive.py)")
        except DriveError as e:
            return f"auto-commit of the builder's leftover changes failed: {e}"
        changed = git(self.repo, "diff", "--name-only", f"{self.main}...{branch}").splitlines()
        if not changed:
            return f"builder left no committed change on {branch}"
        hits = [p for p in changed if p.startswith(self.protected)]
        if hits:
            return f"changed protected files ({', '.join(hits[:5])}); never edit tests/acceptance, spec or overhaul.toml"
        return self.cycles_problem(wt)

    def cycles_problem(self, wt: Path) -> str:
        """The builder gate's cycles check, run on the branch before review: land.py runs it again on the merged
        tree, so a cycle or a broken Public line found only there costs a review and a land."""
        cmd = self.cfg["commands"].get("cycles")
        if not cmd:
            return ""
        try:
            p = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=self.timeout_s, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return f"cycles check timed out in the worktree: `{cmd}`"
        if p.returncode == 0:
            return ""
        lines = [ln.strip() for ln in (p.stdout + p.stderr).splitlines() if ln.strip()]
        shown = lines[:4] + (lines[-1:] if len(lines) > 4 else [])
        return (f"cycles failed (exit {p.returncode}) in the worktree before review: `{cmd}`: " + " / ".join(shown)
                + ". Run it before you hand back; every name on your module's Public line must exist")

    # ---- conflict: main moved under a branch that land.py could not merge ----
    def start_conflict(self, fid: str, reason: str) -> bool:
        """Start a conflict-fix builder pass on fn/<id>'s own worktree. False once conflict_retries is spent
        (or the worktree is unusable): the caller then counts the land failure as an attempt."""
        rec = self.st["functions"][fid]
        n = int(rec.get("conflicts") or 0) + 1
        if n > self.conflict_retries:
            return False
        f, branch, wt = self.fns[fid], f"fn/{fid}", self.ov / "wt" / fid
        try:
            git(wt, "checkout", "-q", "-f", branch)  # keep the reviewed work; never reset to main here
            git(wt, "clean", "-fdq")
            base = git(self.repo, "rev-parse", self.main).strip()
        except DriveError as e:
            self.say(f"{fid}: cannot start a conflict pass ({e})")
            return False
        brief = render("conflict-brief.md", {
            "id": fid, "pass": str(n), "branch": branch, "worktree": wt.as_posix(), "main": self.main,
            "module": f.module, "reason": reason, "row": self.row_table(f), "allowed_paths": self.allowed(f),
            "commands": self.commands_text(f), "max_calls": str(self.max_calls)})
        path = self.ov / "briefs" / f"conflict-{fid}-{n}.md"
        path.write_text(brief, encoding="utf-8")
        self.start_job(fid, "conflict", rec["attempts"] + 1, path, wt, tag=f"conflict{n}")
        self.jobs[fid].base = base
        self.set(fid, status="building", conflicts=n)
        self.log(fid, "conflict_start", f"pass {n}/{self.conflict_retries}, not an attempt: {reason} "
                 f"model={self.jobs[fid].model}")
        self.say(f"conflict pass {n}/{self.conflict_retries} for {fid}: {reason}")
        return True

    def after_conflict(self, job: Job) -> None:
        tokens, turns, text = read_result(job.out)
        flags = (" over-budget" if turns is not None and turns > self.max_calls else "") + \
                (" timeout" if job.timed_out else "")
        self.log(job.fn, "conflict_end", f"rc={job.proc.returncode} turns={turns}/{self.max_calls}{flags} "
                 f"model={job.model}", time.monotonic() - job.started, tokens)
        problem = self.conflict_problem(job, text)
        if problem:  # a conflict pass that fails is a failed attempt: the retry rebuilds from the new main
            self.fail(job.fn, f"conflict pass {self.st['functions'][job.fn].get('conflicts', 0)}: {problem}")
        else:
            self.start_review(job.fn, job.attempt)

    def conflict_problem(self, job: Job, text: str) -> str:
        """Why this conflict pass cannot go back to review, or ''. Commits a merge the builder left staged."""
        wt, branch = self.ov / "wt" / job.fn, f"fn/{job.fn}"
        try:
            if not job.timed_out and job.proc.returncode == 0:  # before build_problem stages everything
                left = git(wt, "diff", "--name-only", "--diff-filter=U").split()
                if left:
                    return f"conflicts left unresolved in {', '.join(left[:5])}"
            problem = self.build_problem(job, text)
            if problem:
                return problem
            base = job.base or self.main  # main as it was when the pass began; it may have moved on since
            if run(["git", "merge-base", "--is-ancestor", base, branch], self.repo).returncode != 0:
                return f"{branch} does not contain {self.main} yet ({base[:12]}); run git merge {self.main} and commit"
        except DriveError as e:
            return f"git failed after the conflict pass: {e}"
        return ""

    # ---- review ----
    def policies(self) -> str:
        """The `## Policies` section of ARCHITECTURE.md: concurrency, persistence and error owners."""
        return (module_section(self.arch, "Policies")
                or "(spec/ARCHITECTURE.md declares no Policies section; re-run plan_validate.py)")

    def standard_values(self, f: Fn) -> dict[str, str]:
        """The quality-standard placeholders of a build or review brief for function f."""
        text = "\n".join((self.row_table(f), module_section(self.arch, f.module), self.acceptance_text(f)))
        return {"standard_path": STANDARD.as_posix(), "standard": relevant_rules(text)}

    def round_rule(self, fid: str) -> str:
        prev = self.st["functions"][fid].get("review")
        if not prev:
            return ("Round 1: every NO you list may block once verified. List them all now; a later round may "
                    "raise only items still NO or regressions in changed code.")
        return (f"Round {prev.get('round', 1) + 1}. The previous round's verified blocking NOs were "
                f"{', '.join(prev.get('ids') or []) or 'none'}. Answer every item, but a NO now blocks only when "
                "(a) it is one of those items, still NO, or (b) it points at a line this attempt changed (a "
                "regression). Any other NO on unchanged code is logged as review_overreach and does not block.")

    def start_review(self, fid: str, attempt: int) -> None:
        f, branch = self.fns[fid], f"fn/{fid}"
        review_file = self.ov / "reviews" / f"{fid}.json"
        review_file.parent.mkdir(parents=True, exist_ok=True)
        review_file.unlink(missing_ok=True)
        values = self.standard_values(f)
        scope = "\n".join((self.row_table(f), module_section(self.arch, f.module), self.acceptance_text(f)))
        brief = render("review-brief.md", {
            "id": fid, "branch": branch, "row": self.row_table(f), "allowed_paths": self.allowed(f),
            "acceptance_source": self.acceptance_text(f), "review_file": review_file.as_posix(),
            "policies": self.policies(), **values, "checklist_path": CHECKLIST.as_posix(),
            "checklist_version": checklist()[0], "checklist": checklist_text(applicable_items(scope)),
            "round_rule": self.round_rule(fid),
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

    def reset_wt(self, fid: str, head: str) -> None:
        git(self.ov / "wt" / fid, "checkout", "-q", "-f", "-B", f"fn/{fid}", head)  # drop reviewer edits
        git(self.ov / "wt" / fid, "clean", "-fdq")

    def log_nits(self, fid: str | None, nits: list[dict], where_from: str) -> None:
        if nits:
            shown = nits[:NITS_MAX]
            more = f" (+{len(nits) - NITS_MAX} over the cap of {NITS_MAX}, dropped)" if len(nits) > NITS_MAX else ""
            self.log(fid, "review_nits", f"{where_from}: {len(shown)} nits{more}: "
                     + " / ".join(f"{n['id']} {where(n)}" for n in shown))

    def round_filter(self, fid: str, blocking: list[dict], head: str) -> list[dict]:
        """The round rule: on a re-review only still-NO items or regressions in changed code may block."""
        prev = self.st["functions"][fid].get("review")
        if not prev:
            return blocking
        changed = changed_lines(self.repo, prev.get("head", ""), head) if prev.get("head") else None
        ids = set(prev.get("ids") or [])
        keep = [n for n in blocking if n["id"] in ids or changed is None or in_changed(n, changed)]
        over = [n for n in blocking if n not in keep]
        if over:
            self.log(fid, "review_overreach", f"{len(over)} NOs on unchanged code, not blocking: "
                     + " / ".join(f"{n['id']} {where(n)}" for n in over))
        return keep

    def after_review(self, job: Job) -> None:
        fid, secs = job.fn, time.monotonic() - job.started
        tokens, _, _ = read_result(job.out)
        self.reset_wt(fid, job.head)
        raw = read_json(self.ov / "reviews" / f"{fid}.json")
        nos = None if job.timed_out else checklist_nos(raw)
        if nos is None:
            why = "reviewer timed out" if job.timed_out else "review file missing or not the checklist JSON shape"
            self.log(fid, "review_fail", f"{why} model={job.model}", secs, tokens)
            self.fail(fid, f"review FAIL: {why}")
            return
        gap = str(raw.get("plan_gap") or "").strip() if isinstance(raw, dict) else ""
        blocking = [n for n in nos if n["severity"] in BLOCKING]
        self.log_nits(fid, [n for n in nos if n["severity"] not in BLOCKING], "review")
        unknown = sum(1 for e in (raw.get("items") or []) if isinstance(e, dict)
                      and str(e.get("answer", "")).lower() == "unknown") if isinstance(raw, dict) else 0
        detail = f"{len(blocking)} blocking NOs, {unknown} unknown model={job.model}"
        notes = (f"PLAN-GAP: {gap} / " if gap else "") + " / ".join(
            f"{n.get('evidence', '')} {n.get('fix', '')}" for n in blocking)
        if (gap or blocking) and self.plan_gap(fid, notes):
            self.log(fid, "review_fail", f"plan gap: {notes[:REASON_MAX]} {detail}", secs, tokens)
            self.park(fid, f"review asks for a change outside the allowed paths (plan gap): {notes}")
            return
        blocking = self.round_filter(fid, blocking, job.head)
        if not blocking:
            self.log(fid, "review_pass", detail, secs, tokens)
            self.land_queue.append(fid)
            self.set(fid, status="landing")
            return
        self.log(fid, "review_nos", f"{detail}: " + " / ".join(f"{n['id']} {where(n)}" for n in blocking),
                 secs, tokens)
        self.start_verify(fid, job, blocking)

    def verify_brief(self, fid: str, branch: str, findings: list[dict], rnd: int) -> Path:
        """Write the verifier's brief for `findings` (numbered from 1); returns the brief path."""
        out = self.ov / "verify" / f"{fid}-{rnd}.json"
        scratch = self.ov / "verify" / f"{fid}-{rnd}-scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        listed = "\n".join(f"{i}. [{n['id']} {n['severity']}] `{where(n)}`"
                           + (f" (module {n['module']})" if n.get("module") else "")
                           + f": {n.get('evidence') or n.get('issue') or ''}" for i, n in enumerate(findings, 1))
        brief = render("verify-brief.md", {
            "id": fid, "branch": branch, "verify_file": out.as_posix(), "scratch": scratch.as_posix(),
            "findings": listed, "commands": self.polish_commands(), "max_calls": str(REVIEW_MAX_CALLS)})
        path = self.ov / "briefs" / f"verify-{fid}-{rnd}.md"
        path.write_text(brief, encoding="utf-8")
        return path

    def verified(self, fid: str, rnd: int, findings: list[dict], timed_out: bool) -> list[dict] | None:
        """The findings the verifier reproduced; None when it wrote no usable result."""
        data = None if timed_out else read_json(self.ov / "verify" / f"{fid}-{rnd}.json")
        if not isinstance(data, list):
            return None
        hits = {}
        for e in data:
            if isinstance(e, dict) and e.get("reproduced") is True and str(e.get("how") or "").strip():
                hits[e.get("n")] = str(e["how"]).strip()
        return [{**n, "how": hits[i]} for i, n in enumerate(findings, 1) if i in hits]

    def start_verify(self, fid: str, job: Job, blocking: list[dict]) -> None:
        path = self.verify_brief(fid, f"fn/{fid}", blocking, job.attempt)
        self.verify_meta[fid] = {"findings": blocking, "head": job.head}
        self.start_job(fid, "verify", job.attempt, path, self.ov / "wt" / fid)
        self.jobs[fid].head = job.head

    def after_verify(self, job: Job) -> None:
        fid, secs = job.fn, time.monotonic() - job.started
        tokens, _, _ = read_result(job.out)
        self.reset_wt(fid, job.head)
        findings = self.verify_meta.pop(fid, {}).get("findings") or []
        real = self.verified(fid, job.attempt, findings, job.timed_out)
        if real is None:
            why = "verifier timed out" if job.timed_out else "verify file missing or not a JSON list"
            self.log(fid, "verify_fail", f"{why} model={job.model}", secs, tokens)
            self.fail(fid, f"review FAIL: {why}; the review's NOs were not checked")
            return
        dropped = [n for n in findings if not any(r["id"] == n["id"] and where(r) == where(n) for r in real)]
        if dropped:
            self.log(fid, "review_unverified", f"{len(dropped)} NOs not reproduced, not blocking: "
                     + " / ".join(f"{n['id']} {where(n)}" for n in dropped))
        if not real:
            self.log(fid, "review_pass", f"0 verified of {len(findings)} blocking NOs model={job.model}", secs, tokens)
            self.land_queue.append(fid)
            self.set(fid, status="landing")
            return
        self.log(fid, "review_fail", f"{len(real)} verified blocking NOs model={job.model}", secs, tokens)
        prev = self.st["functions"][fid].get("review") or {}
        self.set(fid, review={"head": job.head, "ids": sorted({n["id"] for n in real}),
                              "round": prev.get("round", 0) + 1})
        full = self.ov / "reviews" / f"{fid}-nos.md"
        listing = format_nos(real)
        full.write_text(f"# Verified blocking NOs for {fid}: fix every one\n\n{listing}\n", encoding="utf-8")
        head = f"review FAIL: {len(real)} verified blocking NOs; fix every one (full list: {full.as_posix()}):\n"
        self.fail(fid, head + (listing if len(head) + len(listing) <= REASON_MAX
                               else listing[:REASON_MAX - len(head) - 4] + " ..."))

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
            self.cycle_due = f"landed {fid}"  # run after launch(): dependents start before verdict and dash
            return
        reason = "land.py timed out" if job.timed_out else (
            last_line(out) or last_line(err) or f"land.py exit {job.proc.returncode}")
        if not self.land_logged(fid, job.log_at):  # killed or crashed before land.py wrote its own land_fail line
            self.log(fid, "land_fail", reason, secs)
        if GREEN_BEFORE in reason:
            self.park(fid, f"land: {reason}")
        elif CONFLICT in reason.lower() and self.start_conflict(fid, reason):
            return  # main moved under the branch: a conflict-fix pass, not an attempt
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

    # ---- polish: once every function is proven, a blind audit, fix passes per module, then a re-audit ----
    def polish_state(self) -> dict:
        """st["polish"]: round, status (idle | audit | fix | done | failed), issues and [round, issues] history."""
        return self.st.setdefault("polish", {"round": 0, "status": "idle", "issues": None, "history": []})

    def polish_pending(self) -> bool:
        land = self.land_job is not None and self.land_job.kind == "polish_land"
        return land or bool(self.polish_queue or self.polish_lands) or any(
            j.kind in POLISH_KINDS for j in self.jobs.values())

    def polish_step(self) -> bool:
        """Drive the polish phase one step; True if anything started."""
        pol = self.polish_state()
        if self.polish_rounds < 1 or pol["status"] in ("done", "failed"):
            return False
        if any(self.st["functions"][fid]["status"] != "proven" for fid in self.fns):
            return False
        started = self.pump_polish_land()
        if pol["status"] == "fix":
            for item in self.polish_ready():
                self.polish_queue.remove(item)
                self.start_polish_fix(*item)
                started = True
            if not self.polish_pending():
                return self.start_polish_audit(pol["round"] + 1)
        elif not self.busy():  # idle, or an audit a previous drive left unfinished
            return self.start_polish_audit(max(1, pol["round"]))
        return started

    def polish_claims(self, module: str) -> list[str]:
        section = module_section(self.arch, module)
        return module_paths(section, module) + module_shared(section)

    def polish_ready(self) -> list[tuple[str, list[dict]]]:
        """Queued fix passes that may start now: a free slot each and no path overlap with a fix in flight."""
        held = [fid for fid, j in self.jobs.items() if j.kind in ("polish_fix", "polish_review")] + self.polish_lands
        if self.land_job and self.land_job.kind == "polish_land":
            held.append(self.land_job.fn)
        taken = [self.polish_claims(self.polish_meta[fid][0]) for fid in held if fid in self.polish_meta]
        out, free = [], self.max_parallel - len(self.jobs)
        for item in self.polish_queue:
            if len(out) >= free:
                break
            mine = self.polish_claims(item[0])
            if not any(paths_overlap(mine, other) for other in taken):
                taken.append(mine)
                out.append(item)
        return out

    def polish_cmd(self, kind: str) -> tuple[list[str], str]:
        """The worker command for a polish kind, and its model label."""
        custom = self.custom_cmd("review" if kind in ("polish_audit", "polish_review", "polish_verify") else "build")
        if custom:
            return custom, "custom"
        base = self.worker_cmd({"polish_review": "review", "polish_verify": "verify"}.get(kind, "build"))
        if kind == "polish_audit" and self.audit_model:
            base = (base[:base.index("--model")] if "--model" in base else base) + ["--model", self.audit_model]
        return base, base[base.index("--model") + 1] if "--model" in base[:-1] else "default"

    def start_polish_job(self, fid: str, kind: str, rnd: int, brief: Path, cwd: Path) -> None:
        base, model = self.polish_cmd(kind)
        out = self.ov / "runs" / f"{fid}-r{rnd}-{kind}.json"
        try:
            proc = spawn([*base, f"Read and do {brief.as_posix()}"], cwd, out)
        except OSError as e:
            raise DriveError(f"cannot start the {kind} worker {base[0]}: {e}") from e
        start = time.monotonic()
        self.jobs[fid] = Job(fid, kind, proc, out, start, start + self.timeout_s, rnd, model=model)

    def polish_commands(self) -> str:
        lines = [f"- {name}: `{cmd}`" for name, cmd in self.cfg["commands"].items() if "{test}" not in cmd]
        return "\n".join(lines) or "(overhaul.toml has no whole-tree [commands])"

    def polish_allowed(self, module: str) -> str:
        paths = module_paths(module_section(self.arch, module), module)
        tests = unit_dirs(self.cfg["commands"].get("test", "")) or ["tests/unit/"]
        return "\n".join([f"- `{p}`" for p in paths] + [f"- `{t}` (unit tests for this module only)" for t in tests])

    def start_polish_audit(self, rnd: int) -> bool:
        """The fixed checklist over the whole tree plus the dedup role (template polish-audit.md)."""
        fid, pol = "polish-audit", self.polish_state()
        audit = self.ov / "polish" / f"audit-{rnd}.json"
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.unlink(missing_ok=True)
        modules = sorted({f.module for f in self.order})
        brief = render("polish-audit.md", {
            "round": str(rnd), "main": self.main, "audit_file": audit.as_posix(), "max_calls": str(self.max_calls),
            "scratch": (self.ov / "polish").as_posix(), "policies": self.policies(), "commands": self.polish_commands(),
            "standard": STANDARD.as_posix(), "checklist_path": CHECKLIST.as_posix(),
            "checklist_version": checklist()[0], "checklist": checklist_text(applicable_items(None)),
            "modules": "\n".join(f"- `{m}`: " + ", ".join(module_paths(module_section(self.arch, m), m)) for m in modules)})
        path = self.ov / "briefs" / f"polish-audit-{rnd}.md"
        path.write_text(brief, encoding="utf-8")
        self.start_polish_job(fid, "polish_audit", rnd, path, self.prepare_worktree(fid))
        pol.update(round=rnd, status="audit")
        self.save()
        self.say(f"polish checklist round {rnd}")
        return True

    @staticmethod
    def read_audit(path: Path) -> list[dict] | None:
        data = read_json(path)
        if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
            return None
        return data

    def polish_audit_failed(self, rnd: int, why: str, model: str, secs: float, tokens: int | None) -> None:
        pol = self.polish_state()
        self.log(None, "polish_audit", f"round {rnd} failed: {why} model={model}", secs, tokens)
        self.log(None, "polish_done", f"? issues: {why}")
        pol.update(status="failed", issues=None)
        self.save()
        self.say(f"polish audit round {rnd} failed: {why}")

    def after_polish_audit(self, job: Job) -> None:
        rnd, secs = job.attempt, time.monotonic() - job.started
        tokens, _, _ = read_result(job.out)
        raw = None if job.timed_out else self.read_audit(self.ov / "polish" / f"audit-{rnd}.json")
        nos = None if raw is None else checklist_nos(raw)
        if nos is None:
            why = "auditor timed out" if job.timed_out else f"audit-{rnd}.json missing or not a JSON list"
            self.polish_audit_failed(rnd, why, job.model, secs, tokens)
            return
        for e in raw:
            if str(e.get("id") or e.get("rule") or "") == "NEW-RULE":
                self.log(None, "polish_new_rule", f"{where(e)} {e.get('evidence', '')}"[:REASON_MAX])
        nits = [n for n in nos if n["severity"] not in BLOCKING]
        blocking = [n for n in nos if n["severity"] in BLOCKING]
        self.log_nits(None, nits, f"polish round {rnd}")
        self.polish_state()["nits"] = min(len(nits), NITS_MAX)
        meta = {"rnd": rnd, "model": job.model, "secs": secs, "tokens": tokens, "nos": len(nos)}
        if not blocking:
            self.polish_checked(meta, [], [])
            return
        path = self.verify_brief("polish-verify", self.main, blocking, rnd)
        self.verify_meta["polish-verify"] = {**meta, "findings": blocking}
        self.start_polish_job("polish-verify", "polish_verify", rnd, path, self.ov / "wt" / "polish-audit")

    def after_polish_verify(self, job: Job) -> None:
        fid = job.fn
        meta = self.verify_meta.pop(fid, {})
        findings = meta.get("findings") or []
        real = self.verified(fid, job.attempt, findings, job.timed_out)
        if fid != "polish-verify":  # a polish fix's review NOs
            if real is None:
                self.polish_failed(fid, "verifier wrote no result; the review's NOs were not checked")
            elif real:
                self.polish_failed(fid, f"review: {len(real)} verified blocking NOs: {format_nos(real)}")
            else:
                self.polish_lands.append(fid)
            return
        if real is None:
            why = "verifier timed out" if job.timed_out else "verify file missing or not a JSON list"
            self.polish_audit_failed(meta.get("rnd", job.attempt), why, job.model, meta.get("secs", 0.0), None)
            return
        dropped = [n for n in findings if n not in [{k: v for k, v in r.items() if k != "how"} for r in real]]
        self.polish_checked(meta, real, dropped)

    def polish_checked(self, meta: dict, real: list[dict], dropped: list[dict]) -> None:
        """A polish checklist run is judged: verified blocking NOs become the round's issues."""
        rnd, pol = meta["rnd"], self.polish_state()
        (self.ov / "polish" / f"checklist-{rnd}.json").write_text(json.dumps(real, indent=1), encoding="utf-8")
        if dropped:
            self.log(None, "review_unverified", f"polish round {rnd}: {len(dropped)} NOs not reproduced, "
                     "not blocking: " + " / ".join(f"{n['id']} {where(n)}" for n in dropped))
        n = len(real)
        pol["history"].append([rnd, n])
        self.log(None, "polish_audit", f"round {rnd}: {n} issues model={meta['model']}", meta["secs"], meta["tokens"])
        self.log(None, "polish_checklist", f"round {rnd}: {meta['nos']} NOs, {n} verified blocking, "
                 f"{len(dropped)} unverified, {pol.get('nits', 0)} nits")
        issues, n = self.keep_best_polish(pol, rnd, real)
        pol["issues"] = n
        if not issues or rnd > self.polish_rounds:
            pol["status"] = "done"
            self.save()
            self.log(None, "polish_done", f"{n} issues after {rnd - 1} fix rounds")
            self.say(f"polish done: {n} issues after {rnd - 1} fix rounds")
            return
        groups: dict[str, list[dict]] = {}
        for item in issues:
            groups.setdefault(str(item.get("module") or "unknown").strip(), []).append(item)
        self.polish_queue = sorted(groups.items())
        pol["status"] = "fix"
        self.save()

    def keep_best_polish(self, pol: dict, rnd: int, issues: list[dict]) -> tuple[list[dict], int]:
        """A fix round that raised the verified NO count is reverted: main goes back to the best checked tree.

        pol["best"] is [round, issues, sha] of the run with the fewest verified NOs so far. Returns the list
        the next fix round works from (the best round's) and its count."""
        sha, best = git(self.repo, "rev-parse", self.main).strip(), pol.get("best")
        if not best or len(issues) <= best[1]:
            pol["best"] = [rnd, len(issues), sha]
            return issues, len(issues)
        kept = self.read_audit(self.ov / "polish" / f"checklist-{best[0]}.json")
        if kept is None or not self.revert_main(best[2], sha, f"polish: revert round {rnd - 1} fixes "
                                                f"({len(issues)} issues > {best[1]} after round {best[0]})"):
            pol["best"] = [rnd, len(issues), sha]
            return issues, len(issues)
        self.log(None, "polish_revert", f"round {rnd - 1} fixes reverted: {len(issues)} issues > {best[1]}")
        self.say(f"polish round {rnd - 1} raised issues {best[1]} -> {len(issues)}: reverted")
        return kept, len(kept)

    def revert_main(self, good: str, head: str, message: str) -> bool:
        """Commit good's tree on top of main (head) under the land lock; False if main moved or the lock is busy."""
        if good == head:
            return False
        if not acquire_lock(self.repo, LOCK_WAIT_S):
            return False
        try:
            tree = git(self.repo, "rev-parse", f"{good}^{{tree}}").strip()
            new = git(self.repo, "commit-tree", tree, "-p", head, "-m", message).strip()
            land_mod.advance(str(self.repo), self.main, new, head)
            return True
        except (DriveError, land_mod.LandFail) as e:
            self.say(f"polish revert failed: {e}")
            return False
        finally:
            release_lock(self.repo)

    def start_polish_fix(self, module: str, items: list[dict]) -> None:
        rnd = self.polish_state()["round"]
        fid = f"polish-r{rnd}-" + (re.sub(r"[^A-Za-z0-9._-]+", "-", module).strip("-.") or "misc")
        issues = "\n".join(f"- `{i.get('file', '?')}:{i.get('line', '?')}` [{i.get('rule', '?')}] "
                           f"{i.get('evidence') or i.get('issue') or ''}" for i in items)
        self.polish_meta[fid] = (module, issues)
        brief = render("polish-fix-brief.md", {
            "id": fid, "round": str(rnd), "module": module, "branch": f"fn/{fid}", "max_calls": str(self.max_calls),
            "issues": issues, "allowed_paths": self.polish_allowed(module), "policies": self.policies(),
            "commands": self.polish_commands()})
        path = self.ov / "briefs" / f"{fid}.md"
        path.write_text(brief, encoding="utf-8")
        self.start_polish_job(fid, "polish_fix", rnd, path, self.prepare_worktree(fid))
        self.log(fid, "polish_fix", f"start {module}: {len(items)} issues model={self.jobs[fid].model}")
        self.say(f"polish fix {fid}")

    def after_polish_fix(self, job: Job) -> None:
        tokens, turns, text = read_result(job.out)
        self.log(job.fn, "polish_fix", f"built rc={job.proc.returncode} turns={turns}/{self.max_calls}",
                 time.monotonic() - job.started, tokens)
        problem = self.build_problem(job, text)
        if problem:
            self.polish_failed(job.fn, problem)
            return
        module, issues = self.polish_meta[job.fn]
        branch, review_file = f"fn/{job.fn}", self.ov / "reviews" / f"{job.fn}.json"
        review_file.parent.mkdir(parents=True, exist_ok=True)
        review_file.unlink(missing_ok=True)
        brief = render("review-brief.md", {
            "id": job.fn, "branch": branch, "allowed_paths": self.polish_allowed(module),
            "row": f"A polish fix of module `{module}`: no function row, and no behaviour may change. "
                   f"The issues it must fix:\n\n{issues}",
            "acceptance_source": "(none: a polish fix adds no function; land.py re-runs every proven acceptance test)",
            "review_file": review_file.as_posix(), "policies": self.policies(),
            "standard_path": STANDARD.as_posix(), "standard": relevant_rules(module + "\n" + issues),
            "checklist_path": CHECKLIST.as_posix(), "checklist_version": checklist()[0],
            "checklist": checklist_text(applicable_items(module + "\n" + issues)),
            "round_rule": "A polish fix: a NO blocks only when it points at a line this fix changed; NOs on "
                          "unchanged code are logged as review_overreach and do not block.",
            "diffstat": git(self.repo, "diff", "--stat", f"{self.main}...{branch}").rstrip(),
            "diff": git(self.repo, "diff", f"{self.main}...{branch}").rstrip(), "max_calls": str(REVIEW_MAX_CALLS)})
        if len(brief) // 4 > self.max_tokens:
            self.polish_failed(job.fn, f"change too large to review (over brief_max_tokens {self.max_tokens})")
            return
        path = self.ov / "briefs" / f"review-{job.fn}.md"
        path.write_text(brief, encoding="utf-8")
        head = git(self.repo, "rev-parse", branch).strip()
        self.start_polish_job(job.fn, "polish_review", job.attempt, path, self.ov / "wt" / job.fn)
        self.jobs[job.fn].head = head

    def after_polish_review(self, job: Job) -> None:
        """Only blocking NOs on lines the fix changed may stop it, and only once a verifier reproduces them."""
        fid = job.fn
        tokens, _, _ = read_result(job.out)
        self.reset_wt(fid, job.head)
        nos = None if job.timed_out else checklist_nos(read_json(self.ov / "reviews" / f"{fid}.json"))
        if nos is None:
            why = "reviewer timed out" if job.timed_out else "review file missing or not the checklist JSON shape"
            self.log(fid, "polish_fix", f"review FAIL: {why} model={job.model}", time.monotonic() - job.started,
                     tokens)
            self.polish_failed(fid, f"review FAIL: {why}")
            return
        self.log_nits(fid, [n for n in nos if n["severity"] not in BLOCKING], "polish review")
        base = git(self.repo, "merge-base", self.main, job.head).strip()
        changed = changed_lines(self.repo, base, job.head)
        blocking = [n for n in nos if n["severity"] in BLOCKING]
        mine = [n for n in blocking if changed is None or in_changed(n, changed)]
        over = [n for n in blocking if n not in mine]
        if over:
            self.log(fid, "review_overreach", f"{len(over)} NOs on unchanged code, not blocking: "
                     + " / ".join(f"{n['id']} {where(n)}" for n in over))
        self.log(fid, "polish_fix", f"review {'NO' if mine else 'PASS'}: {len(mine)} blocking NOs in changed code "
                 f"model={job.model}", time.monotonic() - job.started, tokens)
        if not mine:
            self.polish_lands.append(fid)
            return
        path = self.verify_brief(fid, f"fn/{fid}", mine, job.attempt)
        self.verify_meta[fid] = {"findings": mine}
        self.start_polish_job(fid, "polish_verify", job.attempt, path, self.ov / "wt" / fid)

    def pump_polish_land(self) -> bool:
        if self.land_job or self.land_queue or not self.polish_lands:
            return False
        fid = self.polish_lands.pop(0)
        out = self.ov / "runs" / f"{fid}-land.out"
        cmd = [sys.executable, str(self.scripts / "land.py"), "--repo", str(self.repo), "--fn", fid,
               "--main", self.main, "--polish"]
        start = time.monotonic()
        self.land_job = Job(fid, "polish_land", spawn(cmd, self.repo, out), out, start,
                            start + 2 * self.timeout_s, 0)
        return True

    def after_polish_land(self, job: Job) -> None:
        if job.proc.returncode == 0 and not job.timed_out:
            self.log(job.fn, "polish_fix", f"landed {self.polish_meta[job.fn][0]}", time.monotonic() - job.started)
            self.say(f"polish landed {job.fn}")
            return
        out, err = read_text(job.out), read_text(job.out.with_suffix(".err"))
        self.polish_failed(job.fn, "land: " + ("land.py timed out" if job.timed_out else (
            last_line(out) or last_line(err) or f"land.py exit {job.proc.returncode}")))

    def polish_failed(self, fid: str, reason: str) -> None:
        """A fix pass that cannot land is dropped; the re-audit finds its issues again."""
        self.log(fid, "polish_fix", f"failed: {reason[:REASON_MAX]}")
        self.say(f"polish {fid} failed: {reason}")

    def reap_polish(self, job: Job) -> None:
        if job is self.land_job:
            self.land_job = None
        else:
            del self.jobs[job.fn]
        {"polish_audit": self.after_polish_audit, "polish_verify": self.after_polish_verify,
         "polish_fix": self.after_polish_fix,
         "polish_review": self.after_polish_review, "polish_land": self.after_polish_land}[job.kind](job)

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
        self.set(fid, status="planned", attempts=0, conflicts=0, last_reason="")
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
