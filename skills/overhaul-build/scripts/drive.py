#!/usr/bin/env python3
"""Overhaul v2 harness: build, review and land every function in spec/FUNCTIONS.md.

A READY function (planned, its `behaviour` deps proven, retries left, no in-flight function whose files overlap
its own) gets a worktree at .overhaul/wt/<id> on fn/<id> from main, a brief from templates/, and a `claude -p`
builder. A dep marked `stub` in the FUNCTIONS.md `needs` column does not block: the plan commit landed its interface
stub, so the dependent builds against that contract at once. A finished build merges main into its branch and reruns
the gate in its worktree (a conflict there goes to the conflict-fix pass), then gets a blind reviewer; a PASS goes to
land.py. Lands whose footprints do not overlap run at once, the later ones stacked on the earlier (land.py --stack). A failure retries from main
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
os.environ.setdefault("PROPTEST_DISABLE_FAILURE_PERSISTENCE", "1")  # no .proptest-regressions files (vc1)
os.environ.setdefault("PROPTEST_RNG_SEED", "0")  # fixed seed: a property green at land stays green on main (vc1)

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
    (r"output|--out|save as|write to", (36,)),  # a user-named output target must not clobber (C33)
    (r"\bcli\b|argparse|command|argv", (5, 6)),
    (r"total|tax|price|sum|report", (9, 11)),
)
COLUMNS = ("id", "phase", "module", "deps", "entry", "acceptance", "status")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
PATHS_LINE = re.compile(r"\W*(?:paths?|files)\W*?[:=]\s*(.+)", re.I)
SHARED_LINE = re.compile(r"\W*shared\W*?[:=]\s*(.+)", re.I)  # files every function may touch; never a lock
CONFLICT_SYNC = "merge conflict merging"
FILES_COLUMN = "files"  # required FUNCTIONS.md column (plan_validate): the function's own files, the unit of the build lock
NEEDS_COLUMN = "needs"  # required FUNCTIONS.md column: which deps must land first (behaviour) or only stub (default)
RULES_COLUMN = "rules"  # optional FUNCTIONS.md column: the project rule ids (P07, S18) the row must hold
RULE_PATHS = ("patterns", "standards")  # [paths] keys of the project's rule files (spec/PATTERNS.md, spec/STANDARDS.md)
RULE_ID = re.compile(r"\b[A-Z]{1,3}\d+\b")
RULE_DEF = re.compile(r"^[\s|#>*-]*\**([A-Z]{1,3}\d+)\**(?:[.:)|]|\s|$)")
RULE_TEXT_MAX = 700  # characters of one cited rule in a brief


def rule_defs(text: str) -> dict[str, str]:
    """Rule id -> its text, from a rule file: a line that starts with the id (`S18. ...`, `| P07 | ... |`,
    `### P07 ...`) plus the lines that continue it up to a blank line, a table row, a heading or the next id."""
    defs: dict[str, str] = {}
    cur = None
    for line in text.splitlines():
        m = RULE_DEF.match(line)
        if m and m.group(1) not in defs:
            cur = m.group(1)
            defs[cur] = re.sub(r"\s+", " ", line.strip().strip("|#>*- "))
        elif m or not line.strip() or line.lstrip().startswith(("|", "#")):
            cur = None
        elif cur:
            defs[cur] += " " + line.strip()
    return {k: v if len(v) <= RULE_TEXT_MAX else v[:RULE_TEXT_MAX - 4] + " ..." for k, v in defs.items()}


def parse_needs(cell: str, deps: list[str]) -> list[str] | None:
    """The deps of a row that block its build: `stub` (or empty) blocks none, `behaviour` blocks all, and a list
    `F03:behaviour, F04:stub` blocks the named ones. None when the cell is malformed (plan_validate reports it)."""
    cell = cell.strip().strip("`").strip().lower()
    if cell in ("", "-", "stub"):
        return []
    if cell in ("behaviour", "behavior"):
        return list(deps)
    out: list[str] = []
    for part in re.split(r"[,;]", cell):
        did, _, kind = part.strip().strip("`").partition(":")
        did, kind = did.strip().upper(), kind.strip()
        if kind not in ("stub", "behaviour", "behavior") or did not in {d.upper() for d in deps}:
            return None
        if kind != "stub":
            out.append(next(d for d in deps if d.upper() == did))
    return out
IN_FLIGHT = ("building", "review", "landing")
POLISH_KINDS = ("polish_audit", "polish_verify", "polish_fix", "polish_review", "polish_land")
GREEN_BEFORE = "acceptance already green before change"
CONFLICT = "merge conflict"  # land.py: "merge conflict merging <main> into fn/<id>"
# A review fix the builder cannot make: a verified blocking NO tied to the reviewer's PLAN-GAP line, or one saying
# "outside (the) allowed paths" on a diff that stayed inside them. Such a FAIL goes to needs-plan; another builder
# pass would only re-learn it. Any other PLAN-GAP goes to .overhaul/plan-gaps.jsonl: one cross-module pass after
# the build (polish_step) fixes them all, since a shared constant needs its owner and every caller changed together.
PLAN_GAP = re.compile(r"\bPLAN-GAP\b")
# Checklist items whose only fix lives in another module (a shared constant, rule or helper its owner should export).
# A verified blocking NO of one of these, tied to a PLAN-GAP, does not park the function: the gap is batched.
DEDUP_IDS = frozenset({"C03", "C05", "C06", "C07", "C08"})
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
    "probe": {"enabled": True, "mutation_floor": 0.9, "max_mutants": 40, "cmd": ""},
}
PROBE_PREFIX = "probe-"  # a probe fix pass: fid probe-<module>, landed like a polish fix
PROBE_DONE = ("done", "below_floor", "fix_failed", "error", "skipped")
PLAN_FIXES = 2  # --reset per function; a third needs-plan stops the build for the owner
PROBE_FIXES = 2  # fix passes per module below its mutation floor; the score is measured again after each
DEPFIX_PREFIX = "depfix-"  # a dependency fix: a builder's BLOCKED-BY defect, fixed in the owning module
BLOCKED_BY = re.compile(r"^\s*BLOCKED-BY:\s*`?([^\s`:]+)(?::(\d+))?`?\s*(.*)$", re.M)
GAPS_FID = "polish-gaps"  # the one cross-module pass over .overhaul/plan-gaps.jsonl, run alone before polish round 1
MAIN_FLAKE_RE = re.compile(r"main flake: test=(\S+) path=(\S+) module=(\S+)")
FIX_RETRIES = 1  # a probe or polish fix whose review has verified blocking NOs gets this many retry passes


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
    needs: list[str] | None = None  # deps that must land before the build starts (behaviour); None = all deps
    rules: list[str] = field(default_factory=list)  # project rule ids from the optional rules column

    @property
    def blocking(self) -> list[str]:
        return self.deps if self.needs is None else self.needs


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
    slot: int = -1  # a land: k of its CARGO_TARGET_DIR .overhaul/target/land-<k>


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
    project: bool = False  # `R` in the rules field: asked only of a row that cites project rules
    whole: bool = False  # `W` in the rules field: a whole-tree item, asked only by the polish audit


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
            items[it[1]] = Item(it[1], it[2], nums, it[4].strip(), it[5].strip(),
                                bool(re.search(r"(?<![A-Za-z])R(?![A-Za-z0-9])", it[3])),
                                bool(re.search(r"(?<![A-Za-z])W(?![A-Za-z0-9])", it[3])))
    return (m[1] if m else "v?"), items


def applicable_items(text: str | None, project_rules: bool = False) -> list[Item]:
    """Contract items always, plus the items whose rules apply to the text; None means the whole checklist. The
    project-rules item (C30) only for a row that cites project rules; whole-tree (W) items only for None."""
    items = [i for i in checklist()[1].values() if project_rules or not i.project]
    if text is None:
        return items
    want = wanted_rules(text)
    return [i for i in items if not i.whole and (not i.rules or want.intersection(i.rules))]


def checklist_text(items: list[Item]) -> str:
    lines = [f"- {i.id} [{i.severity}] {i.question} Decide: {i.decide}" for i in items]
    return "\n".join(lines) or f"(checklist missing: {CHECKLIST.as_posix()})"


BAD_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def read_json(path: Path) -> object:
    r"""An agent's result file; a hand-written lone backslash (a regex or a Windows path in evidence text) is read
    as a literal backslash instead of voiding the whole result (trial 11: `'\.file'` lost a polish verdict)."""
    text = read_text(path)
    for t in (text, BAD_ESCAPE.sub(r"\\\\", text)):
        try:
            return json.loads(t)
        except ValueError:
            pass
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


def spawn(cmd: list[str], cwd: Path, out: Path, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Start a worker in its own process group; stdout to out, stderr beside it; env replaces the environment."""
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    with open(out, "wb") as fo, open(out.with_suffix(".err"), "wb") as fe:
        return subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, env=env, **group)


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
    names_needs: int | None = None
    names_rules: int | None = None
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
            names_needs = names.index(NEEDS_COLUMN) if NEEDS_COLUMN in names else None
            names_rules = names.index(RULES_COLUMN) if RULES_COLUMN in names else None
            continue
        if idx is None or all(set(c) <= set(":- ") for c in row):
            continue
        get = {c: (row[i] if i < len(row) else "") for c, i in idx.items()}
        files = row[names_files] if names_files is not None and names_files < len(row) else ""
        rules = row[names_rules] if names_rules is not None and names_rules < len(row) else ""
        deps = [bare(d) for d in get["deps"].split(",") if bare(d) not in ("", "-")]
        # no needs column: every dep blocks (the v2 behaviour, so old plans are unchanged)
        needs = parse_needs(row[names_needs], deps) if names_needs is not None and names_needs < len(row) else None
        fns.append(Fn(
            id=bare(get["id"]), phase=bare(get["phase"]), module=bare(get["module"]),
            deps=deps, needs=deps if names_needs is not None and needs is None else needs,
            entry=get["entry"], acceptance=[bare(a) for a in get["acceptance"].split(";") if bare(a)],
            status=bare(get["status"]).lower(), header=header, row=line.strip(), order=len(fns),
            files=[bare(x) for x in re.split(r"[,;]", files) if bare(x) not in ("", "-")],
            rules=list(dict.fromkeys(RULE_ID.findall(rules)))))
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
    raw_cmds = raw.get("commands", {})
    # [commands.acceptance_by] and [commands.extra] are tables, kept apart: every cfg["commands"] value is a string
    cfg["commands"] = {k: str(v) for k, v in raw_cmds.items() if not isinstance(v, dict)}
    cfg["acceptance"] = land_mod.acceptance_template(raw_cmds)
    cfg["extra"] = land_mod.extra_cmds(raw_cmds)
    return cfg


class Driver:
    """One run of the harness over one repo."""

    def __init__(self, repo: Path, cfg: dict):
        lim, har = cfg["limits"], cfg["harness"]
        self.repo, self.cfg = repo, cfg
        # [harness] max_parallel, else the older [limits] key, else 6
        self.max_parallel = max(1, int(har.get("max_parallel") or lim.get("max_parallel") or 6))
        self.max_lands = max(1, int(har.get("max_lands") or 3))  # lands at once; overlapping footprints never
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
        self.polish_meta: dict[str, tuple[str, str]] = {}  # fix fid -> (module(s), issues text)
        probe = {**DEFAULTS["probe"], **(cfg.get("probe") or {})}
        self.probe_on = bool(probe["enabled"])
        self.mutation_floor = float(probe["mutation_floor"])
        self.max_mutants = int(probe["max_mutants"])
        self.probe_cmd = str(probe.get("cmd") or "")
        scripts = Path(str(har.get("scripts_dir") or HERE))
        self.scripts = scripts if scripts.is_absolute() else repo / scripts
        self.ov = repo / ".overhaul"
        self.state_path = repo / cfg["paths"]["state"]
        self.jobs: dict[str, Job] = {}
        self.land_queue: list[str] = []
        self.land_job: Job | None = None
        self.side_lands: dict[str, Job] = {}  # lands stacked on the ones in flight (land.py --stack)
        self.slots: dict[str, int] = {}  # fid -> k of the CARGO_TARGET_DIR .overhaul/target/slot-<k> it reuses
        self.flake_tests: set[str] = set()  # main-flake tests already sent to a fix this run (one fix per test)
        self.barrier = bool(har.get("phase_barrier", True))  # phase N+1 waits until every phase <=N is proven
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
            rec.pop("blocked_on", None)  # its dependency fix died with the previous drive
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
        main_wt = land_mod.checked_out_at(str(self.repo), self.main)
        dirty = land_mod.dirty_tracked(main_wt) if main_wt else []
        if dirty:  # every land would fail to fast-forward main (vc1: a manager's plan fix left uncommitted)
            raise DriveError(f"{land_mod.DIRTY_MAIN} {main_wt}: {', '.join(dirty[:5])}; commit the plan fix "
                             "(plan_validate PLAN OK first) or revert it, then run again")
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
                changed = self.probe_step() or changed
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
        """In flight: jobs, the land queue, lands, and functions waiting at landing on a main-flake fix."""
        waiting = {fid for fid, r in self.st["functions"].items() if r["status"] == "landing" and r.get("blocked_on")}
        return set(self.jobs) | set(self.land_queue) | {j.fn for j in self.land_jobs()} | waiting

    def land_jobs(self) -> list[Job]:
        """Lands in flight, the plain one (or a polish land) first, then the stacked ones in start order."""
        return [*([self.land_job] if self.land_job else []), *self.side_lands.values()]

    def claims(self, f: Fn) -> list[str]:
        """The files a function's build locks: its FUNCTIONS.md `files` cell, else its whole module's paths.
        The module's `shared:` files are left out: a clash there goes to the conflict-fix pass."""
        section = module_section(self.arch, f.module)
        shared = set(module_shared(section))
        return [p for p in (f.files or module_paths(section, f.module)) if p not in shared]

    def ready(self) -> list[Fn]:
        """Functions to launch now: planned, every blocking dep proven, retries left, and no file overlap with a
        function in flight (building, in review or landing) or with one launched before it in this pass. A dep the
        `needs` column marks `stub` does not block: its stub is on main from the plan commit.

        Two builders that edit one file fail to land with a merge conflict, so a function waits while an in-flight
        function's claimed files overlap its own. A plan with a `files` column runs one module's functions at once;
        without it a function claims its module's paths, one function per module at a time."""
        recs = self.st["functions"]
        proven = {fid for fid, r in recs.items() if r["status"] == "proven"}
        # the phase barrier: nothing past the lowest phase that still has an unproven function starts
        open_phase = min((phase_key(f)[0] for f in self.order if f.id not in proven), default=0)
        busy = self.busy()
        taken = [self.claims(self.fns[fid]) for fid in busy if fid in self.fns]
        ready: list[Fn] = []
        for f in self.order:
            if (recs[f.id]["status"] != "planned" or f.id in busy or recs[f.id]["attempts"] > self.budget(f.id)
                    or recs[f.id].get("blocked_on") or (self.barrier and phase_key(f)[0] > open_phase)
                    or not all(d in proven for d in f.blocking)):
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
        for job in [*self.jobs.values(), *self.land_jobs()]:
            if job.proc.poll() is None:
                if now < job.deadline:
                    continue
                kill_tree(job.proc)
                job.timed_out = True
            done = True
            if job.kind in POLISH_KINDS:
                self.reap_polish(job)
            elif job.kind == "land":
                if job is self.land_job:
                    self.land_job = None
                else:
                    del self.side_lands[job.fn]
                self.after_land(job)
            else:
                del self.jobs[job.fn]
                {"build": self.after_build, "conflict": self.after_conflict,
                 "probe_mutate": self.after_probe_mutate,
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
            proc = spawn(cmd, cwd, out, self.slot_env(fid))
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
                    return self.lint_hook(wt)
            except DriveError:
                pass
        if wt.exists():
            shutil.rmtree(wt, onexc=force_remove)
        git(self.repo, "worktree", "prune")
        git(self.repo, "worktree", "add", "-q", "-f", "-B", branch, str(wt), self.main)
        return self.lint_hook(wt)

    def allowed_lists(self, f: Fn) -> tuple[list[str], list[str]]:
        section = module_section(self.arch, f.module)
        paths = f.files + module_shared(section) if f.files else module_paths(section, f.module)
        return list(dict.fromkeys(paths)), unit_dirs(self.cfg["commands"].get("test", "")) or ["tests/unit/"]

    def allowed(self, f: Fn) -> str:
        paths, tests = self.allowed_lists(f)
        return "\n".join([f"- `{p}`" for p in paths] + [f"- `{t}` (unit tests for this module only)" for t in tests])

    def gap_nos(self, fid: str, gap: str, real: list[dict]) -> list[dict]:
        """The verified blocking NOs only the plan can fix (see PLAN_GAP): each tied to the PLAN-GAP line (its id
        or file named there) or saying PLAN-GAP / outside the allowed paths, on a diff that stayed inside them."""
        def tied(n: dict) -> bool:
            text = f"{n.get('evidence', '')} {n.get('fix', '')}"
            name = Path(str(n.get("file") or "")).name
            return bool(PLAN_GAP.search(text) or OUTSIDE.search(text) or (gap and (
                re.search(rf"(?<![\w-]){re.escape(str(n['id']))}(?![\w-])", gap) or (name and name in gap))))
        hits = [n for n in real if tied(n)]
        if not hits:
            return []
        allowed = [p for group in self.allowed_lists(self.fns[fid]) for p in group]
        changed = git(self.repo, "diff", "--name-only", f"{self.main}...fn/{fid}").splitlines()
        return hits if all(in_paths(p, allowed) for p in changed) else []  # else the builder strayed: ordinary FAIL

    @property
    def polish_tries(self) -> dict[str, dict]:
        """fix fid -> {"round", "head", "ids"} of its last failed review; kept in state.json across a restart."""
        return self.st.setdefault("polish_tries", {})

    def gaps_path(self) -> Path:
        return self.ov / "plan-gaps.jsonl"

    def read_gaps(self) -> list[dict]:
        out = []
        for line in read_text(self.gaps_path()).splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and str(e.get("gap") or "").strip():
                out.append(e)
        return out

    def keep_gap(self, fid: str, gap: str) -> None:
        """A PLAN-GAP that blocks nothing: record it once for the plan-gaps pass after the build."""
        if not gap or any(e.get("fn") == fid and e.get("gap") == gap for e in self.read_gaps()):
            return
        self.gaps_path().parent.mkdir(parents=True, exist_ok=True)
        with self.gaps_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"fn": fid, "module": self.fns[fid].module, "gap": gap, "at": now_iso()}) + "\n")
        self.log(fid, "plan_gap", f"recorded for the plan-gaps pass: {gap[:REASON_MAX]}")

    def acceptance_text(self, f: Fn) -> str:
        parts = []
        for file in dict.fromkeys(acc_file(a) for a in f.acceptance):
            src = (self.read_main(file) or "(missing on main)").rstrip()
            parts.append(f"`{file}`\n`````{FENCE.get(Path(file).suffix, '')}\n{src}\n`````")
        return "\n\n".join(parts)

    def commands_text(self, f: Fn) -> str:
        lines = []
        for name, cmd in self.cfg["commands"].items():
            if name == "acceptance":  # per test file: [commands.acceptance_by] may give a file its own template
                cmd = self.cfg.get("acceptance") or cmd
            if "{test}" in str(cmd):  # the exact commands the gate runs ({test}, {file}, {stem}, {node} filled)
                lines += [f"- {name}: `{c}`" for _, c in land_mod.acceptance_cmds(cmd, f.acceptance)]
            else:
                lines.append(f"- {name}: `{cmd}`")
        lines += [f"- {name}: `{cmd}`" for name, cmd in self.cfg.get("extra", [])]
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
        if self.blocked_by(job.fn, text):
            return
        self.to_review(job, self.build_problem(job, text) or self.sync_problem(job.fn))

    def owner_module(self, path: str) -> str:
        """The module whose ARCHITECTURE paths hold path, or ''."""
        for m in dict.fromkeys(f.module for f in self.order):
            if any(paths_overlap([path], [p]) for p in module_paths(module_section(self.arch, m), m)):
                return m
        return ""

    def blocked_by(self, fid: str, text: str) -> bool:
        """A builder's BLOCKED-BY line: its acceptance fails on a landed dependency's bug outside its paths, which
        no retry of it can fix. The owning module gets one fix pass (fid depfix-<id>, built, reviewed and landed
        like a probe fix) and the function waits for it without spending an attempt. One per function; a second
        BLOCKED-BY is an ordinary failed attempt."""
        m = BLOCKED_BY.search(text or "")
        rec = self.st["functions"][fid]
        if not m or rec.get("depfixes", 0) >= 1:
            return False
        path, line, why = m.group(1).strip("`"), m.group(2) or "?", m.group(3).strip() or "(no detail)"
        if path.startswith(self.protected):  # the contract itself is wrong: only a plan fix helps, so stop now
            self.log(fid, "blocked_by", f"contract {path}:{line}: {why}"[:REASON_MAX])
            self.park(fid, f"contract defect reported by the builder in {path}:{line}: {why}")
            return True
        module, dfid = self.owner_module(path), f"{DEPFIX_PREFIX}{fid}"
        if (not module or module == self.fns[fid].module or dfid in self.jobs or dfid in self.polish_lands
                or any(paths_overlap([path], [c]) for c in self.claims(self.fns[fid]))):
            return False
        out = self.ov / "runs" / f"{fid}-acceptance.out"
        evidence = (f"{why} (found by {fid}, whose acceptance {', '.join(self.fns[fid].acceptance)} fails on it"
                    + (f"; its last output: {out.as_posix()}" if out.is_file() else "") + ")")
        self.set(fid, status="planned", depfixes=rec.get("depfixes", 0) + 1, blocked_on=dfid,
                 last_reason=f"waiting on {dfid}: {path}:{line} {why}"[:REASON_MAX])
        self.log(fid, "blocked_by", f"{path}:{line} in {module}: {why}"[:REASON_MAX])
        self.start_polish_fix(module, [{"file": path, "line": line, "rule": f"dependency defect found by {fid}",
                                        "evidence": evidence}], fid=dfid)
        return True

    def depfix_settled(self, dfid: str, ok: bool, reason: str = "") -> None:
        """A dependency fix landed or failed: its waiting function builds again, or parks with the reason."""
        for fid, rec in self.st["functions"].items():
            if rec.get("blocked_on") == dfid:
                self.set(fid, blocked_on=None)
                if not ok:
                    self.park(fid, f"dependency fix {dfid} failed: {reason}")
                elif rec["status"] == "landing":  # it waited on a main-flake fix: its reviewed branch lands now
                    self.land_queue.append(fid)

    def main_flake(self, fid: str, reason: str) -> bool:
        """land.py found the land's red test failing on clean main too (`main flake: test=.. path=.. module=..`):
        the branch did not cause it, so the test's owner module gets a fix pass (fid depfix-flake-<id>) and the
        function waits at landing, its branch reviewed, to land after the fix. One fix per test per run: the same
        test failing again parks the function with the reason. False when no owner module is found."""
        m = MAIN_FLAKE_RE.match(reason)
        if not m:
            return False
        test, path, module = (x if x != "?" else "" for x in m.groups())
        module = module or (self.owner_module(path) if path else "")
        dfid = f"{DEPFIX_PREFIX}flake-{fid}"
        if not module or dfid in self.jobs or dfid in self.polish_lands:
            return False
        self.log(fid, "main_flake", f"test={test} path={path or '?'} module={module}")
        if test in self.flake_tests:
            self.park(fid, f"main flake on {test} again after its fix this run: {reason}")
            return True
        self.flake_tests.add(test)
        self.set(fid, status="landing", blocked_on=dfid,
                 last_reason=f"waiting on {dfid}: {test} fails on clean main"[:REASON_MAX])
        self.start_polish_fix(module, [{"file": path or "?", "line": "?", "rule": f"main flake found by {fid}",
                                        "evidence": f"{test} fails on clean {self.main}: {reason}"}], fid=dfid)
        return True

    def to_review(self, job: Job, problem: str) -> None:
        """After a build or conflict pass: review when clean, a conflict pass on a pre-review merge conflict."""
        if problem == CONFLICT_SYNC:
            if not self.start_conflict(job.fn, f"{CONFLICT_SYNC} {self.main} into fn/{job.fn} before review"):
                self.fail(job.fn, f"{CONFLICT_SYNC} {self.main} before review; conflict_retries spent")
        elif problem:
            self.fail(job.fn, problem)
        else:
            self.start_review(job.fn, job.attempt)

    def sync_problem(self, fid: str) -> str:
        """Merge main into fn/<id> and rerun the gate in its worktree before review, so a land conflict means main
        moved in the seconds between. '' when clean, CONFLICT_SYNC on a merge conflict, else the failing gate."""
        wt = self.ov / "wt" / fid
        self.log(fid, "sync", f"merge {self.main} before review")
        p = run(["git", "merge", "-q", "--no-edit", "-m", f"{fid}: merge {self.main} before review", self.main], wt)
        if p.returncode != 0:
            run(["git", "merge", "--abort"], wt)
            return CONFLICT_SYNC
        return self.gate_problem(wt, fid)

    def slot(self, fid: str) -> int:
        """The parallel slot whose CARGO_TARGET_DIR fid's worktree commands reuse, so a worktree does not
        cold-build: fid keeps its slot while no other running job holds it, else takes the lowest free one. Two
        processes that run at once never share a slot."""
        held = {self.slots[j] for j in self.jobs if j != fid and j in self.slots}
        k = self.slots.get(fid)
        if k is None or k in held:
            k = next(i for i in range(len(held) + 1) if i not in held)
            self.slots[fid] = k
        return k

    def slot_env(self, fid: str) -> dict[str, str]:
        return {**os.environ, "CARGO_TARGET_DIR": str(self.ov / "target" / f"slot-{self.slot(fid)}")}

    def sh(self, cmd: str, wt: Path, fid: str = "") -> subprocess.CompletedProcess[str] | None:
        """One gate command in a worktree, on fid's slot target dir; None on timeout."""
        try:
            return subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=self.timeout_s, stdin=subprocess.DEVNULL,
                                  env=self.slot_env(fid or wt.name))
        except subprocess.TimeoutExpired:
            return None

    def gate_problem(self, wt: Path, fid: str = "") -> str:
        """The whole-tree gate commands land.py runs ([harness] pre_review names them; default all of them), run
        in the worktree on the merged branch. '' when all pass."""
        names = self.cfg["harness"].get("pre_review", land_mod.WHOLE_TREE)
        for name, cmd in self.gate_list(names):
            if name == "cycles":
                problem = self.cycles_problem(wt)
                if problem:
                    return problem
                continue
            cmd = land_mod.fill(cmd)
            p = self.sh(cmd, wt, fid)
            if p is None:
                return f"{name} timed out in the worktree after merging {self.main}: `{cmd}`"
            if not land_mod.passes(cmd, p.returncode):
                tail = " / ".join([ln.strip() for ln in (p.stdout + p.stderr).splitlines() if ln.strip()][-3:])
                return (f"{name} failed (exit {p.returncode}) in the worktree after merging {self.main}, before "
                        f"review: `{cmd}`: {tail}. Merge {self.main} and rerun the gate before you hand back")
        return self.acceptance_problem(wt, fid)

    def gate_list(self, names) -> list[tuple[str, str]]:
        """The pre-review commands: the named [commands] that are set, then [commands.extra], as land runs them."""
        return [(n, self.cfg["commands"][n]) for n in names if self.cfg["commands"].get(n)] + \
            list(self.cfg.get("extra", []))

    def acceptance_problem(self, wt: Path, fid: str) -> str:
        """The function's own acceptance tests, run before review: a red one would only fail at land, after a
        wasted review. The full output goes to a file the retry brief names. '' when green or not a function."""
        tmpl = self.cfg.get("acceptance") or self.cfg["commands"].get("acceptance")
        if fid not in self.fns or not tmpl or not self.cfg["harness"].get("pre_review_acceptance", True):
            return ""
        for node, cmd in land_mod.acceptance_cmds(tmpl, self.fns[fid].acceptance):
            p = self.sh(cmd, wt, fid)
            if p is None:
                return f"acceptance {node} timed out in the worktree after merging {self.main}: `{cmd}`"
            if p.returncode != 0:
                out = self.ov / "runs" / f"{fid}-acceptance.out"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(p.stdout + p.stderr, encoding="utf-8")
                tail = " / ".join([ln.strip() for ln in (p.stdout + p.stderr).splitlines() if ln.strip()][-8:])
                return (f"acceptance red in the worktree after merging {self.main}, before review: {node} "
                        f"(exit {p.returncode}): {tail} (full output: {out.as_posix()}). Make it pass, then hand back")
        return ""

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
        return ""  # the gate, cycles included, runs after the pre-review merge (sync_problem)

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
            self.to_review(job, self.sync_problem(job.fn))

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
        return {"standard_path": STANDARD.as_posix(), "standard": relevant_rules(text),
                "project_rules": self.project_rules(f)}

    def project_rules(self, f: Fn) -> str:
        """The brief section quoting each project rule the row's rules column cites, from the [paths] patterns and
        standards files on main; '' when the row cites none."""
        if not f.rules:
            return ""
        srcs = [str(self.cfg["paths"][k]) for k in RULE_PATHS if self.cfg["paths"].get(k)]
        defs: dict[str, str] = {}
        for rel in srcs:
            for k, v in rule_defs(self.read_main(rel) or "").items():
                defs.setdefault(k, v)
        where = ", ".join(f"`{x}`" for x in srcs) or "no [paths] patterns or standards in overhaul.toml"
        lines = [f"- {defs[r]}" if r in defs else f"- {r}: (not found in {where})" for r in f.rules]
        return (f"\n## Project rules this row cites (from {where})\n\n"
                "Every rule below must hold in the change, and the acceptance test proves each pattern id it names "
                "(checklist C30).\n\n" + "\n".join(lines) + "\n")

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
            "checklist_version": checklist()[0],
            "checklist": checklist_text(applicable_items(scope, bool(f.rules))),
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
        blocking = self.round_filter(fid, blocking, job.head)
        if not blocking:
            self.keep_gap(fid, gap)
            self.log(fid, "review_pass", detail, secs, tokens)
            self.land_queue.append(fid)
            self.set(fid, status="landing")
            return
        self.log(fid, "review_nos", f"{detail}: " + " / ".join(f"{n['id']} {where(n)}" for n in blocking),
                 secs, tokens)
        self.start_verify(fid, job, blocking, gap)

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

    def start_verify(self, fid: str, job: Job, blocking: list[dict], gap: str = "") -> None:
        path = self.verify_brief(fid, f"fn/{fid}", blocking, job.attempt)
        self.verify_meta[fid] = {"findings": blocking, "head": job.head, "gap": gap}
        self.start_job(fid, "verify", job.attempt, path, self.ov / "wt" / fid)
        self.jobs[fid].head = job.head

    def after_verify(self, job: Job) -> None:
        fid, secs = job.fn, time.monotonic() - job.started
        tokens, _, _ = read_result(job.out)
        self.reset_wt(fid, job.head)
        meta = self.verify_meta.pop(fid, {})
        findings, gap = meta.get("findings") or [], str(meta.get("gap") or "")
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
            self.keep_gap(fid, gap)
            self.log(fid, "review_pass", f"0 verified of {len(findings)} blocking NOs model={job.model}", secs, tokens)
            self.land_queue.append(fid)
            self.set(fid, status="landing")
            return
        if (all(n["id"] in DEDUP_IDS for n in real)
                and self.st["functions"][fid]["attempts"] + 1 > self.budget(fid)):
            note = " / ".join(f"[{n['id']} {where(n)}] {n.get('evidence', '')} {n.get('fix', '')}" for n in real)
            self.keep_gap(fid, f"{gap} / {note}" if gap else note)  # a duplicate never parks a working function
            self.log(fid, "review_pass", f"{len(real)} verified dedup NOs ({', '.join(n['id'] for n in real)}) "
                     f"recorded as plan gap on the last attempt model={job.model}", secs, tokens)
            self.land_queue.append(fid)
            self.set(fid, status="landing")
            return
        stuck = self.gap_nos(fid, gap, real)
        if stuck and gap and all(n["id"] in DEDUP_IDS for n in stuck) and len(stuck) == len(real):
            self.keep_gap(fid, gap)  # only another module's shared constant or helper can fix it: batch it
            self.log(fid, "review_pass", f"{len(real)} verified dedup NOs ({', '.join(n['id'] for n in stuck)}) "
                     f"recorded as plan gap model={job.model}", secs, tokens)
            self.land_queue.append(fid)
            self.set(fid, status="landing")
            return
        stuck = [n for n in stuck if n["id"] not in DEDUP_IDS]
        if stuck:
            notes = (f"PLAN-GAP: {gap} / " if gap else "") + " / ".join(
                f"[{n['id']} {where(n)}] {n.get('evidence', '')} {n.get('fix', '')}" for n in stuck)
            self.log(fid, "review_fail", f"plan gap: {len(stuck)} of {len(real)} verified blocking NOs need a change "
                     f"outside the allowed paths: {notes[:REASON_MAX]} model={job.model}", secs, tokens)
            self.park(fid, f"review asks for a change outside the allowed paths (plan gap): {notes}")
            return
        self.keep_gap(fid, gap)
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
    def footprint(self, fid: str) -> list[str]:
        """What a land touches: the function's claimed files plus every file its branch changes."""
        changed = run(["git", "diff", "--name-only", f"{self.main}...fn/{fid}"], self.repo).stdout.split()
        return [*self.claims(self.fns[fid]), *changed]

    def pump_land(self) -> bool:
        """Start the next land. With none in flight it is a plain land under the lock. Otherwise the first queued
        function whose footprint overlaps no in-flight land's starts stacked on them (land.py --stack): its gate
        runs alongside theirs on main + their branches, and it commits right after them. Overlapping lands wait."""
        flying = self.land_jobs()
        if not self.land_queue or len(flying) >= self.max_lands or any(j.kind != "land" for j in flying):
            return False
        fid = self.land_queue[0]
        if flying:
            taken = [self.footprint(j.fn) for j in flying]
            fid = next((q for q in self.land_queue
                        if not any(paths_overlap(self.footprint(q), t) for t in taken)), "")
            if not fid:
                return False
        self.land_queue.remove(fid)
        out = self.ov / "runs" / f"{fid}-a{self.st['functions'][fid]['attempts'] + 1}-land.out"
        # land.py takes .overhaul/lock itself (a stacked land only to commit), so drive.py holds nothing meanwhile.
        cmd = [sys.executable, str(self.scripts / "land.py"), "--repo", str(self.repo), "--fn", fid,
               "--main", self.main]
        if flying:
            cmd += ["--stack", ",".join(j.fn for j in flying)]
        used = {j.slot for j in flying}
        k = next(i for i in range(len(used) + 1) if i not in used)
        cmd += ["--target-dir", str(self.ov / "target" / f"land-{k}")]  # one per concurrent land, reused
        log = self.ov / "log.jsonl"
        log_at = log.stat().st_size if log.exists() else 0
        start = time.monotonic()
        job = Job(fid, "land", spawn(cmd, self.repo, out), out, start, start + 2 * self.timeout_s, 0, log_at=log_at,
                  slot=k)
        if flying:
            self.side_lands[fid] = job
            self.log(fid, "land_stacked", f"on {', '.join(j.fn for j in flying)}")
        else:
            self.land_job = job
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
        if reason.startswith(land_mod.MAIN_FLAKE) and self.main_flake(fid, reason):
            return  # a test that fails on clean main too: its owner gets a fix, the function lands after it
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
        if self.probe_waiting():
            return started
        if pol["status"] == "fix":
            for item in self.polish_ready():
                self.polish_queue.remove(item)
                self.start_polish_fix(*item)
                started = True
            if not self.polish_pending():
                return self.start_polish_audit(pol["round"] + 1)
        elif pol["status"] == "gaps":  # the plan-gaps pass runs alone; polish round 1 waits for it
            if not self.polish_pending() and not self.busy():
                return self.start_polish_audit(max(1, pol["round"]))
        elif not self.busy() and not self.polish_pending():  # idle, or an audit a previous drive left unfinished
            return self.start_gap_pass() or self.start_polish_audit(max(1, pol["round"]))
        return started

    def gap_modules(self, gaps: list[dict]) -> list[str]:
        """Every module a plan gap names (by name or path), plus the modules of the functions that recorded them."""
        named = {str(e.get("module") or "") for e in gaps} - {""}
        text = "\n".join(str(e.get("gap") or "") for e in gaps)
        for m in {f.module for f in self.order}:
            paths = module_paths(module_section(self.arch, m), m)
            if re.search(rf"(?<![\w]){re.escape(m)}(?![\w])", text) or any(p.rstrip("/") in text for p in paths):
                named.add(m)
        return sorted(named)

    def start_gap_pass(self) -> bool:
        """The plan gaps the function reviews recorded, as ONE fix pass that may touch every module they name
        (a shared constant changes in its owner and every caller together). Runs once, alone; False if none."""
        pol = self.polish_state()
        if pol.get("gaps") or pol["round"] or pol["status"] != "idle":
            return False
        gaps = self.read_gaps()
        if not gaps:
            return False
        modules = self.gap_modules(gaps)
        items = [{"text": "These are plan gaps the function reviews recorded: duplicated constants, messages or "
                          "helpers another module should own or export. For each one, export it from the owner "
                          "module and make every caller import it, all in this pass. No behaviour may change; skip "
                          "a gap that would change behaviour and say so in your hand-back.\n"}]
        items += [{"text": f"- [plan gap from {e.get('fn', '?')}, module {e.get('module', '?')}] {e['gap']}"}
                  for e in gaps]
        pol.update(gaps="started", status="gaps")
        self.save()
        self.log(GAPS_FID, "plan_gaps", f"start: {len(gaps)} gaps across {', '.join(modules)}")
        self.start_polish_fix(", ".join(modules), items, fid=GAPS_FID)
        return True

    def polish_claims(self, module: str) -> list[str]:
        out = []
        for m in module.split(", "):  # the plan-gaps pass names several modules
            section = module_section(self.arch, m)
            out += module_paths(section, m) + module_shared(section)
        return out

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
            proc = spawn([*base, f"Read and do {brief.as_posix()}"], cwd, out, self.slot_env(fid))
        except OSError as e:
            raise DriveError(f"cannot start the {kind} worker {base[0]}: {e}") from e
        start = time.monotonic()
        self.jobs[fid] = Job(fid, kind, proc, out, start, start + self.timeout_s, rnd, model=model)

    def polish_commands(self) -> str:
        lines = [f"- {name}: `{cmd}`" for name, cmd in self.cfg["commands"].items() if "{test}" not in cmd]
        return "\n".join(lines) or "(overhaul.toml has no whole-tree [commands])"

    def polish_allowed(self, module: str, fid: str = "") -> str:
        paths = [p for m in module.split(", ") for p in module_paths(module_section(self.arch, m), m)]
        tests = unit_dirs(self.cfg["commands"].get("test", "")) or ["tests/unit/"]
        return "\n".join([f"- `{p}`" for p in paths] + [f"- `{t}` (unit tests for this module only)" for t in tests]
                         + (["- `tests/property/` (add property tests; never edit a planned one)"]
                            if fid.startswith(PROBE_PREFIX) else []))

    def start_polish_audit(self, rnd: int) -> bool:
        """The fixed checklist over the whole tree plus the dedup role (template polish-audit.md)."""
        fid, pol = "polish-audit", self.polish_state()
        audit = self.ov / "polish" / f"audit-{rnd}.json"
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.unlink(missing_ok=True)
        modules = sorted({f.module for f in self.order})
        brief = render("polish-audit.md", {
            "scope": self.polish_scope(rnd), "round": str(rnd), "main": self.main, "audit_file": audit.as_posix(), "max_calls": str(self.max_calls),
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

    def polish_scope(self, rnd: int) -> str:
        """Round 1 audits the whole tree. A later round re-checks the previous round's verified NOs and audits only
        the files its fixes changed, like a function's re-review (trial 11: a whole-tree re-audit timed out twice)."""
        best, prev = self.polish_state().get("best"), self.ov / "polish" / f"checklist-{rnd - 1}.json"
        if rnd < 2 or not best or not prev.is_file():
            return "Round 1: the whole tree."
        changed = [f for f in git(self.repo, "diff", "--name-only", f"{best[2]}..{self.main}").split() if f]
        items = self.read_audit(prev) or []
        listed = "\n".join(f"- [{n.get('id')}] `{where(n)}`: {n.get('evidence') or ''}"[:400] for n in items)
        files = "\n".join(f"- `{f}`" for f in changed) or "- (none)"
        return (f"Round {rnd} is a re-check, not a new whole-tree audit. Do exactly two things:\n"
                f"1. For each verified NO of round {rnd - 1} below, check the fix: still NO (same item, same "
                f"evidence) or YES.\n{listed or '- (none)'}\n"
                f"2. Answer the checklist only for these files, changed by the fixes since round {rnd - 1} "
                f"(`git diff {best[2][:12]}..{self.main}`), and only for code the fixes touched:\n{files}\n"
                "Do not audit other files, write mutation or load harnesses, or re-measure the whole tree; "
                "the project's commands and the dedup greps on the changed code are enough.")

    @staticmethod
    def read_audit(path: Path) -> list[dict] | None:
        data = read_json(path)
        if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
            return None
        return data

    def polish_audit_failed(self, rnd: int, why: str, model: str, secs: float, tokens: int | None) -> None:
        pol = self.polish_state()
        self.log(None, "polish_audit", f"round {rnd} failed: {why} model={model}", secs, tokens)
        if pol.get("audit_retry") != rnd:  # one unreadable result must not void the round: audit it once more
            pol.update(status="idle", audit_retry=rnd)
            self.save()
            self.say(f"polish audit round {rnd} failed ({why}); running it once more")
            return
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
                if not self.retry_polish_fix(fid, real, meta.get("head", ""), job.attempt):
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

    def start_polish_fix(self, module: str, items: list[dict], fid: str = "") -> None:
        rnd = 0 if fid else self.polish_state()["round"]
        fid = fid or f"polish-r{rnd}-" + (re.sub(r"[^A-Za-z0-9._-]+", "-", module).strip("-.") or "misc")
        issues = "\n".join(i["text"] if "text" in i else f"- `{i.get('file', '?')}:{i.get('line', '?')}` "
                           f"[{i.get('rule', '?')}] {i.get('evidence') or i.get('issue') or ''}" for i in items)
        self.polish_meta[fid] = (module, issues)
        self.polish_tries.pop(fid, None)
        path = self.polish_fix_brief(fid, rnd, module, issues)
        self.start_polish_job(fid, "polish_fix", rnd, path, self.prepare_worktree(fid))
        self.log(fid, self.fix_event(fid), f"start {module}: {len(items)} issues model={self.jobs[fid].model}")
        self.say(f"polish fix {fid}")

    def polish_fix_brief(self, fid: str, rnd: int, module: str, issues: str) -> Path:
        brief = render("polish-fix-brief.md", {
            "id": fid, "round": str(rnd), "module": module, "branch": f"fn/{fid}", "max_calls": str(self.max_calls),
            "issues": issues, "allowed_paths": self.polish_allowed(module, fid), "policies": self.policies(),
            "commands": self.polish_commands()})
        path = self.ov / "briefs" / f"{fid}.md"
        path.write_text(brief, encoding="utf-8")
        return path

    def retry_polish_fix(self, fid: str, real: list[dict], head: str, rnd: int) -> bool:
        """Like a build retry: a fix whose review has verified blocking NOs gets FIX_RETRIES more passes on its own
        branch, carrying the NO list; the round rule then applies to its review. False once they are spent."""
        prev = self.polish_tries.get(fid) or {}
        n = prev.get("round", 0) + 1
        if n > FIX_RETRIES or fid not in self.polish_meta or not head:
            return False
        module, issues = self.polish_meta[fid]
        self.polish_tries[fid] = {"round": n, "head": head, "ids": sorted({x["id"] for x in real})}
        self.save()
        self.reset_wt(fid, head)
        listing = format_nos(real)
        path = self.polish_fix_brief(fid, rnd, module, issues + (
            f"\n\nRetry {n}: the branch keeps your last attempt, whose review found {len(real)} verified blocking "
            f"NOs on lines it changed. Fix every one, and keep the fixes above:\n{listing}"))
        self.start_polish_job(fid, "polish_fix", rnd, path, self.ov / "wt" / fid)
        self.log(fid, self.fix_event(fid), f"retry {n}/{FIX_RETRIES}: {len(real)} verified blocking NOs model="
                 f"{self.jobs[fid].model}: " + " / ".join(f"{x['id']} {where(x)}" for x in real))
        self.say(f"polish fix {fid}: retry {n}")
        return True

    def after_polish_fix(self, job: Job) -> None:
        tokens, turns, text = read_result(job.out)
        self.log(job.fn, self.fix_event(job.fn), f"built rc={job.proc.returncode} turns={turns}/{self.max_calls}",
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
            "id": job.fn, "branch": branch, "allowed_paths": self.polish_allowed(module, job.fn),
            "row": f"A polish fix of module `{module}`: no function row, and no behaviour may change. "
                   f"The issues it must fix:\n\n{issues}",
            "acceptance_source": "(none: a polish fix adds no function; land.py re-runs every proven acceptance test)",
            "review_file": review_file.as_posix(), "policies": self.policies(),
            "standard_path": STANDARD.as_posix(), "standard": relevant_rules(module + "\n" + issues),
            "checklist_path": CHECKLIST.as_posix(), "checklist_version": checklist()[0],
            "checklist": checklist_text(applicable_items(module + "\n" + issues)),
            "round_rule": self.polish_round_rule(job.fn),
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

    def polish_round_rule(self, fid: str) -> str:
        prev = self.polish_tries.get(fid)
        if not prev:
            return ("A polish fix: a NO blocks only when it points at a line this fix changed; NOs on unchanged code "
                    "are logged as review_overreach and do not block.")
        return (f"Retry {prev['round']} of a polish fix. The previous review's verified blocking NOs were "
                f"{', '.join(prev['ids']) or 'none'}. A NO now blocks only when (a) it is one of those items, still "
                "NO, or (b) it points at a line this retry changed (a regression). Any other NO is logged as "
                "review_overreach and does not block.")

    def after_polish_review(self, job: Job) -> None:
        """Only blocking NOs on lines the fix changed may stop it, and only once a verifier reproduces them."""
        fid = job.fn
        tokens, _, _ = read_result(job.out)
        self.reset_wt(fid, job.head)
        nos = None if job.timed_out else checklist_nos(read_json(self.ov / "reviews" / f"{fid}.json"))
        if nos is None:
            why = "reviewer timed out" if job.timed_out else "review file missing or not the checklist JSON shape"
            self.log(fid, self.fix_event(fid), f"review FAIL: {why} model={job.model}", time.monotonic() - job.started,
                     tokens)
            self.polish_failed(fid, f"review FAIL: {why}")
            return
        self.log_nits(fid, [n for n in nos if n["severity"] not in BLOCKING], "polish review")
        prev = self.polish_tries.get(fid)
        base = prev["head"] if prev else git(self.repo, "merge-base", self.main, job.head).strip()
        changed = changed_lines(self.repo, base, job.head)
        ids = set(prev["ids"]) if prev else set()
        blocking = [n for n in nos if n["severity"] in BLOCKING]
        mine = [n for n in blocking if n["id"] in ids or changed is None or in_changed(n, changed)]
        over = [n for n in blocking if n not in mine]
        if over:
            self.log(fid, "review_overreach", f"{len(over)} NOs on unchanged code, not blocking: "
                     + " / ".join(f"{n['id']} {where(n)}" for n in over))
        self.log(fid, self.fix_event(fid), f"review {'NO' if mine else 'PASS'}: {len(mine)} blocking NOs in changed code "
                 f"model={job.model}", time.monotonic() - job.started, tokens)
        if not mine:
            self.polish_lands.append(fid)
            return
        path = self.verify_brief(fid, f"fn/{fid}", mine, job.attempt)
        self.verify_meta[fid] = {"findings": mine, "head": job.head}
        self.start_polish_job(fid, "polish_verify", job.attempt, path, self.ov / "wt" / fid)

    def pump_polish_land(self) -> bool:
        if self.land_job or self.side_lands or self.land_queue or not self.polish_lands:
            return False
        fid = self.polish_lands.pop(0)
        out = self.ov / "runs" / f"{fid}-land.out"
        cmd = [sys.executable, str(self.scripts / "land.py"), "--repo", str(self.repo), "--fn", fid,
               "--main", self.main, "--polish", "--target-dir", str(self.ov / "target" / "land-0")]
        start = time.monotonic()
        self.land_job = Job(fid, "polish_land", spawn(cmd, self.repo, out), out, start,
                            start + 2 * self.timeout_s, 0, slot=0)
        return True

    def after_polish_land(self, job: Job) -> None:
        if job.proc.returncode == 0 and not job.timed_out:
            self.log(job.fn, self.fix_event(job.fn), f"landed {self.polish_meta[job.fn][0]}",
                     time.monotonic() - job.started)
            self.say(f"polish landed {job.fn}")
            self.probe_settled(job.fn, True)
            self.depfix_settled(job.fn, True)
            return
        out, err = read_text(job.out), read_text(job.out.with_suffix(".err"))
        self.polish_failed(job.fn, "land: " + ("land.py timed out" if job.timed_out else (
            last_line(out) or last_line(err) or f"land.py exit {job.proc.returncode}")))

    def polish_failed(self, fid: str, reason: str) -> None:
        """A fix pass that cannot land is dropped; the re-audit finds its issues again."""
        self.log(fid, self.fix_event(fid), f"failed: {reason[:REASON_MAX]}")
        self.say(f"polish {fid} failed: {reason}")
        self.probe_settled(fid, False)
        self.depfix_settled(fid, False, reason)

    def reap_polish(self, job: Job) -> None:
        if job is self.land_job:
            self.land_job = None
        else:
            del self.jobs[job.fn]
        {"polish_audit": self.after_polish_audit, "polish_verify": self.after_polish_verify,
         "polish_fix": self.after_polish_fix,
         "polish_review": self.after_polish_review, "polish_land": self.after_polish_land}[job.kind](job)

    # ---- probe (v3): mutation score and property counterexamples, one fix pass per module ----
    def probe_modules(self) -> dict[str, str]:
        """{probe fid: module} for every module that owns a function, in build order."""
        return {PROBE_PREFIX + (re.sub(r"[^A-Za-z0-9._-]+", "-", f.module).strip("-.") or "misc"): f.module
                for f in self.order}

    def probe_state(self) -> dict:
        """st["probe"][module]: status (mutating | fixing | one of PROBE_DONE), score, survivors."""
        return self.st.setdefault("probe", {})

    def probe_waiting(self) -> bool:
        """True while a module still owes its probe (polish waits for every probe)."""
        pst = self.probe_state()
        return self.probe_on and any(pst.get(m, {}).get("status") not in PROBE_DONE
                                     for m in self.probe_modules().values())

    def probe_step(self) -> bool:
        """Start mutate.py for each module whose functions are all proven; True if anything started."""
        if not self.probe_on:
            return False
        started = self.pump_polish_land()
        pst, recs = self.probe_state(), self.st["functions"]
        held = set(self.jobs) | set(self.polish_lands) | ({self.land_job.fn} if self.land_job else set())
        for fid, module in self.probe_modules().items():
            if pst.get(module, {}).get("status") in PROBE_DONE or fid in held:
                continue
            if any(recs[f.id]["status"] != "proven" for f in self.order if f.module == module):
                continue
            if len(self.jobs) >= self.max_parallel:
                break
            self.start_probe(fid, module)
            started = True
        return started

    def probe_tests(self, module: str) -> list[str]:
        """The module's unit test dir and its functions' property test files that exist on main. A Rust module (.rs
        files and no .py in its Paths) gives instead: its .rs files holding `#[cfg(test)]` (mutate.py runs --lib for a
        path under src/), each `<path>/tests` dir and its functions' tests/property/test_<id>.rs ([[test]] targets)."""
        paths = module_paths(module_section(self.arch, module), module)
        tree = run(["git", "ls-tree", "-r", "--name-only", self.main, "--", *paths], self.repo).stdout.split()
        if any(p.endswith(".rs") for p in tree) and not any(p.endswith(".py") for p in tree):
            grep = run(["git", "grep", "-l", "-F", "-e", "#[cfg(test)]", self.main, "--", *paths], self.repo)
            units = [ln.split(":", 1)[1] for ln in grep.stdout.splitlines() if ln.endswith(".rs") and ":" in ln]
            cands = units + [p.rstrip("/") + "/tests" for p in paths if not p.endswith(".rs")]
            cands += [land_mod.anchors.property_file(f.id, ".rs") for f in self.order if f.module == module]
        else:
            cands = [f"tests/unit/{module}"] + [land_mod.anchors.property_file(f.id) for f in self.order
                                                if f.module == module]
        return [c for c in cands
                if run(["git", "cat-file", "-e", f"{self.main}:{c}"], self.repo).returncode == 0]

    def start_probe(self, fid: str, module: str) -> None:
        pst, script = self.probe_state(), self.scripts / "mutate.py"
        tests = self.probe_tests(module)
        if not script.is_file():
            pst[module] = {"status": "skipped", "reason": f"no {script.name} in {self.scripts}"}
            self.log(fid, "probe_mutation", f"{module}: skipped, no {script.name}")
            self.save()
            return
        if not tests:
            self.probe_result(fid, module, {"score": 0.0, "mutants": 0, "survivors": [], "counterexamples": [
                f"module {module} has no unit tests (tests/unit/{module}/) and no property tests on main"]})
            return
        wt = self.prepare_worktree(fid)
        paths = module_paths(module_section(self.arch, module), module)
        cmd = [sys.executable, str(script), "--repo", str(wt), "--paths", *paths, "--tests", *tests,
               "--max-mutants", str(self.max_mutants)] + (["--cmd", self.probe_cmd] if self.probe_cmd else [])
        out = self.ov / "runs" / f"{fid}-mutate.out"
        start = time.monotonic()
        self.jobs[fid] = Job(fid, "probe_mutate", spawn(cmd, wt, out, self.slot_env(fid)), out, start,
                             start + 2 * self.timeout_s, 0)
        pst[module] = {**pst.get(module, {}), "status": "mutating"}
        self.save()
        self.say(f"probe {module}: mutating")

    def after_probe_mutate(self, job: Job) -> None:
        module = self.probe_modules()[job.fn]
        try:
            res = None if job.timed_out else json.loads(last_line(read_text(job.out)))
        except ValueError:
            res = None
        if not isinstance(res, dict):
            why = "mutate.py timed out" if job.timed_out else (
                f"mutate.py exit {job.proc.returncode}: " + last_line(read_text(job.out.with_suffix(".err"))))
            self.probe_state()[module] = {"status": "error", "reason": why[:REASON_MAX]}
            self.log(job.fn, "probe_mutation", f"{module}: error {why[:REASON_MAX]}", time.monotonic() - job.started)
            self.save()
            return
        self.probe_result(job.fn, module, res, time.monotonic() - job.started)

    def probe_result(self, fid: str, module: str, res: dict, secs: float = 0.0) -> None:
        """Log probe_mutation; below the floor (or with a counterexample) start the module's ONE fix pass."""
        score, survivors, cex = float(res.get("score") or 0.0), res.get("survivors") or [], res.get(
            "counterexamples") or []
        shown = "; ".join(f"{s.get('file')}:{s.get('line')} {s.get('op')} `{s.get('before')}` -> `{s.get('after')}`"
                          for s in survivors)
        self.log(fid, "probe_mutation", f"{module}: score {score} floor {self.mutation_floor} mutants "
                 f"{res.get('mutants', 0)} survivors {len(survivors)} counterexamples {len(cex)}: {shown}"[:REASON_MAX],
                 secs)
        fixes = int(self.probe_state().get(module, {}).get("fixes", 0))
        rec = {"score": score, "survivors": len(survivors), "counterexamples": len(cex), "fixes": fixes}
        if (score >= self.mutation_floor and not cex) or fixes >= PROBE_FIXES:
            ok = score >= self.mutation_floor and not cex
            self.probe_state()[module] = {**rec, "status": "done" if ok else "below_floor"}
            self.save()
            return
        items = [{"file": s.get("file"), "line": s.get("line"), "rule": f"surviving mutant {s.get('op')}",
                  "evidence": f"`{s.get('before')}` -> `{s.get('after')}` passes every test: add a unit or property "
                              "test that kills it, or fix the code if the mutant is the right behaviour"}
                 for s in survivors]
        items += [{"file": "tests/property/", "line": "?", "rule": "property counterexample",
                   "evidence": f"{c} (fix the code; a planned test is an anchor)"} for c in cex]
        self.probe_state()[module] = {**rec, "status": "fixing"}
        self.save()
        self.log(fid, "probe_fix", f"queued {module}: score {score} < floor {self.mutation_floor}, {len(items)} items")
        self.start_polish_fix(module, items, fid=fid)

    @staticmethod
    def fix_event(fid: str) -> str:
        if fid.startswith(DEPFIX_PREFIX):
            return "dep_fix"
        return "probe_fix" if fid.startswith(PROBE_PREFIX) else "polish_fix"

    def probe_settled(self, fid: str, ok: bool) -> None:
        """A probe fix landed or failed: the module's probe is over (one fix pass, never a second)."""
        module = self.probe_modules().get(fid) if fid.startswith(PROBE_PREFIX) else None
        if module and module in self.probe_state():
            pm = self.probe_state()[module]
            if ok:  # mutate the module again (probe_step), so the verdict reads the score after the fix
                pm.update(status="remeasure", fixes=int(pm.get("fixes", 0)) + 1)
            else:
                pm["status"] = "fix_failed"
            self.save()

    def lint_hook(self, wt: Path) -> Path:
        """Lint on every edit: a PostToolUse hook (lint_hook.py) in the worktree's .claude/settings.json,
        kept out of git through the repo's info/exclude. Skipped when the project tracks its own settings.json."""
        script = self.scripts / "lint_hook.py"
        target = wt / ".claude" / "settings.json"
        if not script.is_file() or run(["git", "ls-files", "--error-unmatch", ".claude/settings.json"],
                                       wt).returncode == 0:
            return wt
        hook = {"type": "command", "command": f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"',
                "timeout": 90}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "Edit|Write|MultiEdit",
                                                                 "hooks": [hook]}]}}, indent=2), encoding="utf-8")
        exclude = Path(git(wt, "rev-parse", "--git-path", "info/exclude").strip())
        exclude = exclude if exclude.is_absolute() else wt / exclude
        if "/.claude/settings.json" not in read_text(exclude):
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as fh:
                fh.write("\n/.claude/settings.json\n")
        return wt


    # ---- outcomes ----
    def fail(self, fid: str, reason: str) -> None:
        attempts = self.st["functions"][fid]["attempts"] + 1
        self.set(fid, status="planned", attempts=attempts, last_reason=reason[:REASON_MAX])
        self.say(f"{fid} attempt {attempts} failed: {reason}")
        if attempts > self.budget(fid):
            self.park(fid, reason)

    def waiting_on(self, fid: str) -> set[str]:
        """Every function whose build waits, directly or through others, on fid's real behaviour."""
        out: set[str] = set()
        todo = [fid]
        while todo:
            cur = todo.pop()
            for f in self.fns.values():
                if cur in f.blocking and f.id not in out:
                    out.add(f.id)
                    todo.append(f.id)
        return out

    def budget(self, fid: str) -> int:
        """Retries for one function: the configured count plus one per 5 functions waiting on it (at most 2 more),
        since parking a function others need stalls all of them behind one manager round."""
        if fid not in self.fns:
            return self.retries
        return self.retries + min(2, len(self.waiting_on(fid)) // 5)

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
        jobs = [*self.jobs.values(), *self.land_jobs()]
        for job in jobs:
            kill_tree(job.proc)
        for fid in [*(j.fn for j in jobs), *self.land_queue]:
            rec = self.st["functions"].get(fid)
            if rec and rec["status"] in IN_FLIGHT:
                rec["status"] = "planned"
        self.jobs.clear()
        self.land_queue.clear()
        self.land_job = None
        self.side_lands.clear()
        try:
            self.save(5.0)
        except (OSError, DriveError):
            pass
        if jobs:
            self.say(f"stopped; killed {len(jobs)} worker tree(s)")

    # ---- manager commands ----
    def reset(self, fid: str) -> None:
        """A manager's plan fix sends fid back to planned. At most PLAN_FIXES per function, so a function that
        keeps failing stops the build with a report instead of looping through manager rounds."""
        resets = int(self.st["functions"][fid].get("resets", 0))
        if resets >= PLAN_FIXES:
            raise DriveError(f"{fid} already had {resets} plan fixes and failed again: stop and report it "
                             f"to the owner as the deciding item (last reason: "
                             f"{self.st['functions'][fid].get('last_reason', '')[:300]})")
        self.set(fid, status="planned", attempts=0, conflicts=0, last_reason="", resets=resets + 1)
        if self.dirty:
            self.flush()
        self.log(fid, "cycle", "reset to planned, attempts 0")
        self.say(f"reset {fid}")

    def repolish(self) -> None:
        """--repolish: the next run audits again with the current checklist (a new item, a bumped version), as a
        fresh polish phase of at most polish_rounds fix rounds. Old rounds stay in history and the plan-gaps pass
        does not rerun. Bounded too: at most polish_rounds repolishes per build."""
        if self.polish_rounds < 1:
            raise DriveError("[limits] polish_rounds is 0: there is no polish to run again")
        old = self.st.get("polish") or {}
        if old.get("status", "idle") not in ("idle", "done", "failed"):
            raise DriveError(f"polish is {old.get('status')} (round {old.get('round')}): let it finish first")
        done = int(old.get("repolishes", 0))
        if done >= self.polish_rounds:
            raise DriveError(f"already repolished {done} times ([limits] polish_rounds {self.polish_rounds}): "
                             "report the issues left in .overhaul/polish/ to the owner instead")
        new = {"round": 0, "status": "idle", "issues": None, "history": list(old.get("history") or []),
               "repolishes": done + 1, "checklist": checklist()[0]}
        if old.get("gaps"):
            new["gaps"] = old["gaps"]
        self.st["polish"] = new
        for ph in (self.st.get("polish_phases") or {}).values():  # per-phase polish: audit each phase again
            ph.update(round=0, status="idle", issues=None, best=None, audit_retry=None)
        self.dirty = True
        self.flush()
        self.log(None, "polish_repolish", f"polish reset ({done + 1} of {self.polish_rounds}), checklist "
                 f"{checklist()[0]}")
        self.say(f"repolish: the next run audits again with checklist {checklist()[0]}")

    def mark_proven(self, fid: str, evidence: Path) -> None:
        if not evidence.is_file() or not read_text(evidence).strip():
            raise DriveError(f"evidence file missing or empty: {evidence}")
        if not acquire_lock(self.repo, LOCK_WAIT_S, "drive.py --mark-proven"):
            raise DriveError(f"lock held by {lock_holder(self.repo)}")
        try:  # the row's status cell, committed on main like a land's
            land_mod.commit_status(str(self.repo), self.main, self.cfg["paths"]["functions"], fid,
                                   f"mark {fid}: proven (manager; evidence {evidence.name})")
        except land_mod.LandFail as e:
            raise DriveError(str(e)) from e
        finally:
            release_lock(self.repo)
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
    ap.add_argument("--repolish", action="store_true",
                    help="reset the polish state so the next run audits again with the current checklist, then exit")
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
        if args.repolish:
            drv.repolish()
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
