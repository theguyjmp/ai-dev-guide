#!/usr/bin/env python3
"""Overhaul v2 harness: build, review and land every function in spec/FUNCTIONS.md.

A READY function (planned, its `behaviour` deps proven, retries left, no in-flight function whose files overlap
its own) gets a worktree at .overhaul/wt/<id> on fn/<id> from main, a brief from templates/, and a `claude -p`
builder. A dep marked `stub` in the FUNCTIONS.md `needs` column does not block: the plan commit landed its interface
stub, so the dependent builds against that contract at once. A finished build merges main into its branch and reruns
the gate in its worktree (a conflict there goes to the conflict-fix pass), then gets a blind reviewer; a PASS goes to
land.py. Up to [harness] land_parallel lands whose footprints (files, owning and touched modules) do not overlap run at once
(land.py --parallel: each gates on main outside the lock, commits under it, and re-gates only what newly landed work
affects). A land over [harness] land_timeout_min is requeued once uncharged, then fails. A failure retries from main
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

Live re-plan (owner 2026-11-09): supervise.py lands plan fixes on main while drive runs, then writes
.overhaul/requests/reset-<id>. At the next cycle boundary (looked for every REQUEST_POLL_S) drive re-reads the plan on
main (new and changed rows, no restart; refused with event reload_refused if plan_validate static fails) and applies
each request exactly like --reset (event reset_applied; a third reset is refused, event reset_refused), then removes it.
A reload-<round> request (every fixed row was replaced by new ones) only reloads.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
from concurrent.futures import Future, ThreadPoolExecutor
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
import drivelock  # noqa: E402  beside this script: one drive per repo, orphans, slot markers
import feasibility  # noqa: E402  beside this script: a row is checked buildable before its first build

import index_api  # noqa: E402  beside this script: the capability INDEX and the semantic duplicate gate
import land as land_mod  # noqa: E402  the lock protocol is land.py's
import mainred  # noqa: E402  beside this script: main red, systemic failures, dead depfixes
import memcap  # noqa: E402  beside this script: [limits] proc_mem_gb
import periodic  # noqa: E402  beside this script: [gates.periodic]
import priority  # noqa: E402  beside this script: Windows process priority classes
import remote  # noqa: E402  beside this script: [harness.remote] builder slots on a second machine
import router  # noqa: E402  beside this script: failure classes routed to their fix
import stack  # noqa: E402  beside this script: [harness] stacked_starts, builds on reviewed dep branches
import worker  # noqa: E402  beside this script: lean claude launch, stream-json watch
os.environ.setdefault("PROPTEST_DISABLE_FAILURE_PERSISTENCE", "1")  # no .proptest-regressions files (vc1)
os.environ.setdefault("PROPTEST_RNG_SEED", "0")  # fixed seed: a property green at land stays green on main (vc1)
os.environ.setdefault("OVERHAUL_HARNESS", "drive")  # the main guard (guard/reference-transaction) lets only the harness move main's code

HERE = Path(os.path.abspath(__file__)).parent
TEMPLATES = HERE / "templates"
STANDARD = HERE.parent / "references" / "quality-standard.md"
AUDIT_BRIEF = TEMPLATES / "audit-brief.md"
CHECKLIST = HERE.parent / "references" / "checklist.md"
ITEM_LINE = re.compile(r"- ([A-Z]\d+) \[(P0|P1|nit)\] ([^|]*)\|([^|]+)\|(.+)")
BLOCKING = ("P0", "P1")
NITS_MAX = 5  # nits per pass: logged, never dispatched
POLISH_ROUNDS_MAX = 3  # pass E: at most 3 checklist fix rounds (default 3), whatever [limits] polish_rounds says
BASE_RULES = (2, 3, 8, 10, 13, 16, 17, 18, 19, 20, 21, 24, 25, 26, 27)
RULE_TRIGGERS = (  # words in a function's row, module section or acceptance test -> the rules they bring in
    (r"http|server|web|socket|request|port|url", (33, 35)),
    (r"store|json|file|save|write|persist|database|\bdb\b|record", (23, 29, 30, 31, 32, 33, 34)),
    (r"lock", (34,)),
    (r"csv|export", (31, 36)),
    (r"output|--out\b|save as|write to", (36,)),  # a user-named output target must not clobber (C33)
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
ID_LISTS = ("concurrent writers", "boundaries", "multi-store writes")  # Policies lines that list function ids
DEFERRED = "deferred"  # FUNCTIONS status of a row planned later (rolling plan): drive never starts it
POLISH_KINDS = ("polish_audit", "polish_verify", "polish_fix", "polish_review", "polish_land", "arch_audit")
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
WIDTH_RED_MIN = 30  # [harness] width_red_min: ready rows under free slots this long is `width red`
WIDTH_EVERY_S = 10  # width_step runs at most this often (startable() walks the plan)
WIDTH_NAMES = 3  # the width red line names this many rows that unblock the most
GATE_PRIORITY = priority.popen_kwargs(priority.NORMAL)  # the pre-review gate: above the builders
REQUEST_POLL_S = 30.0  # how often the loop looks for .overhaul/requests/reset-<id> (live re-plan)
REQUESTS_DIR = "requests"
# watchdog.py requests drive honours at the same cycle boundary (owner 2026-11-10: the heartbeat must start what is
# due and not happening): audit-phase-<N> runs phase N's polish + arch audit now (again if it already ran), arch-sweep
# runs the whole-tree sweep now, periodic-<gate> re-arms a red gate's bisect and one more depfix (PERIODIC_REARMS).
HARNESS_REQUESTS = ("audit-phase-", "arch-sweep", "periodic-")
PERIODIC_REARMS = 2
LOCK_WAIT_S = 120.0  # a write that must happen now waits this long for land.py's lock
# [limits] min_free_gb: under it, cargo's incremental dirs under .overhaul/target are pruned and no build starts
# (pilot2026-11-09: 20 slot target dirs of ~10 GB filled Z: to 0 GB and ENOSPC killed the drive)
MIN_FREE_GB = 40.0
# [limits] prune_at_gb: under it the same dirs are pruned with builds still starting, so min_free_gb is not reached
# (pilot2026-11-10: the 40 GB floor blocked every local build); a job's own slot is pruned when the job ends
PRUNE_AT_GB = 80.0
# [harness] detach_builders (default true): a local build runs outside drive's kill-on-close job (worker.py launcher,
# pid file .overhaul/jobs/<id>.json) and a restarted drive re-adopts it (pilot2026-11-09: 73 of 248 builds killed
# by restarts). Only the owner's stop (`drive.py --stop`, Ctrl-C) kills it.
DETACH_KINDS = ("build",)
BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
INCREMENTAL = ("*/debug/incremental/*", "*/release/incremental/*")
HELPERS_TOP = 40  # [harness] index_top: INDEX lines a build or review brief lists, ranked by purpose
HELPERS_SHARE = 8  # those lines take at most 1/HELPERS_SHARE of brief_max_tokens
DEFAULTS = {
    "limits": {"builder_max_calls": 60, "brief_max_tokens": 40000,
               "retries_per_function": 1, "conflict_retries": 2, "builder_timeout_min": 30,
               "polish_rounds": 3, "fix_retries": 2, "proc_mem_gb": 12},
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
FIX_RETRIES = 2  # default [limits] fix_retries: retry passes of a probe or polish fix whose review has verified NOs
DUP_IDS = ("C03", "C05")  # checklist duplication items: a cross-module one goes to the caller module (pass E)
# Phase audit (owner 2026-11-09): the items of templates/arch-audit.md and their severity. A01 (one capability
# in two modules) and A02 (a module reaching around its owner's API) route like DUP_IDS: the caller gets the fix.
ARCH_ITEMS = {"A01": "P1", "A02": "P1", "A03": "P0", "A04": "P1", "A05": "P1"}
ARCH_DUP_IDS = ("A01", "A02")
PHASE_AUDIT_EVERY = 15  # default [harness] phase_audit_every: a phase audit per N of its rows proven since the last
ARCH_EVERY_LANDS = 25  # default [harness] arch_audit_every_lands: a whole-tree arch audit (the sweep) per N lands
ARCH_FID, SWEEP_FID, SWEEP_VERIFY = "arch-audit", "arch-sweep", "arch-verify"


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
    stream: worker.Stream | None = None  # a stream-json worker's live events (worker.py)
    idle: str = ""  # why the no-event watchdog killed it
    host: str = ""  # a build on a [harness.remote] slot: the host's name; "" = this machine
    calls: int = 0  # its --max-turns when not builder_max_calls (a build from a feasibility work packet)


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
    project-rules item (C30) only for a row that cites project rules; whole-tree (W) items only for None, or for a
    text that names the item (the review of a polish fix of that finding)."""
    items = [i for i in checklist()[1].values() if project_rules or not i.project]
    if text is None:
        return items
    want = wanted_rules(text)
    return [i for i in items if (i.whole and re.search(rf"\b{i.id}\b", text))  # the review of a polish fix of a W finding
            or (not i.whole and (not i.rules or want.intersection(i.rules)))]


def checklist_text(items: list[Item]) -> str:
    whole = " (whole tree: judge it on the whole tree, not only the scope)"
    lines = [f"- {i.id} [{i.severity}]{whole if i.whole else ''} {i.question} Decide: {i.decide}" for i in items]
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
        nos.append({**e, "id": cid, "severity": known[cid].severity if cid in known else ARCH_ITEMS.get(cid, "P1")})
    return nos


def slug(module: str) -> str:
    """A module name as a fix pass fid suffix."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", module).strip("-.") or "misc"


def where(n: dict) -> str:
    return f"{n.get('file', '?')}:{n.get('line', '?')}"


def format_nos(nos: list[dict]) -> str:
    return "\n".join(f"- [{n['id']} {n['severity']}] `{where(n)}` {n.get('evidence') or n.get('issue') or ''}"
                     + (f" -> {n['fix']}" if n.get("fix") else "")
                     + (f" (existing item to reuse instead: `{n['other']}`)" if n.get("other") else "") for n in nos)


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


def spawn(cmd: list[str], cwd: Path, out: Path, env: dict[str, str] | None = None,
          detach: bool = False) -> subprocess.Popen:
    """Start a worker in its own process group; stdout to out, stderr beside it; env replaces the environment.
    detach (Windows): also out of drive's job (CREATE_BREAKAWAY_FROM_JOB); OSError when a job above refuses it."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | spawn_priority(env) | (BREAKAWAY if detach else 0) \
        if os.name == "nt" else 0
    group = {"creationflags": flags} if os.name == "nt" else {"start_new_session": True}
    with open(out, "wb") as fo, open(out.with_suffix(".err"), "wb") as fe:
        return subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, env=env, **group)


def spawn_priority(env: dict[str, str] | None) -> int:
    """Windows priority class for spawn: slot work (a builder, reviewer, fix agent or probe, given its slot_env)
    runs BELOW_NORMAL and its checks inherit it; land.py (no env) runs NORMAL and lifts its own acceptance."""
    return priority.flag(priority.BELOW_NORMAL if env is not None else priority.NORMAL)


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
    """(tokens, turns, final text) from a `claude -p --output-format json` result, or the last `result` event of
    a stream-json run (None, None, "" when a killed stream never sent one)."""
    raw = read_text(path)
    data: object = None
    for chunk in (raw, *reversed(raw.splitlines())):
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if not (isinstance(data, dict) and data.get("type") not in (None, "result")):
            break
    if isinstance(data, dict) and data.get("type") not in (None, "result"):  # a stream killed before its result
        live = worker.Stream(path, 0.0)
        live.poll(0.0)
        return live.tokens or None, live.turns or None, ""
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


def module_deps(arch: str, module: str) -> list[str]:
    """The modules a module's `Depends on:` line in ARCHITECTURE.md names (none for `-` or `none`)."""
    m = re.search(r"^[\s>*-]*\**\s*Depends on\s*:\s*\**\s*(.*)$", module_section(arch, module), re.I | re.M)
    deps = [x.strip().strip("`*").strip() for x in re.split(r"[,;]", m.group(1))] if m else []
    return [d for d in dict.fromkeys(deps) if d not in ("", "-", "none", module)]


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


VIS_PREFIX = re.compile(r"^(?:pub(?:\s*\([^)]*\))?\s+|export\s+)")  # Rust pub / pub(crate) / pub(in ..), TS/JS export
REEXPORT = re.compile(r"^(?:pub(?:\s*\([^)]*\))?\s+use\s+[^;]+;"
                      r"|export\s*(?:\*|\{[^}]*\})\s*(?:from\s*['\"][^'\"]+['\"])?\s*;?)$")


def visibility_only(diff: str) -> bool:
    """Whether a `git diff -U0` of one file only changes visibility (item 8, #19): each hunk's added lines, with
    pure re-export lines (`pub use ..;`, `export {..} from '..'`) left out, equal its removed lines once a leading
    `pub`/`pub(..)`/`export` is stripped. A new or deleted file, or any other edit, is not visibility-only."""
    if re.search(r"^(?:new|deleted) file mode", diff, re.M):
        return False
    hunks = re.split(r"^@@[^\n]*\n", diff, flags=re.M)[1:]
    if not hunks:
        return False
    for hunk in hunks:
        lines = [ln for ln in hunk.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
        removed = [VIS_PREFIX.sub("", ln[1:].strip()) for ln in lines if ln[0] == "-"]
        added = [ln[1:].strip() for ln in lines if ln[0] == "+"]
        if [VIS_PREFIX.sub("", a) for a in added if not REEXPORT.match(a)] != removed:
            return False
    return True


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
    cfg["tiers"] = land_mod.tier_globs(raw)  # [tiers]: proven tests that rerun only inside a footprint, as at land
    cfg["plan"] = raw.get("plan") if isinstance(raw.get("plan"), dict) else {}  # plan_validate's keys, read-only
    cfg["periodic"] = periodic.config(raw)  # [gates.periodic]: slow gates on main in the background (X2, X13)
    return cfg


class Driver(mainred.MainRed):
    """One run of the harness over one repo."""

    def __init__(self, repo: Path, cfg: dict):
        lim, har = cfg["limits"], cfg["harness"]
        self.repo, self.cfg = repo, cfg
        # [harness] max_parallel, else the older [limits] key, else 6
        self.max_parallel = max(1, int(har.get("max_parallel") or lim.get("max_parallel") or 6))
        # [harness] land_parallel (the older max_lands): lands at once; overlapping footprints never
        self.max_lands = max(1, int(har.get("land_parallel") or har.get("max_lands") or 3))
        # [harness] gate_parallel: pre-review merges + gates (whole-tree checks, own acceptance, footprint) run in
        # worker threads, so the main loop keeps reaping, reviewing and landing while they run (pilot2026-11-10:
        # run serially in the loop at ~3.5 min each, 11 built rows queued for review and no land started for 35 min)
        self.gate_parallel = max(1, int(har.get("gate_parallel") or 4))
        self.gate_pool: ThreadPoolExecutor | None = None
        self.gating: dict[str, tuple[Job, Future]] = {}
        self.land_timeout_s = float(har.get("land_timeout_min", land_mod.LAND_TIMEOUT_MIN) or 0) * 60
        self.max_calls = int(lim["builder_max_calls"])
        self.max_tokens = int(lim["brief_max_tokens"])
        self.dup_semantic_min = float(har.get("dup_semantic_min", index_api.DUP_SEMANTIC_MIN))
        self.index_top = int(har.get("index_top", HELPERS_TOP))
        self.retries = int(lim["retries_per_function"])
        self.conflict_retries = max(0, int(lim["conflict_retries"]))
        self.timeout_s = float(lim["builder_timeout_min"]) * 60
        self.heartbeat_s = float(har.get("heartbeat_min", 10)) * 60
        self.idle_min = float(har.get("worker_idle_min", worker.IDLE_MIN))  # 0: no no-event watchdog
        self.main = str(har.get("main_branch", "main"))
        self.builder_model = str(har.get("builder_model") or "")  # also used by conflict passes
        self.review_model = str(har.get("review_model") or "")
        self.audit_model = str(har.get("audit_model") or "opus")  # the final judge's model: one bar
        self.polish_rounds = min(int(lim["polish_rounds"]), POLISH_ROUNDS_MAX)
        self.fix_retries = max(0, int(lim.get("fix_retries", FIX_RETRIES)))
        self.verify_model = str(har.get("verify_model") or self.review_model)  # a cheap model is fine
        self.verify_meta: dict[str, dict] = {}  # fid -> the findings a verifier is checking, and their context
        self.polish_queue: list[tuple[str, list[dict]]] = []  # (module, issues) fix passes waiting for a slot
        self.polish_lands: list[str] = []
        self.polish_meta: dict[str, tuple[str, str]] = {}  # fix fid -> (module(s), issues text)
        self.polish_owners: dict[str, list[str]] = {}  # caller module -> owners of its cross-module dups this round
        self.polish_after: dict[str, list[str]] = {}  # caller module -> owners whose own fix must land first
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
        self.side_lands: dict[str, Job] = {}  # lands started beside the one in flight (land.py --parallel)
        self.slots: dict[str, int] = {}  # fid -> k of the CARGO_TARGET_DIR .overhaul/target/slot-<k> it reuses
        self.flake_tests: set[str] = set()  # main-flake tests already sent to a fix this run (one fix per test)
        self.barrier = bool(har.get("phase_barrier", True))  # phase N+1 waits until every phase <=N is proven
        self.phase_polish = bool(har.get("phase_polish", True))  # item 8: gaps, probes and polish per phase too
        self.phase_audit = bool(har.get("phase_audit", True))  # the same per-phase polish beside builds, no barrier
        self.arch_every = max(0, int(har.get("arch_audit_every_lands", ARCH_EVERY_LANDS)))
        self.phase_audit_every = max(0, int(har.get("phase_audit_every", PHASE_AUDIT_EVERY)))
        self.arch_meta: dict[str, dict] = {}  # arch audit fid -> {"phase": n, "file": path}
        self.fns: dict[str, Fn] = {}
        self.deferred: set[str] = set()  # rows planned later: never built, counted apart
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
        self.periodic = periodic.Periodic(self, cfg.get("periodic") or {})
        self.red_init(har)
        self.last_request_check = 0.0  # live re-plan: .overhaul/requests/ is looked at every REQUEST_POLL_S
        self.last_harness_request = 0.0  # watchdog requests (HARNESS_REQUESTS), same cadence
        self.refused_plan = ""  # the main sha whose plan failed plan_validate: not reloaded again
        self.min_free_gb = float(lim.get("min_free_gb", MIN_FREE_GB) or 0)  # 0: no disk guard
        self.prune_at_gb = float(lim.get("prune_at_gb", PRUNE_AT_GB) or 0)  # 0: prune only under min_free_gb
        self.detach = bool(har.get("detach_builders", True))
        self.detach_off = False  # a job above drive's refused breakaway once: builders run attached from then on
        self.adopt: dict[str, dict] = {}  # fid -> pid file of a live detached build a previous drive left (load)
        self.width_red_s = float(har.get("width_red_min", WIDTH_RED_MIN) or 0) * 60
        self.width_since: float | None = None  # monotonic time ready rows first fell under free slots
        self.last_width = float("-inf")
        self.wait_cache: tuple[object, dict[str, int]] = (None, {})
        self.remote = remote.Pool.from_harness(har, self.log, self.say)  # slots on top of max_parallel
        self.stacked = bool(har.get("stacked_starts", False))  # rows start on deps' reviewed branches (stack.py)
        self.feas = feasibility.Gate(self)  # [harness] feasibility: checked before a row's first build

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
        """Write state.json now, waiting for land.py to release the lock. A live land may hold it for minutes (its
        merged-tree regate with land_parallel lands; the pilot project 2026-11-10 02:15 the old 120 s wait killed the drive
        twice and stopped the run), so it waits up to a land's own timeout plus 10 min; a dead holder's lock is
        broken by acquire_lock."""
        deadline = time.monotonic() + max(LOCK_WAIT_S, getattr(self, "land_timeout_s", 0.0) + 600)
        while not self.save(LOCK_WAIT_S):
            if time.monotonic() >= deadline:
                raise DriveError(f"cannot write {self.state_path.name}: {lock_path(self.repo).as_posix()} is held "
                                 f"by {lock_holder(self.repo) or '?'} (delete it if that process is gone)")
            self.log(None, "state_lock_wait", f"{lock_holder(self.repo) or '?'} holds the land lock; still waiting")

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
        self.deferred = {f.id for f in fns if f.status == DEFERRED}
        fns = [f for f in fns if f.id not in self.deferred]
        if not fns:
            raise DriveError(f"{rel} has only deferred rows; plan a phase first")
        self.order = sorted(fns, key=phase_key)
        self.fns = {f.id: f for f in fns}
        self.arch = self.read_main(arch) or ""
        self.on_main = set(git(self.repo, "ls-tree", "-r", "--name-only", self.main).splitlines())
        prot = {"tests/acceptance/", "overhaul.toml", rel, arch}
        prot |= {f"{Path(p).parent.as_posix()}/" for p in [rel, arch] + [acc_file(a) for f in fns for a in f.acceptance]}
        self.protected = tuple(sorted(p for p in prot if p != "./"))
        self.st = self.disk_state()
        self.st.pop("main_red", None)  # measured again this run (mainred.py)
        self.st.pop("systemic", None)
        recs = self.st["functions"]
        for fid in [k for k in recs if k not in self.fns]:
            del recs[fid]
        for fid, job in drivelock.read_jobs(self.ov).items():
            rec = recs.get(fid) or {}
            if (fid in self.fns and self.fns[fid].status != "proven" and drivelock.job_live(job)
                    and "building" in (rec.get("status"), rec.get("resume"))):
                self.adopt[fid] = job  # still running: re-adopted in single_start, never committed over or reset
            else:
                drivelock.drop_job(self.ov, fid)  # ended (or its row went): the normal resume below
        for f in self.order:
            rec = recs.setdefault(f.id, {"status": "planned", "attempts": 0, "conflicts": 0, "last_reason": "",
                                         "branch": f"fn/{f.id}", "updated": now_iso()})
            stage = rec.pop("resume", "")
            if f.id in self.adopt:
                rec["status"] = "building"
                continue
            if f.status == "proven":
                rec["status"] = "proven"
            elif rec["status"] in IN_FLIGHT or (stage and rec["status"] == "planned"):
                self.resume_mark(f.id, rec, stage or rec["status"])  # a previous drive stopped mid-flight
            rec.pop("blocked_on", None)  # its dependency fix died with the previous drive
        self.flush()

    def plan_problem(self, f: Fn) -> str:
        if not ID_RE.fullmatch(f.id):
            return f"id {f.id!r} cannot name a branch; use letters, digits, . _ -"
        later = [d for d in f.deps if d in self.deferred]
        if later:
            return f"deps are deferred (planned later): {', '.join(later)}; defer {f.id} too, or plan them now"
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

    # ---- resume: a restart keeps the work on fn/<id> (pilot2026-11-09: 87 of 324 builds never ended, every
    # restart turned them back to planned and `worktree add -B` reset their branches to main) ----
    def ahead(self, fid: str) -> int:
        """Commits on fn/<id> that main does not have; 0 when the branch is missing."""
        p = run(["git", "rev-list", "--count", f"{self.main}..fn/{fid}"], self.repo)
        out = p.stdout.strip()
        return int(out) if p.returncode == 0 and out.isdigit() else 0

    def commit_leftovers(self, fid: str) -> None:
        """A builder killed mid-edit: commit what its worktree holds on fn/<id>, so the resumed builder keeps it."""
        wt, branch = self.ov / "wt" / fid, f"fn/{fid}"
        if not (wt / ".git").is_file():
            return
        try:
            if git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() != branch:
                return
            if run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], wt).returncode == 0:
                run(["git", "merge", "--abort"], wt)  # a pre-review merge the stop cut short
            if git(wt, "status", "--porcelain").strip():
                git(wt, "add", "-A")
                git(wt, "commit", "-q", "-m", f"{fid}: work in progress when the drive stopped (committed by drive.py)")
        except DriveError:
            pass

    def resume_mark(self, fid: str, rec: dict, stage: str) -> None:
        """A function the previous drive left at stage (building, review or landing). With commits on fn/<id> it
        resumes there: review and landing go back to review and the land queue (resume_step), building restarts its
        builder on the same branch and worktree (start_build). A branch with no commits starts over."""
        if stage == "building":
            self.commit_leftovers(fid)
        n = self.ahead(fid) if stage in IN_FLIGHT else 0
        if not n:
            rec["status"] = "planned"
            self.log(fid, "resume", f"{stage} when the last drive stopped; fn/{fid} has no commits ahead of "
                     f"{self.main}: starts over")
            return
        rec["status"], rec["resume"] = ("planned" if stage == "building" else stage), stage
        self.log(fid, "resume", f"{stage} when the last drive stopped; fn/{fid} keeps {n} commits: resumes at {stage}")

    def resume_worktree(self, fid: str) -> Path:
        """fn/<id>'s worktree on its own branch as the stopped drive left it: never reset to main."""
        wt, branch = self.ov / "wt" / fid, f"fn/{fid}"
        if (wt / ".git").is_file():
            try:
                top = git(wt, "rev-parse", "--show-toplevel").strip()
                if os.path.normcase(os.path.abspath(top)) == os.path.normcase(os.path.abspath(wt)):
                    if git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() != branch:
                        git(wt, "checkout", "-q", "-f", branch)
                    return self.lint_hook(wt)
            except DriveError:
                pass
        if wt.exists():
            shutil.rmtree(wt, onexc=force_remove)
        git(self.repo, "worktree", "prune")
        git(self.repo, "worktree", "add", "-q", "-f", str(wt), branch)
        return self.lint_hook(wt)

    def resume_step(self) -> None:
        """At start: functions resumed at review or landing go straight back to review or the land queue."""
        for f in self.order:
            rec = self.st["functions"][f.id]
            stage = rec.get("resume")
            if stage not in ("review", "landing") or rec["status"] != stage:
                continue
            rec.pop("resume")
            try:
                self.resume_worktree(f.id)
            except DriveError as e:
                self.set(f.id, status="planned")
                self.log(f.id, "resume", f"worktree for fn/{f.id} failed ({e}): starts over")
                continue
            if stage == "review":
                self.start_review(f.id, rec["attempts"] + 1)
            else:
                self.land_queue.append(f.id)
                self.set(f.id, status="landing")
            self.say(f"resumed {f.id} at {stage}")

    # ---- disk guard ----
    def free_gb(self) -> float:
        try:
            return shutil.disk_usage(self.repo).free / 1e9
        except OSError:
            return float("inf")

    def incremental_dirs(self) -> list[Path]:
        """Cargo's incremental caches under .overhaul/target, oldest first; a running job's or land's dir is kept."""
        root = self.ov / "target"
        busy = [self.slot_dir(self.slots[fid]) for fid in self.jobs if fid in self.slots]
        busy += [root / f"land-{j.slot}" for j in self.land_jobs()]
        found = [d for pat in INCREMENTAL for d in root.glob(pat)
                 if d.is_dir() and not any(d.is_relative_to(b) for b in busy)]

        def age(d: Path) -> float:
            try:
                return d.stat().st_mtime
            except OSError:
                return 0.0
        return sorted(found, key=age)

    def disk_guard(self, why: str) -> bool:
        """Before a build start and every cycle: under [limits] prune_at_gb (or min_free_gb, the larger) free on the
        repo's drive, delete cargo's incremental dirs of idle slots oldest first until above it. False (log disk_low,
        state disk_low for the verdict) when still under min_free_gb: no new build starts."""
        if self.min_free_gb <= 0:
            return True
        free = before = self.free_gb()
        goal = max(self.min_free_gb, self.prune_at_gb)
        if free < goal:
            n = 0
            for d in self.incremental_dirs():
                shutil.rmtree(d, ignore_errors=True)
                n += 1
                free = self.free_gb()
                if free >= goal:
                    break
            if n:
                self.log(None, "disk_pruned", f"freed {max(0.0, free - before):.1f} GB from {n} incremental dirs "
                         f"({why}); free {free:.1f} GB")
        if free >= self.min_free_gb:
            if self.st.pop("disk_low", None):
                self.log(None, "disk_ok", f"free {free:.1f} GB >= min_free_gb {self.min_free_gb:g}: builds start again")
                self.save()
            return True
        line = (f"disk low: {free:.1f} GB free < min_free_gb {self.min_free_gb:g} on {self.repo.anchor or self.repo} "
                "after pruning incremental dirs; no new build starts")
        if not self.st.get("disk_low") or why == "cycle":
            self.log(None, "disk_low", line)
            self.say(line)
        self.st["disk_low"] = {"free_gb": round(free, 1), "min_gb": self.min_free_gb, "t": now_iso()}
        self.save()
        return False

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
            self.resume_step()
            self.cycle("start")
            while True:
                changed = self.replan_step()
                changed = self.harness_requests() or changed
                changed = self.reap() or changed
                changed = stack.check(self) or changed
                changed = self.pump_land() or changed
                changed = self.launch() or changed
                self.width_step()
                changed = self.probe_step() or changed
                changed = self.polish_step() or changed
                changed = self.arch_step() or changed
                changed = self.periodic.step(idle=not self.busy()) or changed
                changed = self.red_step() or changed
                if self.cycle_due:
                    why, self.cycle_due = self.cycle_due, ""
                    self.cycle(why)
                if self.dirty:
                    self.save()  # a write that had to wait for land.py's lock
                if not self.busy() or self.red_done():
                    return self.finish()
                self.dash_due = self.dash_due or changed
                if time.monotonic() - self.last_cycle >= self.heartbeat_s:
                    self.cycle("heartbeat")
                elif self.dash_due and time.monotonic() - self.last_dash >= DASH_EVERY_S:
                    self.dash()
                time.sleep(POLL_S)
        except KeyboardInterrupt:  # the owner's stop: every builder goes too
            self.abort(owner=True)
            raise
        except BaseException:  # a crash: detached builds keep running for the next drive to re-adopt
            self.abort()
            raise

    def busy(self) -> set[str]:
        """In flight: jobs, the land queue, lands, and functions waiting at landing on a main-flake fix."""
        waiting = {fid for fid, r in self.st["functions"].items()
                   if (r["status"] == "landing" and r.get("blocked_on")) or r.get("blocked_on") == mainred.MAIN_RED}
        gates = ({"periodic"} if self.periodic.busy() else set()) | self.feas.busy()  # a feasibility check runs
        return (set(self.jobs) | set(self.gating) | set(self.land_queue) | {j.fn for j in self.land_jobs()} | waiting
                | gates)

    def land_jobs(self) -> list[Job]:
        """Lands in flight, the plain one (or a polish land) first, then the stacked ones in start order."""
        return [*([self.land_job] if self.land_job else []), *self.side_lands.values()]

    def extra(self, fid: str) -> list[str]:
        """Files a row's claim grew by without a planning round (state `files_extra`, written only by
        router.add_files): a files_gap widening and the feasibility check's widening."""
        return list((self.st["functions"].get(fid) or {}).get("files_extra") or [])

    def claims(self, f: Fn) -> list[str]:
        """The files a function's build locks: its FUNCTIONS.md `files` cell, else its whole module's paths, plus
        the files the router widened it by. The module's `shared:` files are left out: a clash there goes to the
        conflict-fix pass."""
        section = module_section(self.arch, f.module)
        shared = set(module_shared(section))
        own = f.files or module_paths(section, f.module)
        return [p for p in dict.fromkeys([*own, *self.extra(f.id)]) if p not in shared]

    def widen_scope(self, f: Fn, deps: bool = True) -> list[str]:
        """Where the router may widen f's claim: its own module's paths and, with deps, its dep modules' paths."""
        return module_paths(module_section(self.arch, f.module), f.module) + (
            self.dep_paths([f.module]) if deps else [])

    def waiters(self) -> dict[str, int]:
        """fid -> how many not-proven rows wait on it through `blocking` deps, directly or through others: the
        critical-path weight. Cached until a row is proven or the plan's deps change."""
        recs = self.st["functions"]
        proven = frozenset(fid for fid, r in recs.items() if r.get("status") == "proven")
        key = (proven, tuple((f.id, tuple(f.blocking)) for f in self.order))
        if self.wait_cache[0] == key:
            return self.wait_cache[1]
        rdeps: dict[str, list[str]] = {}
        for f in self.order:
            for d in f.blocking:
                if d != f.id:
                    rdeps.setdefault(d, []).append(f.id)
        out: dict[str, int] = {}
        for f in self.order:
            seen: set[str] = set()
            todo = list(rdeps.get(f.id, []))
            while todo:
                cur = todo.pop()
                if cur not in seen and cur != f.id:
                    seen.add(cur)
                    todo.extend(rdeps.get(cur, []))
            out[f.id] = sum(1 for x in seen if x not in proven)
        self.wait_cache = (key, out)
        return out

    def rank(self, fid: str) -> tuple[int, int, int, str]:
        """Start and land order: most rows waiting first, then phase, then plan order (id). Unknown ids (a polish
        or fix pass) sort after the rows that unblock others, by id."""
        f = self.fns.get(fid)
        if f is None:
            return (0, 1 << 30, 1 << 30, fid)
        return (-self.waiters().get(fid, 0), *phase_key(f), fid)

    def startable(self, busy: set[str]) -> list[Fn]:
        """Planned rows that may build now, resumed builds first, then critical-path order (rank): every blocking
        dep proven (or, with [harness] stacked_starts, its branch reviewed and waiting to land), not in flight,
        retries left, not held on a fix, its module not audit-locked and not past the phase barrier. File claims are
        left to ready(), so a resumed build claims its files before a fresh one."""
        recs = self.st["functions"]
        proven = {fid for fid, r in recs.items() if r["status"] == "proven"}
        # the phase barrier: nothing past the lowest phase that still has an unproven function starts
        open_phase = min((phase_key(f)[0] for f in self.order if f.id not in proven), default=0)
        if self.barrier and self.phase_polish and (polishing := self.polish_phase()) is not None:
            open_phase = min(open_phase, polishing)  # ... and phase N+1 waits for phase N's polish and probes
        locked = self.audit_locked()
        out = [f for f in self.order
               if not (recs[f.id]["status"] != "planned" or f.id in busy or f.module in locked
                       or recs[f.id].get("blocked_on") or (self.barrier and phase_key(f)[0] > open_phase)
                       or not stack.deps_ok(self, f, proven)
                       or recs[f.id]["attempts"] > self.budget(f.id))]  # budget() walks the graph: last
        return sorted(out, key=lambda f: (not recs[f.id].get("resume"), self.rank(f.id)))  # resumed work first

    def ready(self) -> list[Fn]:
        """Functions to launch now, critical path first: startable() rows with no file overlap with a function in
        flight (building, in review or landing) or with one launched before it in this pass. A dep the `needs`
        column marks `stub` does not block: its stub is on main from the plan commit.

        Two builders that edit one file fail to land with a merge conflict, so a function waits while an in-flight
        function's claimed files overlap its own. A plan with a `files` column runs one module's functions at once;
        without it a function claims its module's paths, one function per module at a time."""
        busy = self.busy()
        taken = [self.claims(self.fns[fid]) for fid in busy if fid in self.fns]
        ready: list[Fn] = []
        for f in self.startable(busy):
            mine = self.claims(f)
            if any(paths_overlap(mine, other) for other in taken):
                continue
            taken.append(mine)
            ready.append(f)
        return ready

    def width_step(self) -> None:
        """Record st["width"] for verdict.py and dash.py: `ready` rows (startable, deps proven, not in flight)
        beside free builder slots. Ready under free slots for [harness] width_red_min is `width red`, naming the
        not-proven rows that unblock the most (pilot2026-11-09: ~9 of 120 rows ready, slots idle)."""
        now = time.monotonic()
        if now - self.last_width < WIDTH_EVERY_S:
            return
        self.last_width = now
        n = len(self.startable(self.busy()))
        free = max(0, self.max_parallel - len(self.jobs))
        if n >= free or self.red_hold():
            self.width_since = None
        elif self.width_since is None:
            self.width_since = now
        short_s = 0.0 if self.width_since is None else now - self.width_since
        red = self.width_since is not None and short_s >= self.width_red_s
        recs, w = self.st["functions"], self.waiters()
        top = sorted((fid for fid, c in w.items() if c and recs.get(fid, {}).get("status") != "proven"),
                     key=self.rank)[:WIDTH_NAMES]
        unblock = [{"id": fid, "waiting": w[fid], "status": recs[fid]["status"]} for fid in top]
        prev = self.st.get("width") or {}
        self.st["width"] = {"ready": n, "free": free, "slots": self.max_parallel, "short_min": round(short_s / 60),
                            "red": red, "unblock": unblock, "t": now_iso()}
        if red and not prev.get("red"):
            names = ", ".join(f"{u['id']} ({u['waiting']} waiting, {u['status']})" for u in unblock) or "none"
            self.log(None, "width_red", f"ready {n} < free {free} for {round(short_s / 60)} min; "
                                         f"unblock most: {names}")

    def launch(self) -> bool:
        launched = False
        if self.red_hold():  # main red, or stopping for the manager
            return False
        local_ok: bool | None = None  # the disk guard, checked once, and only before a build on this machine
        self.feas.collect()
        rows = self.ready()
        self.feas.prefetch(rows)  # the next rows' feasibility checks run while slots are busy
        for f in rows:  # resumed work first, then critical-path order (startable)
            if self.local_jobs() < self.max_parallel and local_ok is None:
                local_ok = self.disk_guard("build start")
            if self.local_jobs() < self.max_parallel and local_ok:
                self.start_build(f)
            elif self.remote and (k := self.remote.free_slot()) is not None:  # a host's disk is its own
                self.start_build(f, rslot=k)
            else:
                break
            launched = True
        return launched

    def local_jobs(self) -> int:
        """Jobs on this machine's max_parallel slots ([harness.remote] builds count on their host)."""
        return sum(not j.host for j in self.jobs.values())

    def reap(self) -> bool:
        """Handle finished or timed-out jobs; True if any finished."""
        now, done = time.monotonic(), False
        for job in [*self.jobs.values(), *self.land_jobs()]:
            if job.proc.poll() is None:
                if now < job.deadline and not self.stalled(job, now):
                    continue
                kill_tree(job.proc)
                job.timed_out = True
            if job.stream is not None:  # its last progress line, finished or killed
                job.stream.poll(now)
                job.stream.write_progress(now, force=True)
            done = True
            if job.kind in POLISH_KINDS:
                self.reap_polish(job)
            elif job.kind == "land":
                if job.slot >= 0:
                    self.prune_slot(self.ov / "target" / f"land-{job.slot}")
                if job is self.land_job:
                    self.land_job = None
                else:
                    del self.side_lands[job.fn]
                self.after_land(job)
            else:
                del self.jobs[job.fn]
                self.job_ended(job)
                {"build": self.after_build, "conflict": self.after_conflict,
                 "probe_mutate": self.after_probe_mutate,
                 "verify": self.after_verify}.get(job.kind, self.after_review)(job)
        return self.reap_gates() or done

    def job_ended(self, job: Job) -> None:
        """A slot job ended: its pid file goes, and a local job's slot loses cargo's incremental dirs (the next
        build of that slot recompiles the workspace crates, never the deps), so finished jobs do not fill the disk."""
        drivelock.drop_job(self.ov, job.fn)
        if not job.host and job.fn in self.slots:
            self.prune_slot(self.slot_dir(self.slots[job.fn]))

    @staticmethod
    def prune_slot(d: Path) -> None:
        for sub in ("debug/incremental", "release/incremental"):
            if (d / sub).is_dir():
                shutil.rmtree(d / sub, ignore_errors=True)

    def gate_async(self, job: Job) -> None:
        """The pre-review merge and gate (sync_problem) for a finished build, in a worker thread; reap_gates hands
        the result to to_review in the main loop. The row keeps its slot while gating (slot() counts it held)."""
        self.slot(job.fn)
        if self.gate_pool is None:
            self.gate_pool = ThreadPoolExecutor(max_workers=self.gate_parallel, thread_name_prefix="gate")
        self.gating[job.fn] = (job, self.gate_pool.submit(self.sync_problem, job.fn))

    def reap_gates(self) -> bool:
        """Finished pre-review gates go on to review (or fail, or a conflict pass); True if any finished."""
        done = [fid for fid, (_, fut) in self.gating.items() if fut.done()]
        for fid in done:
            job, fut = self.gating.pop(fid)
            try:
                problem = fut.result()
            except Exception as e:  # noqa: BLE001 - a gate that crashed is a failed gate, never a dead drive
                problem = f"pre-review gate crashed: {type(e).__name__}: {e}"[:REASON_MAX]
            self.to_review(job, problem)
        return bool(done)

    def stalled(self, job: Job, now: float) -> bool:
        """Read a stream-json worker's new events into <run>.progress; True when it has sent none for
        worker_idle_min (logged as a timeout naming the last tool it ran)."""
        if job.stream is None:
            return False
        job.stream.poll(now)
        job.idle = job.stream.stalled(now, self.idle_min)
        job.stream.write_progress(now, force=bool(job.idle))
        if job.idle:
            self.log(job.fn, "timeout", f"{job.kind}: {job.idle}", now - job.started, job.stream.tokens or None)
        return bool(job.idle)

    def watch(self, job: Job, cmd: list[str]) -> Job:
        """Give a stream-json worker its event reader."""
        if "stream-json" in cmd:
            job.stream = worker.Stream(job.out, job.started)
        return job

    # ---- build ----
    def custom_cmd(self, kind: str) -> list[str]:
        har = self.cfg["harness"]
        custom = (har.get("reviewer_cmd") if kind in ("review", "verify") else None) or har.get("worker_cmd")
        if not custom:
            return []
        return [str(c) for c in custom] if isinstance(custom, list) else shlex.split(custom)

    def worker_cmd(self, kind: str, role: str = "") -> list[str]:
        """The worker command for kind build | conflict | review, without the prompt; role (default kind) picks
        its tools (worker.READ_ROLES get no Edit or Write)."""
        if custom := self.custom_cmd(kind):
            return custom
        exe = shutil.which("claude")
        if not exe:
            raise DriveError("claude CLI not on PATH")
        cmd = [exe, "-p", "--dangerously-skip-permissions", *worker.STREAM, *worker.lean_flags(exe, role or kind)]
        if supports_max_turns(exe):
            cmd += ["--max-turns", str(REVIEW_MAX_CALLS if kind in ("review", "verify") else self.max_calls)]
        model = {"review": self.review_model, "verify": self.verify_model}.get(kind, self.builder_model)
        return cmd + (["--model", model] if model else [])

    def start_job(self, fid: str, kind: str, attempt: int, brief: Path, cwd: Path, tag: str = "",
                  rslot: int | None = None, max_calls: int | None = None) -> None:
        out = self.ov / "runs" / f"{fid}-a{attempt}-{tag or kind}.json"
        base = self.worker_cmd(kind)
        if max_calls and "--max-turns" in base[:-1]:  # a build from a work packet: its own, smaller budget
            base[base.index("--max-turns") + 1] = str(max_calls)
        cmd = [*base, f"Read and do {brief.as_posix()}"]
        try:
            if rslot is not None and self.remote:  # remote.py: push, build on the host, fetch fn/<id> back
                env = {k: v for k, v in os.environ.items() if k != "OVERHAUL_HARNESS"}
                proc = self.remote.start(rslot, fid, attempt, base, bool(self.custom_cmd(kind)), brief, cwd,
                                         self.repo, self.main, self.ov / "runs", self.scripts, STANDARD.parent,
                                         lambda c: spawn(c, self.repo, out, env), worker.env(base, {}))
            elif self.detach and kind in DETACH_KINDS:
                proc = self.spawn_detached(fid, kind, attempt, cmd, cwd, out)
            else:
                proc = spawn(cmd, cwd, out, worker.env(cmd, self.slot_env(fid)))
        except OSError as e:
            raise DriveError(f"cannot start the {kind} worker {cmd[0]}: {e}") from e
        model = base[base.index("--model") + 1] if "--model" in base[:-1] else (
            "custom" if self.custom_cmd(kind) else "default")
        start = time.monotonic()
        remote_job = rslot is not None and self.remote
        limit = self.remote.timeout_s if remote_job else self.timeout_s  # [harness.remote] timeout_min (45)
        job = Job(fid, kind, proc, out, start, start + limit, attempt, model=model, calls=max_calls or 0,
                  host=self.remote.name if remote_job else "")
        # a remote build streams its events (remote.py) and has its own idle check (remote_idle_min): no local watch
        self.jobs[fid] = job if job.host else self.watch(job, cmd)

    def spawn_detached(self, fid: str, kind: str, attempt: int, cmd: list[str], cwd: Path,
                       out: Path) -> subprocess.Popen:
        """A local build in worker.py's launcher, out of drive's job (its own job, same memory cap), with a pid file
        a restarted drive re-adopts. A job above drive that refuses breakaway: started attached (log detach_off)."""
        rc = out.with_suffix(".rc")
        rc.unlink(missing_ok=True)
        wrapped = worker.launch_cmd(cmd, float(self.cfg["limits"].get("proc_mem_gb") or 0), rc)
        env = worker.env(cmd, self.slot_env(fid))
        try:
            proc = spawn(wrapped, cwd, out, env, detach=not self.detach_off)
        except OSError as e:
            if self.detach_off or os.name != "nt":
                raise
            self.detach_off = True
            self.log(fid, "detach_off", f"breakaway refused ({e}): builders run inside drive's job and die with it")
            proc = spawn(wrapped, cwd, out, env)
        if not isinstance(proc.pid, int):  # a stand-in process (tests): nothing to re-adopt
            return proc
        model = cmd[cmd.index("--model") + 1] if "--model" in cmd[:-1] else ""
        drivelock.write_job(self.ov, fid, {
            "pid": proc.pid, "start": drivelock.proc_start(proc.pid) or "", "kind": kind, "out": str(out),
            "rc": str(rc), "started": time.time(), "attempt": attempt, "slot": self.slots.get(fid, -1),
            "model": model, "stream": "stream-json" in cmd, "drive": getattr(self, "drive_id", "")})
        return proc

    def adopt_jobs(self) -> list[int]:
        """At start (single_start): every live detached build a previous drive left becomes a running job again,
        with its slot re-claimed and its deadline counted from its own start. Returns the adopted pids."""
        pids = []
        for fid, rec in self.adopt.items():
            started = time.monotonic() - max(0.0, time.time() - float(rec.get("started") or time.time()))
            out = Path(rec["out"])
            job = Job(fid, str(rec.get("kind") or "build"), drivelock.Adopted(rec), out, started,
                      started + self.timeout_s, int(rec.get("attempt") or 1), model=str(rec.get("model") or ""))
            if rec.get("stream"):
                job.stream = worker.Stream(out, started)
            self.jobs[fid] = job
            if int(rec.get("slot", -1)) >= 0:
                self.slots[fid] = int(rec["slot"])
                self.st.setdefault("slots", {})[fid] = int(rec["slot"])
            self.slot(fid)  # its marker now names this drive
            rec["drive"] = self.drive_id
            drivelock.write_job(self.ov, fid, rec)
            self.set(fid, status="building")
            pids.append(job.proc.pid)
            self.log(fid, "build_adopted", f"pid {job.proc.pid} attempt {job.attempt} kept running across the drive "
                     "restart")
            self.say(f"adopted {fid}: its builder (pid {job.proc.pid}) kept running")
        self.adopt.clear()
        return pids

    def span(self, branch: str) -> list[str]:
        """`git diff` arguments for what branch changes itself: main...branch, or main + stacked deps..branch."""
        return stack.diff_range(self, branch)

    def kill(self, job: Job) -> None:
        """Stop a job dropped from self.jobs (a stack reset): its process tree, and its remote slot."""
        kill_tree(job.proc)
        drivelock.drop_job(self.ov, job.fn)
        if job.host and self.remote:
            self.remote.after(job.fn, -1, True, job.out.with_suffix(".err"))

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
        tests = unit_dirs(self.cfg["commands"].get("test", "")) or ["tests/unit/"]
        return list(dict.fromkeys([*paths, *self.extra(f.id)])), tests

    def dep_paths(self, modules: list[str]) -> list[str]:
        """The paths of the modules these modules depend on (ARCHITECTURE.md `Depends on:`), themselves excluded."""
        deps = dict.fromkeys(d for m in modules for d in module_deps(self.arch, m) if d not in modules)
        return list(dict.fromkeys(p for d in deps for p in module_paths(module_section(self.arch, d), d)))

    @staticmethod
    def vis_line(where: str, vis: list[str]) -> list[str]:
        if not vis:
            return []
        return [f"- visibility-only edits in {where} (" + ", ".join(f"`{p}`" for p in vis) + "): make an existing "
                "item `pub`, `pub(crate)` or `export`ed, or add a `pub use` / `export {...} from` re-export of one, so "
                "you can import a helper instead of duplicating it (C05). Nothing else in those files: drive.py "
                "refuses any other change there before review, and the review checks it too"]

    def allowed(self, f: Fn) -> str:
        paths, tests = self.allowed_lists(f)
        own = [p for p in module_paths(module_section(self.arch, f.module), f.module) if p not in paths]
        deps = self.dep_paths([f.module])
        where = (f"the rest of module `{f.module}`" if own else f"module `{f.module}`") + (
            " and in the modules it depends on" if deps else "")
        return "\n".join([f"- `{p}`" for p in paths] + [f"- `{t}` (unit tests for this module only)" for t in tests]
                         + self.vis_line(where, own + deps))

    def vis_problem(self, changed: list[str], branch: str, allowed: list[str], vis: list[str], what: str) -> str:
        """Why a change to a file under vis (and not under allowed) is more than a visibility edit, or ''. Files
        outside both are left to the review, as before."""
        bad = [p for p in changed if not in_paths(p, allowed) and in_paths(p, vis)
               and not visibility_only(git(self.repo, "diff", "-U0", *self.span(branch), "--", p))]
        return (f"changed {', '.join(bad[:5])} beyond visibility: {what} may only make an existing item pub/"
                "pub(crate)/export or re-export it there") if bad else ""

    def visibility_problem(self, f: Fn, changed: list[str], branch: str) -> str:
        """Why a change to a file of f's own module outside its files cell, or of a module f's module depends on,
        is more than a visibility edit, or ''."""
        paths = [p for group in self.allowed_lists(f) for p in group]
        vis = module_paths(module_section(self.arch, f.module), f.module) + self.dep_paths([f.module])
        return self.vis_problem(changed, branch, paths, vis, f"outside its files cell (module {f.module} and the "
                                "modules it depends on) a function")

    def polish_vis_problem(self, fid: str, module: str, branch: str) -> str:
        """A polish fix's edits in the modules its own modules depend on must be visibility-only (pass E)."""
        mods = module.split(", ")
        own = [p for m in mods for p in module_paths(module_section(self.arch, m), m)]
        own += unit_dirs(self.cfg["commands"].get("test", "")) or ["tests/unit/"]
        if fid.startswith(PROBE_PREFIX):
            own.append("tests/property/")
        changed = git(self.repo, "diff", "--name-only", f"{self.main}...{branch}").splitlines()
        return self.vis_problem(changed, branch, own, self.dep_paths(mods), f"in the modules {module} depends on, "
                                "a polish fix")

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
        f = self.fns[fid]  # its own and its deps' module files count: an edit there passed the visibility check
        allowed = [p for group in self.allowed_lists(f) for p in group]
        allowed += module_paths(module_section(self.arch, f.module), f.module) + self.dep_paths([f.module])
        changed = git(self.repo, "diff", "--name-only", *self.span(f"fn/{fid}")).splitlines()
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

    def scope_gaps(self) -> list[dict]:
        """The plan gaps of the polish in scope: those phase N's functions recorded, or for the final pass every
        gap a phase gap pass has not already taken (a gap of a function outside the plan goes to the final pass)."""
        phase_of, n = {f.id: phase_key(f)[0] for f in self.order}, self.polish_phase()
        if n is not None:  # minus those an earlier audit of this phase already took
            done = set(self.polish_state().get("gaps_done") or [])
            return [e for e in self.read_gaps() if phase_of.get(str(e.get("fn"))) == n and e.get("gap") not in done]
        taken = {int(k) for k, v in self.st.get("phase_polish", {}).items() if v.get("gaps")}
        return [e for e in self.read_gaps() if phase_of.get(str(e.get("fn"))) not in taken]

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

    def proven_ids(self) -> set[str]:
        recs = (getattr(self, "st", None) or {}).get("functions", {})
        return {r for r, g in self.fns.items() if g.status == "proven" or recs.get(r, {}).get("status") == "proven"}

    def proven_footprint(self, fid: str, touched: list[str]) -> list[tuple[str, str]]:
        """[(row id, test id)]: the proven acceptance tests a change to `touched` (paths or path patterns) can
        break, picked as land.py picks a regate's (land.footprint_rows over footprint_modules: fid's module and the
        modules whose Paths hold a touched path): rows of those modules, of every module depending on one, and of modules
        ARCHITECTURE.md does not list. A [tiers] test is in only when its row's module is touched itself, as land
        runs it. fid's own tests are left out: the build runs those anyway."""
        f = self.fns[fid]
        mods = self.footprint_modules(fid, touched)
        proven = self.proven_ids()
        rows = {r: {"status": "proven" if r in proven else "", "module": g.module} for r, g in self.fns.items()}
        ids, own, tiers, out = land_mod.footprint_rows(self.arch, rows, mods, fid), set(f.acceptance), \
            self.cfg.get("tiers", []), {}
        for g in self.order:
            for node in g.acceptance if g.id in ids else ():
                if node not in own and node not in out and (g.module in mods or not land_mod.under(acc_file(node),
                                                                                                  tiers)):
                    out[node] = g.id
        return [(rid, node) for node, rid in out.items()]

    def footprint_text(self, f: Fn) -> str:
        """The build brief's footprint section: the batched commands of proven_footprint() over the allowed paths."""
        tmpl = self.cfg.get("acceptance") or self.cfg["commands"].get("acceptance")
        pairs = self.proven_footprint(f.id, self.allowed_lists(f)[0]) if tmpl else []
        if not pairs:
            return "None yet: no proven row's acceptance test is in this function's footprint."
        rows = ", ".join(dict.fromkeys(r for r, _ in pairs))
        return f"{len(pairs)} tests of proven rows {rows}:\n\n" + "\n".join(
            f"- `{cmd}`" for _, cmd in land_mod.batch_cmds(tmpl, [n for _, n in pairs]))

    def index_modules(self) -> dict[str, list[str]]:
        """{module: paths} of every planned module, as the allowed paths read them from ARCHITECTURE.md."""
        mods = dict.fromkeys(f.module for f in self.order if f.module)
        return {m: module_paths(module_section(self.arch, m), m) for m in mods}

    def helpers_lines(self, f: Fn) -> list[str]:
        """main's INDEX lines of the row's module and every module it may depend on (transitively), ranked by
        purpose-token overlap with the row and its acceptance test; [] when the plan has no module Paths."""
        mods, todo = [], [f.module]
        while todo:
            m = todo.pop(0)
            if m not in mods:
                mods.append(m)
                todo += module_deps(self.arch, m)
        try:
            idx = index_api.index_at(self.repo, self.main_sha(), self.index_modules())
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            self.log(f.id, "index_error", str(e)[:300])
            return []
        items = [it for m in mods for it in idx.get(m, [])]
        lines = index_api.ranked(items, f.row + "\n" + self.acceptance_text(f), self.index_top)
        budget, out = self.max_tokens * 4 // HELPERS_SHARE, []
        for ln in lines:
            ln = ln if len(ln) <= 300 else ln[:297] + "..."
            if sum(len(x) + 1 for x in out) + len(ln) > budget:
                break
            out.append(ln)
        return out

    def helpers_text(self, f: Fn) -> str:
        lines = self.helpers_lines(f)
        if not lines:
            return ("(no indexed item of this module or its dependencies matches this row's words; still search "
                    f"`{index_api.INDEX_DIR}/` before writing any function)")
        return "```\n" + "\n".join(lines) + "\n```"

    def semantic_dup(self, fid: str) -> str:
        """The semantic duplicate gate on fn/<fid>'s new functions against main (index_api), or ''."""
        if self.dup_semantic_min <= 0:
            return ""
        branch = f"fn/{fid}"
        try:
            changed = [p for p in git(self.repo, "diff", "--name-only", *self.span(branch)).splitlines() if p]
            return index_api.semantic_problem(self.repo, self.main_sha(), branch, changed, self.index_modules(),
                                              self.dup_semantic_min)
        except (OSError, ValueError, DriveError, subprocess.SubprocessError) as e:
            self.log(fid, "index_error", str(e)[:300])
            return ""

    def dup_requeue(self, fid: str) -> bool:
        """Pre-review: a semantic duplicate sends the build back before the gate and the review. The first time the
        attempt is not counted (the builder had no index hit to go on); after that it is an ordinary failure."""
        if fid not in self.fns or not (problem := self.semantic_dup(fid)):
            return False
        rec = self.st["functions"][fid]
        self.log(fid, "dup_semantic", problem)
        if rec.get("dup_free"):
            self.fail(fid, problem)
        else:
            rec["dup_free"] = True
            self.set(fid, status="planned", last_reason=f"(not counted) {problem}"[:REASON_MAX])
            self.say(f"{fid} sent back before review, attempt not counted: {problem}")
        return True

    def dup_text(self) -> str:
        """The build brief's C05 section: git grep for each new name, then the dup and literals commands."""
        lines = ['- `git grep -n "<words>"` for every helper, constant, message or format string you added: its '
                 "name and its core expression or distinctive words. A hit outside your change is a duplicate"]
        lines += [f"- {n}: `{c}` (a finding in one of your changed files is a C05 NO)"
                  for n in ("dup", "literals") if (c := self.cfg["commands"].get(n))]
        return "\n".join(lines)

    def footprint_problem(self, wt: Path, fid: str) -> str:
        """The proven tests in the branch's real footprint (proven_footprint() over its changed files), run before
        review in the worktree on fid's slot, batched as land runs them: a regression costs a retry with the test
        named, not a review and a land slot. A failed [tiers] test reruns alone like at land; a test that also fails on
        clean main is land's main-flake case and does not fail the attempt; a timeout is left to land. ''
        when green, off ([harness] pre_review_footprint = false) or not a function."""
        tmpl = self.cfg.get("acceptance") or self.cfg["commands"].get("acceptance")
        if fid not in self.fns or not tmpl or not self.cfg["harness"].get("pre_review_footprint", True):
            return ""
        changed = [p for p in git(self.repo, "diff", "--name-only", *self.span(f"fn/{fid}")).splitlines() if p]
        pairs = self.proven_footprint(fid, changed)
        if not pairs:
            return ""
        owner = {n: r for r, n in pairs}

        def runner(cmd: str) -> tuple[int, str, str]:
            p = self.sh(cmd, wt, fid)
            if p is None:
                return 124, "timed out", ""
            text = p.stdout + p.stderr
            return p.returncode, " / ".join([ln.strip() for ln in text.splitlines() if ln.strip()][-8:]), text
        res = land_mod.batch_results(tmpl, list(owner), runner)
        self.log(fid, "footprint", f"{len(owner)} proven tests of {len(set(owner.values()))} rows before review: "
                 f"{sum(1 for c, _, _ in res.values() if c != 0)} red")
        for node, (code, tail, _) in res.items():
            if code == 0:
                continue
            if code in (-1, 124):
                self.log(fid, "footprint_timeout", f"{node} timed out before review; land runs it")
                continue
            # alone, for its own output; a [tiers] bench starved by the builders gets land's extra reruns
            one = land_mod.acceptance_cmds(tmpl, [node])[0][1]
            tier = land_mod.under(acc_file(node), self.cfg.get("tiers", []))
            for _ in range(1 + (land_mod.TIER_RERUNS if tier else 0)):
                code, tail, text = runner(one)
                if code == 0:
                    break
            if code in (0, -1, 124) or self.footprint_red_on_main(node, one):
                continue
            out = self.ov / "runs" / f"{fid}-footprint.out"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            return memcap.note(text, f"regression before review: proven {owner[node]} {node} (exit {code}) is "
                               f"red in the worktree after merging {self.main}: {tail} (full output: "
                               f"{out.as_posix()}). Keep it green inside your allowed paths (rule 6 b), then "
                               "hand back")
        return ""

    def footprint_red_on_main(self, node: str, cmd: str) -> bool:
        """Whether a proven test is red on clean main too (cached per main sha): then it is not this branch's."""
        sha = self.main_sha()
        key = (sha, "footprint:" + node)
        if key not in self.red_cache:
            wt = self.prepare_worktree("main-check")
            run(["git", "checkout", "-q", "-f", "--detach", sha], wt)
            p = self.sh(cmd, wt, "main-check")
            self.red_cache[key] = (p is None or p.returncode == 0, "")
        red = not self.red_cache[key][0]
        if red:
            self.log(None, "footprint_main_red", f"{node} is red on clean {self.main} {sha[:12]} too; land's "
                     "main-flake path owns it")
        return red

    def row_table(self, f: Fn) -> str:
        return f"{f.header}\n|{'---|' * len(cells(f.header))}\n{f.row}"

    def start_build(self, f: Fn, rslot: int | None = None) -> None:
        if not self.feas.ok(f):  # its check runs, it was parked infeasible, or its widened claim must wait
            return
        rec = self.st["functions"][f.id]
        attempt = rec["attempts"] + 1
        resume = rec.get("resume") == "building"
        on = list(rec.get("stacked_on") or {}) if resume else stack.to_stack(self, f)
        previous = (f"Attempt {attempt - 1} failed: {rec['last_reason']}\n"
                    "The worktree was reset to main. Fix that cause first."
                    if rec["last_reason"] else "None. This is the first attempt.")
        widened = rec.pop("widened", "")
        if resume:
            previous = ((f"Retried at once: {widened}." if widened else
                         "Resumed: the drive stopped while this attempt ran.") + f" fn/{f.id} keeps its "
                        f"{self.ahead(f.id)} commits ahead of {self.main} and the worktree is as it was left. Read "
                        f"`git log --oneline {self.main}..HEAD` and continue from that work; do not start over.\n"
                        + (f"Before that, attempt {attempt - 1} failed: {rec['last_reason']}" if rec["last_reason"]
                           else ""))
        packet, calls = self.feas.packet(f, attempt)  # the feasibility work packet and its tool-call budget
        brief = render("build-brief.md", {
            "id": f.id, "attempt": str(attempt), "branch": f"fn/{f.id}",
            "worktree": (self.ov / "wt" / f.id).as_posix(), "row": self.row_table(f), "entry": f.entry,
            "module": f.module, "module_section": module_section(self.arch, f.module)
            or "(spec/ARCHITECTURE.md has no section naming this module)",
            "allowed_paths": self.allowed(f), "acceptance_source": self.acceptance_text(f),
            "policies": self.policies(f), "commands": self.commands_text(f), "max_calls": str(calls),
            "footprint": self.footprint_text(f), "dup_check": self.dup_text(),
            "helpers": self.helpers_text(f), "index_dir": index_api.INDEX_DIR,
            "previous": previous + stack.brief_note(on), **self.standard_values(f)})
        brief = feasibility.embed(brief, packet)  # under the title: the builder starts there
        tokens = len(brief) // 4
        if tokens > self.max_tokens:
            self.park(f.id, f"brief is {tokens} tokens, over brief_max_tokens {self.max_tokens}; "
                            "split the function or trim its acceptance test or ARCHITECTURE section")
            return
        rec.pop("resume", None)
        wt = self.resume_worktree(f.id) if resume else self.prepare_worktree(f.id)
        if not resume:
            shas = stack.merge_deps(self, f.id, wt, on) if on else {}
            if shas is None:
                self.save()
                return  # a dep branch does not merge: the row waits for its deps to land (no_stack)
            rec.pop("stacked_on", None)
            rec.pop("stacked", None)
            if shas:
                rec["stacked_on"] = shas
        path = self.ov / "briefs" / f"{f.id}.md"
        path.write_text(brief, encoding="utf-8")
        self.start_job(f.id, "build", attempt, path, wt, rslot=rslot, max_calls=calls)
        if packet:
            self.log(f.id, "packet", f"build starts from the work packet, max calls {calls}")
        host = self.jobs[f.id].host
        # a fresh branch from main: a fresh conflict budget; a resumed one keeps its own
        self.set(f.id, status="building", host=host or None, **({} if resume else {"conflicts": 0}))
        on_host = f" host={host}" if host else ""
        self.log(f.id, "build_start", f"attempt {attempt} model={self.jobs[f.id].model}{on_host}"
                 + (" resumed" if resume else ""))
        if on and not resume:
            self.log(f.id, "stacked", f"built on unlanded {', '.join(on)}")
        self.say(f"build {f.id} attempt {attempt}{on_host}" + (f" stacked on {', '.join(on)}" if on else ""))

    def after_build(self, job: Job) -> None:
        # the host streams every builder line, so only a zero-byte run file is a dead host; a slow row with output
        # is a real timeout and spends its attempt (remote-hang-report 2026-11-10: slow rows looped as "offline")
        silent = job.timed_out and not read_text(job.out).strip()
        if job.host and self.remote and self.remote.after(job.fn, job.proc.returncode, job.timed_out,
                                                          job.out.with_suffix(".err"), silent=silent):
            self.set(job.fn, status="planned", host=None)  # the host was unusable: requeued, no attempt spent
            return
        tokens, turns, text = read_result(job.out)
        cap = job.calls or self.max_calls
        flags = (" over-budget" if turns is not None and turns > cap else "") + \
                (" timeout" if job.timed_out else "")
        self.log(job.fn, "build_end", f"rc={job.proc.returncode} turns={turns}/{cap}{flags} "
                 f"model={job.model}", time.monotonic() - job.started, tokens)
        if self.blocked_by(job.fn, text):
            return
        if problem := self.build_problem(job, text):
            self.to_review(job, problem)
            return
        if job.kind == "build" and self.dup_requeue(job.fn):
            return
        self.gate_async(job)

    def owner_module(self, path: str) -> str:
        """The module whose ARCHITECTURE paths hold path, or ''."""
        for m in dict.fromkeys(f.module for f in self.order):
            if any(paths_overlap([path], [p]) for p in module_paths(module_section(self.arch, m), m)):
                return m
        return ""

    def blocked_by(self, fid: str, text: str) -> bool:
        """A builder's BLOCKED-BY line: its acceptance fails on a landed dependency's bug outside its paths, which
        no retry of it can fix. The owning module gets one fix pass (fid depfix-<id>, built, reviewed and landed
        like a probe fix) and the function waits for it without spending an attempt. One per function. Before
        that, router.on_blocked routes a missing capability to a plan fix (never a depfix), widens the claim by a
        file of its own module, and routes a second BLOCKED-BY to a plan fix (never "no committed change")."""
        m = BLOCKED_BY.search(text or "")
        rec = self.st["functions"][fid]
        if not m:
            return False
        path, line, why = m.group(1).strip("`"), m.group(2) or "?", m.group(3).strip() or "(no detail)"
        if path.startswith(self.protected):  # the contract itself is wrong: only a plan fix helps, so stop now
            self.log(fid, "blocked_by", f"contract {path}:{line}: {why}"[:REASON_MAX])
            self.park(fid, f"contract defect reported by the builder in {path}:{line}: {why}")
            return True
        module, dfid = self.owner_module(path), f"{DEPFIX_PREFIX}{fid}"
        if fid in self.fns and router.on_blocked(self, fid, path, f"{path}:{line} in {module or '?'}: {why}",
                                                 rec.get("depfixes", 0) >= 1):
            self.log(fid, "blocked_by", f"{path}:{line} in {module or '?'}: {why}"[:REASON_MAX])
            return True
        if (rec.get("depfixes", 0) >= 1 or not module or module == self.fns[fid].module or dfid in self.jobs
                or dfid in self.polish_lands or any(paths_overlap([path], [c]) for c in self.claims(self.fns[fid]))):
            return False
        if self.red_dead_block(fid, module, f"{path}:{line} {why}"):
            return True
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
        router.on_depfix_settled(self, dfid, ok)  # a main-flake fix settled: the lands it held queue again
        if self.red_depfix(dfid, ok, reason):  # the main-red fix, or a second dead depfix in one module
            return
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
        router.on_main_flake(self, fid, test, path, module, dfid)  # lands that rerun the test wait for its fix
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
        held = {self.slots[j] for j in [*self.jobs, *self.gating] if j != fid and j in self.slots}
        me = getattr(self, "drive_id", "")
        k = self.slots.get(fid)
        if k is None or k in held or drivelock.held_by_other(self.slot_dir(k), me):
            k = next(i for i in range(len(held) + 64) if i not in held and
                     not drivelock.held_by_other(self.slot_dir(i), me))
            self.slots[fid] = k
            if hasattr(self, "st"):  # persisted with the next save; checked at start
                self.st.setdefault("slots", {})[fid] = k
        if me and not drivelock.owns(self.slot_dir(k), me, fid):
            drivelock.claim(self.slot_dir(k), me, fid)
        return k

    def slot_dir(self, k: int) -> Path:
        return self.ov / "target" / f"slot-{k}"

    def single_start(self) -> None:
        """One drive per repo, at start (the lock is held): restore the persisted slot map (dropping a slot given
        twice or held by another live drive), re-adopt the live detached builds a previous drive left, then kill the
        dead drive's other leftover workers."""
        self.drive_id = drivelock.me()
        kept, notes = drivelock.check_slots(self.st.get("slots", {}), self.ov / "target", self.drive_id)
        for note in notes:
            self.log(None, "slot_dropped", note)
        self.slots = dict(kept)
        self.st["slots"] = dict(kept)
        adopted = self.adopt_jobs() if getattr(self, "adopt", None) else []
        for pid in drivelock.kill_orphans(self.repo, lambda pid, cmd: self.log(None, "orphan_killed",
                                                                               f"pid {pid}: {cmd[:300]}"),
                                          adopted=set(adopted)):
            self.say(f"killed orphan pid {pid} (a dead drive's worker)")
        self.clear_git_locks()

    def clear_git_locks(self, min_age_s: float = 60.0) -> None:
        """Remove git lock files a killed run left (pilot2026-11-10: a restart at 05:24 left .git/packed-refs.lock
        and ref updates failed for an hour). Safe at start: the drive lock is held and orphan workers are killed."""
        gdir = self.repo / ".git"
        for name in ("packed-refs.lock", "index.lock", "HEAD.lock", "config.lock"):
            path = gdir / name
            try:
                if path.is_file() and time.time() - path.stat().st_mtime > min_age_s:
                    path.unlink()
                    self.log(None, "git_lock_cleared", name)
            except OSError:
                pass

    def slot_mismatch(self, wt: Path, fid: str, output: str) -> str:
        """Why a red acceptance run may have tested a binary that is not this job's own build, or ''."""
        d = self.slot_dir(self.slots[fid]) if fid in self.slots else None  # never slot(): it would re-claim
        if d is None or not getattr(self, "drive_id", ""):
            return ""
        if not drivelock.owns(d, self.drive_id, fid):
            return f"{d.name} changed hands during the run (now {drivelock.read_marker(d) or 'unmarked'})"
        foreign = drivelock.foreign_binaries(output, d, wt)
        return f"ran {foreign[0]}, outside {d.as_posix()}" if foreign else ""

    def rebuild_slot(self, wt: Path, fid: str, why: str) -> None:
        """Claim fid's slot again and rebuild in it ([commands] build when set; the acceptance command's own build
        otherwise), so the next acceptance run tests this worktree's binary."""
        self.log(fid, "slot_rebuild", why)
        drivelock.claim(self.slot_dir(self.slot(fid)), self.drive_id, fid)
        if self.cfg["commands"].get("build"):
            self.sh(land_mod.fill(self.cfg["commands"]["build"]), wt, fid)

    def slot_env(self, fid: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()  # builders never move main (the guard) nor run under the lock
               if k not in ("OVERHAUL_HARNESS", drivelock.ENV)}
        return {**env, "CARGO_TARGET_DIR": str(self.ov / "target" / f"slot-{self.slot(fid)}")}

    def sh(self, cmd: str, wt: Path, fid: str = "") -> subprocess.CompletedProcess[str] | None:
        """One gate command in a worktree, on fid's slot target dir; None on timeout."""
        try:
            return subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=self.timeout_s, stdin=subprocess.DEVNULL,
                                  env=self.slot_env(fid or wt.name), **GATE_PRIORITY)
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
                return memcap.note(p.stdout + p.stderr, f"{name} failed (exit {p.returncode}) in the worktree after "
                                   f"merging {self.main}, before review: `{cmd}`: {tail}. Merge {self.main} and "
                                   "rerun the gate before you hand back")
        return self.acceptance_problem(wt, fid) or self.footprint_problem(wt, fid)

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
        if getattr(self, "drive_id", "") and fid in self.slots and                 drivelock.read_marker(self.slot_dir(self.slots[fid])).get("fid") not in (None, fid):
            self.rebuild_slot(wt, fid, f"slot-{self.slots[fid]} was last built by "
                              f"{drivelock.read_marker(self.slot_dir(self.slots[fid])).get('fid')}")
        for node, cmd in land_mod.acceptance_cmds(tmpl, self.fns[fid].acceptance):
            p = self.sh(cmd, wt, fid)
            if p is not None and p.returncode != 0 and (why := self.slot_mismatch(wt, fid, p.stdout + p.stderr)):
                self.rebuild_slot(wt, fid, why)  # another build's binary: rebuild and run again, never a red
                p = self.sh(cmd, wt, fid)
            if p is None:
                return f"acceptance {node} timed out in the worktree after merging {self.main}: `{cmd}`"
            if p.returncode != 0:
                out = self.ov / "runs" / f"{fid}-acceptance.out"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(p.stdout + p.stderr, encoding="utf-8")
                tail = " / ".join([ln.strip() for ln in (p.stdout + p.stderr).splitlines() if ln.strip()][-8:])
                return memcap.note(p.stdout + p.stderr, f"acceptance red in the worktree after merging {self.main}, "
                                   f"before review: {node} (exit {p.returncode}): {tail} (full output: "
                                   f"{out.as_posix()}). Make it pass, then hand back")
        return ""

    def build_problem(self, job: Job, text: str) -> str:
        """Why this build cannot go to review, or ''. Commits leftover changes first."""
        if job.timed_out:
            return (f"builder killed: {job.idle}; never run a command that can hang (ssh, a server, a watch) "
                    "without a timeout" if job.idle else f"builder timed out after {self.timeout_s / 60:g} min and "
                    "was killed; work in smaller steps and commit as you go")
        if job.proc.returncode != 0:
            why = last_line(text) or last_line(read_text(job.out.with_suffix(".err"))) or "no output"
            return memcap.note(text, f"builder exited rc={job.proc.returncode}: {why}")
        wt, branch = self.ov / "wt" / job.fn, f"fn/{job.fn}"
        try:
            if git(wt, "status", "--porcelain").strip():
                git(wt, "add", "-A")
                git(wt, "commit", "-q", "-m", f"{job.fn}: {job.kind} attempt {job.attempt} (committed by drive.py)")
        except DriveError as e:
            return f"auto-commit of the builder's leftover changes failed: {e}"
        changed = git(self.repo, "diff", "--name-only", *self.span(branch)).splitlines()
        if not changed:
            return f"builder left no committed change on {branch}"
        hits = [p for p in changed if p.startswith(self.protected)]
        if hits:
            return f"changed protected files ({', '.join(hits[:5])}); never edit tests/acceptance, spec or overhaul.toml"
        if job.fn in self.fns and (vis := self.visibility_problem(self.fns[job.fn], changed, branch)):
            return vis
        return ""  # the gate, cycles included, runs after the pre-review merge (sync_problem)

    def cycles_problem(self, wt: Path) -> str:
        """The builder gate's cycles check, run on the branch before review: land.py runs it again on the merged
        tree, so a cycle or a broken Public line found only there costs a review and a land."""
        cmd = self.cfg["commands"].get("cycles")
        if not cmd:
            return ""
        try:
            p = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=self.timeout_s, stdin=subprocess.DEVNULL, **GATE_PRIORITY)
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
    def policies(self, f: Fn | None = None) -> str:
        """The `## Policies` section of ARCHITECTURE.md: concurrency, persistence and error owners. With
        [harness] trim_policies and a function, its id lists keep only that id and its domain rules only the homes in
        its own module or a module it depends on, so a big plan's briefs stay small."""
        text = module_section(self.arch, "Policies")
        if not text:
            return "(spec/ARCHITECTURE.md declares no Policies section; re-run plan_validate.py)"
        if f is None or not self.cfg["harness"].get("trim_policies"):
            return text
        mods = {f.module} | set(module_deps(self.arch, f.module))
        lists = set(ID_LISTS) | {str(k).lower() for k in (self.cfg.get("plan") or {}).get("policy_lists") or {}}
        out = []
        for line in text.splitlines():
            m = re.match(r"^([\s>*-]*\**\s*)([A-Za-z][\w -]*?)(\s*\**\s*:\s*)(.*)$", line)
            key = m.group(2).strip().lower() if m else ""
            if key in lists:
                if f.id in [x.strip("`") for x in re.split(r"[,;\s]+", m.group(4))]:
                    out.append(f"{m.group(1)}{m.group(2)}{m.group(3)}{f.id}")
            elif key == "domain rules":
                keep = [e.strip() for e in m.group(4).split(";")
                        if (h := re.search(r"->\s*`?([\w\-]+)\.", e)) and h.group(1) in mods]
                if keep:
                    out.append(f"{m.group(1)}{m.group(2)}{m.group(3)}{'; '.join(keep)}")
            else:
                out.append(line)
        out.append("(Trimmed to this function: id lists show only its id, domain rules only homes in "
                   f"{', '.join(sorted(mods))}; the full section is in spec/ARCHITECTURE.md.)")
        return "\n".join(out)

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
        span = self.span(branch)  # the row's own diff: a stacked row's deps are not under review
        brief = render("review-brief.md", {
            "id": fid, "branch": branch, "row": self.row_table(f), "allowed_paths": self.allowed(f),
            "acceptance_source": self.acceptance_text(f), "review_file": review_file.as_posix(),
            "policies": self.policies(f), **values, "checklist_path": CHECKLIST.as_posix(),
            "checklist_version": checklist()[0],
            "checklist": checklist_text(applicable_items(scope, bool(f.rules))),
            "round_rule": self.round_rule(fid), "helpers": self.helpers_text(f),
            "index_dir": index_api.INDEX_DIR,
            "diffstat": git(self.repo, "diff", "--stat", *span).rstrip(),
            "diff": git(self.repo, "diff", *span).rstrip(),
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
        # pf-F: a verified duplicate NO always fails review (pilot2026-11-10: 20 passed "recorded as plan gap").
        # Only one another module must change for (its owner has to export the item) is a plan gap AND a depfix of
        # that module; the function waits for it without spending an attempt, then retries with the NO.
        stuck = self.gap_nos(fid, gap, real)
        if stuck and all(n["id"] in DEDUP_IDS for n in stuck) and len(stuck) == len(real):
            if self.dup_depfix(fid, gap, real, job, secs, tokens):
                return
            stuck = []  # no other module to change: the builder reuses the existing item itself
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
        self.fail(fid, self.review_nos(fid, job, real))

    def review_nos(self, fid: str, job: Job, real: list[dict]) -> str:
        """Record a failed review's verified NOs (state and reviews/<id>-nos.md); the retry's reason line."""
        prev = self.st["functions"][fid].get("review") or {}
        self.set(fid, review={"head": job.head, "ids": sorted({n["id"] for n in real}),
                              "round": prev.get("round", 0) + 1})
        full = self.ov / "reviews" / f"{fid}-nos.md"
        listing = format_nos(real)
        full.write_text(f"# Verified blocking NOs for {fid}: fix every one\n\n{listing}\n", encoding="utf-8")
        head = f"review FAIL: {len(real)} verified blocking NOs; fix every one (full list: {full.as_posix()}):\n"
        return head + (listing if len(head) + len(listing) <= REASON_MAX
                       else listing[:REASON_MAX - len(head) - 4] + " ...")

    def dup_depfix(self, fid: str, gap: str, real: list[dict], job: Job, secs: float, tokens: int | None) -> bool:
        """Verified duplicate NOs only another module can clear (it must export the item): a plan gap, plus one fix
        pass in that module (depfix-dup-<id>); the function fails review, waits for it without spending an attempt
        and retries with the NOs. False when no other owner module is found or this function had its dup depfix."""
        rec, own = self.st["functions"][fid], self.fns[fid].module
        owners = [self.owner_module(str(n.get("other") or "").split(":")[0]) or str(n.get("module") or "")
                  for n in real] + self.gap_modules([{"gap": gap}] if gap else [])
        module = next((m for m in owners if m and m != own), "")
        dfid = f"{DEPFIX_PREFIX}dup-{fid}"
        if not module or rec.get("dupfixes", 0) >= 1 or dfid in self.jobs or dfid in self.polish_lands:
            return False
        note = " / ".join(f"[{n['id']} {where(n)}] {n.get('evidence', '')} {n.get('fix', '')}" for n in real)
        self.keep_gap(fid, f"{gap} / {note}" if gap else note)
        self.log(fid, "review_fail", f"{len(real)} verified dedup NOs ({', '.join(n['id'] for n in real)}) need "
                 f"{module} to export the existing item: plan gap + {dfid} model={job.model}", secs, tokens)
        reason = self.review_nos(fid, job, real)
        self.set(fid, status="planned", dupfixes=rec.get("dupfixes", 0) + 1, blocked_on=dfid,
                 last_reason=f"waiting on {dfid}; then {reason}"[:REASON_MAX])
        items = [{"file": str(n.get("other") or where(n)).split(":")[0], "line": "?",
                  "rule": f"duplicate found by the review of {fid} ({n['id']})",
                  "evidence": f"{n.get('evidence', '')}. Export the existing item from {module} (no behaviour "
                              f"change) so {fid} in {own} can import it instead of its copy at {where(n)}."}
                 for n in real]
        self.start_polish_fix(module, items, fid=dfid)
        return True

    # ---- land ----
    def footprint(self, fid: str) -> list[str]:
        """What a land touches: the function's claimed files plus every file its branch changes."""
        changed = run(["git", "diff", "--name-only", *self.span(f"fn/{fid}")], self.repo).stdout.split()
        return [*self.claims(self.fns[fid]), *changed]

    def footprint_modules(self, fid: str, files: list[str]) -> set[str]:
        """The land's modules: the function's own plus every module whose ARCHITECTURE Paths hold one of files."""
        mods = {self.fns[fid].module} if fid in self.fns else set()
        for m in dict.fromkeys(f.module for f in self.order):
            if m not in mods and paths_overlap(files, module_paths(module_section(self.arch, m), m)):
                mods.add(m)
        return mods - {""}

    def land_footprint(self, fid: str) -> tuple[list[str], set[str]]:
        files = self.footprint(fid)
        return files, self.footprint_modules(fid, files)

    def pump_land(self) -> bool:
        """Start the next land (land.py --parallel): it gates on main outside the lock in its own worktree and
        target dir, and takes the lock only to commit. Beside lands in flight (up to [harness] land_parallel) only
        the first queued function whose footprint (files and modules) overlaps none of theirs starts; when it
        commits after them it re-merges main and reruns only what their landed footprints affect."""
        router.hold_lands(self)  # rows that rerun a test failing on clean main wait off the queue for its fix
        self.land_queue.sort(key=self.rank)  # critical path first, as builds start (stable within a rank)
        flying = self.land_jobs()
        if (not self.land_queue or len(flying) >= self.max_lands or any(j.kind != "land" for j in flying)
                or self.st.get("main_red")):
            return False
        queue = [q for q in self.land_queue if stack.landable(self, q)]  # a stacked row after its deps
        if not queue:
            return False
        fid = queue[0]
        if flying:
            taken = [self.land_footprint(j.fn) for j in flying]

            def clear(q: str) -> bool:
                files, mods = self.land_footprint(q)
                return not any(paths_overlap(files, tf) or mods & tm for tf, tm in taken)
            fid = next((q for q in queue if clear(q)), "")
            if not fid:
                return False
        self.land_queue.remove(fid)
        out = self.ov / "runs" / f"{fid}-a{self.st['functions'][fid]['attempts'] + 1}-land.out"
        # land.py takes .overhaul/lock itself (a stacked land only to commit), so drive.py holds nothing meanwhile.
        cmd = [sys.executable, str(self.scripts / "land.py"), "--repo", str(self.repo), "--fn", fid,
               "--main", self.main, "--parallel"]
        used = {j.slot for j in flying}
        k = next(i for i in range(len(used) + 1) if i not in used)
        cmd += ["--target-dir", str(self.ov / "target" / f"land-{k}")]  # one per concurrent land, reused
        log = self.ov / "log.jsonl"
        log_at = log.stat().st_size if log.exists() else 0
        start = time.monotonic()
        # land.py stops itself at land_timeout_min (lock waits excluded); this is the backstop for a hung land
        limit = max(2 * self.timeout_s, 2 * self.land_timeout_s + 600)
        job = Job(fid, "land", spawn(cmd, self.repo, out), out, start, start + limit, 0, log_at=log_at, slot=k)
        if flying:
            self.side_lands[fid] = job
            self.log(fid, "land_parallel", f"beside {', '.join(j.fn for j in flying)}")
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
            self.periodic.landed()
            self.say(f"landed {fid}")
            self.record_proven()
            stack.landed(self, fid)  # rows stacked on it merge the new main
            self.cycle_due = f"landed {fid}"  # run after launch(): dependents start before verdict and dash
            return
        reason = "land.py timed out" if job.timed_out else (
            last_line(out) or last_line(err) or f"land.py exit {job.proc.returncode}")
        if not self.land_logged(fid, job.log_at):  # killed or crashed before land.py wrote its own land_fail line
            self.log(fid, "land_fail", reason, secs)
        if reason.startswith(land_mod.MAIN_FLAKE) and self.main_flake(fid, reason):
            return  # a test that fails on clean main too: its owner gets a fix, the function lands after it
        if reason.startswith(land_mod.LAND_TIMEOUT) and self.land_timed_out(fid, reason):
            return
        if GREEN_BEFORE in reason:
            self.park(fid, f"land: {reason}")
        elif CONFLICT in reason.lower() and self.start_conflict(fid, reason):
            return  # main moved under the branch: a conflict-fix pass, not an attempt
        else:
            self.fail(fid, f"land: {reason}")

    def land_timed_out(self, fid: str, reason: str) -> bool:
        """land.py stopped at [harness] land_timeout_min. The first time the function is not charged an attempt: it
        goes back to the land queue (its land_timeout event names the slowest test, for the plan to split it or move
        it to [harness] heavy_tests). False from the second on: an ordinary failure."""
        n = int(self.st["functions"][fid].get("land_timeouts", 0)) + 1
        self.set(fid, land_timeouts=n, last_reason=reason[:REASON_MAX])
        if n > 1:
            return False
        self.set(fid, status="landing")
        self.say(f"{fid} land timed out (not charged, requeued): {reason}")
        if fid not in self.land_queue:
            self.land_queue.append(fid)
        return True

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
            if isinstance(rec, dict) and rec.get("fn") == fid and rec.get("event") in ("land_ok", "land_fail",
                                                                                       "land_timeout"):
                return True
        return False

    # ---- polish: once every function is proven, a blind audit, fix passes per module, then a re-audit ----
    # Per phase (item 8): with the phase barrier on, when every function of phase N (not the last) is proven, the
    # same gap pass, probes and audit/fix rounds run scoped to phase N before phase N+1 starts. Its state is
    # st["phase_polish"][N] and its files .overhaul/polish/phase-N/; the final whole-tree polish still runs at the end.
    # pf-F (pilot2026-11-10, 0 phase audits in 3 days): a final polish that ended before the plan grew (status
    # done) no longer blocks every phase audit, phases are scanned past the first unfinished one, and a phase is also
    # due after [harness] phase_audit_every lands since its last audit (its open rows wait on needs-plan for days).
    # A lands audit ("why": "lands") locks only what its fixes edit; a done audit re-arms on the next trigger.
    def polish_phase(self) -> int | None:
        """The phase whose polish is due or running now; None for the final whole-tree polish (or none at all)."""
        phases = sorted({phase_key(f)[0] for f in self.order})
        fin = self.st.get("polish") or {}
        if self.phase_audit_on() and self.polish_rounds >= 1:
            for n in self.st.get("phase_force") or []:  # watchdog audit-phase-<N>: ahead of order and final polish
                if self.st.get("phase_polish", {}).get(str(n), {}).get("status") not in ("done", "failed"):
                    return n
        if (not self.phase_audit_on() or len(phases) < 2 or self.polish_rounds < 1
                or fin.get("status", "idle") not in ("idle", "done", "failed")):
            return None  # the final polish is running: no phase polish beside it
        ended = self.st.get("phase_polish") or {}
        for k, pol in ended.items():  # one already running keeps the slot until it ends
            if pol.get("status") in ("gaps", "audit", "fix") or (pol.get("status") == "idle" and "locked" in pol):
                return int(k)
        for n in phases:
            if (why := self.phase_due(n, n == phases[-1])) is None:
                continue
            self.st["phase_polish"], pol = ended, ended.get(str(n))
            fresh = {"round": 0, "status": "idle", "issues": None, "history": []}
            if pol is not None and pol.get("status") in ("done", "failed"):  # re-arm: a new audit of this phase
                fresh.update(history=pol.get("history", []), at=pol.get("at", 0), gaps_done=pol.get("gaps_done", []))
                ended[str(n)] = fresh
            ended.setdefault(str(n), fresh)["why"] = why
            return n
        return None

    def phase_due(self, n: int, last: bool) -> str | None:
        """Why phase n's audit is due now: "complete" (every row proven, not the last phase: the final polish covers
        that), "lands" (phase_audit_every of its rows proven since its last audit), or None."""
        fns = self.phase_fns(n)
        proven = sum(1 for f in fns if self.st["functions"][f.id]["status"] == "proven")
        pol = (self.st.get("phase_polish") or {}).get(str(n)) or {}
        ended = pol.get("status") in ("done", "failed")
        at = int(pol.get("at", len(fns) if ended else 0))  # a done record without "at" audited the whole phase
        if proven == len(fns):
            return None if last or (ended and at >= proven) else "complete"
        if not self.phase_audit or self.phase_audit_every < 1:
            return None
        return "lands" if proven - at >= self.phase_audit_every else None

    def phase_fns(self, n: int | None) -> list[Fn]:
        """The (non-deferred) functions in polish scope: phase n's, or every one for the final polish."""
        return [f for f in self.order if n is None or phase_key(f)[0] == n]

    def polish_dir(self) -> Path:
        n = self.polish_phase()
        return self.ov / "polish" / (f"phase-{n}" if n is not None else "")

    def polish_tag(self) -> str:
        n = self.polish_phase()
        return f"p{n}-" if n is not None else ""

    def polish_state(self) -> dict:
        """st["polish"] (or st["phase_polish"][N] while phase N is polished): round, status (idle | gaps | audit |
        fix | done | failed), issues and [round, issues] history."""
        n, fresh = self.polish_phase(), {"round": 0, "status": "idle", "issues": None, "history": []}
        if n is not None:
            return self.st.setdefault("phase_polish", {}).setdefault(str(n), fresh)
        return self.st.setdefault("polish", fresh)

    def polish_pending(self) -> bool:
        land = self.land_job is not None and self.land_job.kind == "polish_land"
        return land or bool(self.polish_queue or self.polish_lands) or any(
            j.kind in POLISH_KINDS for j in self.jobs.values())

    def polish_step(self) -> bool:
        """Drive the polish phase one step; True if anything started."""
        pol = self.polish_state()
        if self.polish_rounds < 1 or pol["status"] in ("done", "failed"):
            return False
        n = self.polish_phase()  # deferred rows are out of the plan: polish never waits for them
        if n is None and any(self.st["functions"][fid]["status"] != "proven" for fid in self.fns):
            return False
        if n is not None and "locked" not in pol:  # what audit_locked holds from now until this polish ends
            mine = set() if pol.get("why") == "lands" else {f.module for f in self.phase_fns(n)}
            pol["locked"] = sorted(mine | set(self.gap_modules(self.scope_gaps())))
            pol["at"] = sum(1 for f in self.phase_fns(n) if self.st["functions"][f.id]["status"] == "proven")
            locks = ", ".join(pol["locked"]) or "-"
            self.log(None, "phase_audit", f"phase {n} start ({pol.get('why', 'complete')}): {pol['at']} of "
                                          f"{len(self.phase_fns(n))} rows proven, locks {locks}")
        started = self.pump_polish_land()
        if self.probe_waiting(n) or self.sweep_state()["status"] != "idle":
            return started
        if pol["status"] == "fix":
            for item in self.polish_ready():
                self.polish_queue.remove(item)
                self.start_polish_fix(*item)
                started = True
            if not self.polish_pending():
                return self.start_polish_audit(pol["round"] + 1)
        elif pol["status"] == "gaps":  # the plan-gaps pass runs alone; polish round 1 waits for it
            if not self.polish_pending() and not self.polish_busy():
                return self.start_polish_audit(max(1, pol["round"]))
        elif not self.polish_busy() and not self.polish_pending():  # idle, or an audit left unfinished
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
        gaps = self.scope_gaps()
        if not gaps:
            return False
        modules = self.gap_modules(gaps)
        items = [{"text": "These are plan gaps the function reviews recorded: duplicated constants, messages or "
                          "helpers another module should own or export. For each one, export it from the owner "
                          "module and make every caller import it, all in this pass. No behaviour may change; skip "
                          "a gap that would change behaviour and say so in your hand-back.\n"}]
        items += [{"text": f"- [plan gap from {e.get('fn', '?')}, module {e.get('module', '?')}] {e['gap']}"}
                  for e in gaps]
        pol.update(gaps="started", status="gaps", gaps_done=sorted({*(pol.get("gaps_done") or []),
                                                                   *(str(e.get("gap")) for e in gaps)}))
        self.save()
        self.log(GAPS_FID, "plan_gaps", f"start: {len(gaps)} gaps across {', '.join(modules)}")
        self.start_polish_fix(", ".join(modules), items, fid=GAPS_FID)
        return True

    def polish_claims(self, module: str) -> list[str]:
        out = []
        for m in module.split(", ") + self.polish_owners.get(module, []):  # gaps: several; owners: visibility edits
            section = module_section(self.arch, m)
            out += module_paths(section, m) + module_shared(section)
        return out

    def polish_ready(self) -> list[tuple[str, list[dict]]]:
        """Queued fix passes that may start now: a free slot each and no path overlap with a fix in flight."""
        held = [fid for fid, j in self.jobs.items() if j.kind in ("polish_fix", "polish_review")] + self.polish_lands
        if self.land_job and self.land_job.kind == "polish_land":
            held.append(self.land_job.fn)
        taken = [self.polish_claims(self.polish_meta[fid][0]) for fid in held if fid in self.polish_meta]
        out, free = [], self.max_parallel - self.local_jobs()
        queued = {m for m, _ in self.polish_queue}
        for item in self.polish_queue:
            if len(out) >= free:
                break
            if queued & set(self.polish_after.get(item[0], [])):
                continue  # owner-first: an owner's own fix starts (and lands) before its caller's
            if self.builds_in(item[0].split(", ") + self.polish_owners.get(item[0], [])):
                continue  # phase audit beside builds: a build in flight there lands first (no races)
            mine = self.polish_claims(item[0])
            if not any(paths_overlap(mine, other) for other in taken):
                taken.append(mine)
                out.append(item)
        return out

    def polish_cmd(self, kind: str) -> tuple[list[str], str]:
        """The worker command for a polish kind, and its model label."""
        custom = self.custom_cmd("review" if kind in ("polish_audit", "arch_audit", "polish_review", "polish_verify")
                                 else "build")
        if custom:
            return custom, "custom"
        base = self.worker_cmd({"polish_review": "review", "polish_verify": "verify"}.get(kind, "build"), kind)
        if kind in ("polish_audit", "arch_audit") and self.audit_model:
            base = (base[:base.index("--model")] if "--model" in base else base) + ["--model", self.audit_model]
        return base, base[base.index("--model") + 1] if "--model" in base[:-1] else "default"

    def start_polish_job(self, fid: str, kind: str, rnd: int, brief: Path, cwd: Path) -> None:
        base, model = self.polish_cmd(kind)
        out = self.ov / "runs" / f"{fid}-r{rnd}-{kind}.json"
        try:
            proc = spawn([*base, f"Read and do {brief.as_posix()}"], cwd, out, worker.env(base, self.slot_env(fid)))
        except OSError as e:
            raise DriveError(f"cannot start the {kind} worker {base[0]}: {e}") from e
        start = time.monotonic()
        self.jobs[fid] = self.watch(Job(fid, kind, proc, out, start, start + self.timeout_s, rnd, model=model), base)

    def polish_commands(self) -> str:
        lines = [f"- {name}: `{cmd}`" for name, cmd in self.cfg["commands"].items() if "{test}" not in cmd]
        return "\n".join(lines) or "(overhaul.toml has no whole-tree [commands])"

    def polish_allowed(self, module: str, fid: str = "") -> str:
        mods = module.split(", ")
        paths = [p for m in mods for p in module_paths(module_section(self.arch, m), m)]
        tests = unit_dirs(self.cfg["commands"].get("test", "")) or ["tests/unit/"]
        return "\n".join([f"- `{p}`" for p in paths] + [f"- `{t}` (unit tests for this module only)" for t in tests]
                         + (["- `tests/property/` (add property tests; never edit a planned one)"]
                            if fid.startswith(PROBE_PREFIX) else [])
                         + self.vis_line(f"the modules `{module}` depends on", self.dep_paths(mods)))

    def start_polish_audit(self, rnd: int) -> bool:
        """The fixed checklist over the whole tree plus the dedup role (template polish-audit.md)."""
        fid, pol = "polish-audit", self.polish_state()
        audit = self.polish_dir() / f"audit-{rnd}.json"
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.unlink(missing_ok=True)
        modules = sorted({f.module for f in self.phase_fns(self.polish_phase())})
        brief = render("polish-audit.md", {
            "scope": self.polish_scope(rnd), "round": str(rnd), "main": self.main, "audit_file": audit.as_posix(), "max_calls": str(self.max_calls),
            "scratch": self.polish_dir().as_posix(), "policies": self.policies(), "commands": self.polish_commands(),
            "standard": STANDARD.as_posix(), "checklist_path": CHECKLIST.as_posix(),
            "checklist_version": checklist()[0], "checklist": checklist_text(applicable_items(None)),
            "modules": "\n".join(f"- `{m}`: " + ", ".join(module_paths(module_section(self.arch, m), m)) for m in modules)})
        path = self.ov / "briefs" / f"polish-audit-{self.polish_tag()}{rnd}.md"
        path.write_text(brief, encoding="utf-8")
        self.start_polish_job(fid, "polish_audit", rnd, path, self.prepare_worktree(fid))
        pol.pop("checklist_wait", None)
        if rnd == 1 and self.phase_audit and "arch" not in pol and ARCH_FID not in self.jobs:
            self.start_arch_audit(ARCH_FID, self.polish_phase(), self.polish_dir() / "arch-1.json")
        pol.update(round=rnd, status="audit")
        self.save()
        if (n := self.polish_phase()) is not None:
            self.log(None, "phase_audit", f"phase {n} round {rnd} start")
        self.say(f"polish checklist round {rnd}")
        return True

    def polish_scope(self, rnd: int) -> str:
        """Round 1 audits the whole tree. A later round re-checks the previous round's verified NOs and audits only
        the files its fixes changed, like a function's re-review (trial 11: a whole-tree re-audit timed out twice)."""
        best, prev = self.polish_state().get("best"), self.polish_dir() / f"checklist-{rnd - 1}.json"
        if rnd < 2 or not best or not prev.is_file():
            n = self.polish_phase()
            if n is None:
                return "Round 1: the whole tree."
            fns = self.phase_fns(n)
            files = sorted({p for f in fns for p in f.files})
            return (f"Round 1 of the phase {n} polish: every function of phase {n} ({', '.join(f.id for f in fns)}) is "
                    f"proven; {'later phases are not built yet' if self.barrier else 'later phases build meanwhile'}. "
                    "Answer the checklist only for the modules listed above"
                    + (" and these files: " + ", ".join(f"`{p}`" for p in files) if files else "")
                    + ". The dedup greps still search the whole tree. Leave every other module and anything that "
                    "only a later phase can finish to the final whole-tree polish.")
        changed = [f for f in git(self.repo, "diff", "--name-only", f"{best[2]}..{self.main}").split() if f]
        if self.beside_builds() and self.polish_phase() is not None:  # builds landed meanwhile: not this round's
            mine = [p for m in self.polish_state().get("locked") or []
                    for p in module_paths(module_section(self.arch, m), m)]
            changed = [f for f in changed if in_paths(f, mine)]
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
        raw = None if job.timed_out else self.read_audit(self.polish_dir() / f"audit-{rnd}.json")
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
        if rnd == 1 and self.phase_audit:
            pol = self.polish_state()
            if "arch" not in pol:  # the arch audit still runs: it joins this round when it ends
                pol["checklist_wait"] = {"meta": meta, "blocking": blocking}
                self.save()
                return
            blocking += pol["arch"]
        self.polish_audited(meta, blocking)

    def polish_audited(self, meta: dict, blocking: list[dict]) -> None:
        """The round's blocking NOs (checklist, plus round 1's arch audit) go to one verifier."""
        rnd = meta["rnd"]
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
        if fid == SWEEP_VERIFY:
            self.sweep_checked(meta, real)
            return
        if fid != "polish-verify":  # a polish fix's review NOs
            if real is None:
                self.polish_failed(fid, "verifier wrote no result; the review's NOs were not checked")
            elif real:
                if not self.retry_polish_fix(fid, real, meta.get("head", ""), job.attempt):
                    self.polish_failed(fid, f"review: {len(real)} verified blocking NOs: {format_nos(real)}", real)
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
        (self.polish_dir() / f"checklist-{rnd}.json").write_text(json.dumps(real, indent=1), encoding="utf-8")
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
        groups = self.route_polish(issues)
        self.polish_queue = sorted(groups.items())
        pol["status"] = "fix"
        self.save()

    def named_modules(self, item: dict) -> list[str]:
        """The plan's modules an issue's text names, by a path of theirs or by name."""
        text = " ".join(str(item.get(k) or "") for k in ("evidence", "how", "fix", "issue", "other"))
        out = []
        for m in sorted({f.module for f in self.order}):
            paths = [p.rstrip("/") for p in module_paths(module_section(self.arch, m), m)]
            if any(p and p in text for p in paths) or re.search(rf"(?<![\w/.-]){re.escape(m)}(?![\w-])", text):
                out.append(m)
        return out

    def route_polish(self, issues: list[dict]) -> dict[str, list[dict]]:
        """Group a round's issues by module (pass E). A duplication issue (C03/C05) that names another module goes,
        as one fix, to the module that depends on every other one involved (the caller): it may make the owner's item
        visible (visibility-only edits in its deps) and import it. An owner with issues of its own this round must
        change more than visibility, so its fix lands first (polish_after)."""
        groups: dict[str, list[dict]] = {}
        for item in issues:
            home = str(item.get("module") or "unknown").strip()
            if str(item.get("id") or item.get("rule") or "") in DUP_IDS + ARCH_DUP_IDS:
                involved = [home] + [m for m in self.named_modules(item) if m != home]
                callers = [m for m in involved if all(o == m or o in module_deps(self.arch, m) for o in involved)]
                if len(involved) > 1 and callers:
                    home = callers[0]
                    item = {**item, "module": home, "owners": [m for m in involved if m != home]}
            groups.setdefault(home, []).append(item)
        self.polish_owners = {m: sorted({o for i in items for o in i.get("owners", [])}) for m, items in groups.items()}
        self.polish_after = {m: [o for o in owners if o in groups] for m, owners in self.polish_owners.items()}
        for m, after in self.polish_after.items():
            if after:
                self.log(None, "polish_order", f"{m} waits for {', '.join(after)}: cross-module duplication")
        return groups

    def keep_best_polish(self, pol: dict, rnd: int, issues: list[dict]) -> tuple[list[dict], int]:
        """A fix round that raised the verified NO count is reverted: main goes back to the best checked tree.

        pol["best"] is [round, issues, sha] of the run with the fewest verified NOs so far. Returns the list
        the next fix round works from (the best round's) and its count."""
        sha, best = git(self.repo, "rev-parse", self.main).strip(), pol.get("best")
        if not best or len(issues) <= best[1]:
            pol["best"] = [rnd, len(issues), sha]
            return issues, len(issues)
        kept = self.read_audit(self.polish_dir() / f"checklist-{best[0]}.json")
        beside = self.beside_builds() and self.polish_phase() is not None  # a revert would drop builds' lands too
        if kept is None or beside or not self.revert_main(best[2], sha, f"polish: revert round {rnd - 1} fixes "
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
        pol = self.polish_state()
        rnd = 0 if fid else pol["round"]
        carry = None if fid else (pol.get("failed_nos") or {}).pop(module, None)
        fid = fid or f"polish-r{rnd}-" + slug(module)
        issues = "\n".join(i["text"] if "text" in i else f"- `{i.get('file', '?')}:{i.get('line', '?')}` "
                           f"[{i.get('rule') or i.get('id') or '?'}] {i.get('evidence') or i.get('issue') or ''}"
                           + self.owners_note(module, i.get("owners")) for i in items)
        if carry and carry.get("round") == rnd - 1:
            issues += (f"\n\nThe round {carry['round']} fix of this module failed its review on these verified "
                       f"blocking NOs. Do not repeat them:\n{carry['nos']}")
        self.polish_meta[fid] = (module, issues)
        self.polish_tries.pop(fid, None)
        path = self.polish_fix_brief(fid, rnd, module, issues)
        self.start_polish_job(fid, "polish_fix", rnd, path, self.prepare_worktree(fid))
        self.log(fid, self.fix_event(fid), f"start {module}: {len(items)} issues model={self.jobs[fid].model}")
        self.say(f"polish fix {fid}")

    @staticmethod
    def owners_note(caller: str, owners: list[str] | None) -> str:
        if not owners:
            return ""
        names = ", ".join(f"`{o}`" for o in owners)
        return (f" (cross-module duplication: `{caller}` depends on {names}. Keep the owner's copy, make it `pub` or "
                f"re-export it there with a visibility-only edit, import it in `{caller}` and delete the copy here)")

    def polish_fix_brief(self, fid: str, rnd: int, module: str, issues: str) -> Path:
        brief = render("polish-fix-brief.md", {
            "id": fid, "round": str(rnd), "module": module, "branch": f"fn/{fid}", "max_calls": str(self.max_calls),
            "issues": issues, "allowed_paths": self.polish_allowed(module, fid), "policies": self.policies(),
            "commands": self.polish_commands()})
        path = self.ov / "briefs" / f"{fid}.md"
        path.write_text(brief, encoding="utf-8")
        return path

    def retry_polish_fix(self, fid: str, real: list[dict], head: str, rnd: int) -> bool:
        """Like a build retry: a fix whose review has verified blocking NOs gets fix_retries more passes on its own
        branch, carrying the NO list; the round rule then applies to its review. False once they are spent."""
        prev = self.polish_tries.get(fid) or {}
        n = prev.get("round", 0) + 1
        if n > self.fix_retries or fid not in self.polish_meta or not head:
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
        self.log(fid, self.fix_event(fid), f"retry {n}/{self.fix_retries}: {len(real)} verified blocking NOs model="
                 f"{self.jobs[fid].model}: " + " / ".join(f"{x['id']} {where(x)}" for x in real))
        self.say(f"polish fix {fid}: retry {n}")
        return True

    def after_polish_fix(self, job: Job) -> None:
        tokens, turns, text = read_result(job.out)
        self.log(job.fn, self.fix_event(job.fn), f"built rc={job.proc.returncode} turns={turns}/{self.max_calls}",
                 time.monotonic() - job.started, tokens)
        module, issues = self.polish_meta[job.fn]
        problem = self.build_problem(job, text) or self.polish_vis_problem(job.fn, module, f"fn/{job.fn}")
        if problem:
            self.polish_failed(job.fn, problem)
            return
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
        if (self.land_job or self.side_lands or (self.land_queue and not self.st.get("main_red"))
                or not self.polish_lands):
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
            self.periodic.landed()  # a fix on main: the gates run again
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

    def polish_failed(self, fid: str, reason: str, nos: list[dict] | None = None) -> None:
        """A fix pass that cannot land is dropped; the re-audit finds its issues again. A round fix's verified review
        NOs go into the next round's fix brief for its module (pass E)."""
        self.log(fid, self.fix_event(fid), f"failed: {reason[:REASON_MAX]}")
        if nos and fid in self.polish_meta and re.match(r"polish-r\d+-", fid):
            pol = self.polish_state()
            pol.setdefault("failed_nos", {})[self.polish_meta[fid][0]] = {"round": pol["round"],
                                                                         "nos": format_nos(nos)}
            self.save()
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
         "polish_review": self.after_polish_review, "polish_land": self.after_polish_land,
         "arch_audit": self.after_arch_audit}[job.kind](job)

    # ---- phase audit (owner 2026-11-09): phase polish beside the builds, the arch audit, and the sweep ----
    # [harness] phase_audit runs each phase's polish (above) whatever phase_barrier says. Without the barrier holding
    # later phases it runs beside their builds: the modules it covers start no new build until it ends, and a fix
    # waits for builds in flight in its modules. Round 1 also runs the arch audit (templates/arch-audit.md); its
    # blocking NOs join the checklist's, verified and fixed the same way. Every arch_audit_every_lands lands the arch
    # audit runs over the whole tree on its own (the sweep, state arch_sweep), never beside a phase polish.
    def phase_audit_on(self) -> bool:
        return (self.barrier and self.phase_polish) or self.phase_audit

    def beside_builds(self) -> bool:
        """True when the phase audit runs beside the builds (the barrier does not hold the next phase for it)."""
        return self.phase_audit and not (self.barrier and self.phase_polish)

    def builds_in(self, modules: list[str] | set[str]) -> bool:
        """True if a function (or the probe) of one of these modules is in flight."""
        mods, probes = set(modules), self.probe_modules()
        return any((self.fns[fid].module if fid in self.fns else probes.get(fid)) in mods for fid in self.busy())

    def polish_busy(self) -> bool:
        """What a polish step waits for before it starts: all work in flight, or, beside the builds, only other
        fix passes and the builds in the modules it locks."""
        if self.polish_phase() is None or not self.beside_builds():
            return bool(self.busy())
        probes = self.probe_modules()
        fixes = {fid for fid in self.busy() - {"periodic"} if fid not in self.fns and fid not in probes}
        return bool(fixes) or self.builds_in(self.audit_locked())

    def fix_open(self, fid: str) -> bool:
        return fid in self.jobs or fid in self.polish_lands or bool(self.land_job and self.land_job.fn == fid)

    def audit_locked(self) -> set[str]:
        """Modules that start no new build beside a phase audit: those of the phase polish due now (its phase's and
        its plan gaps'), and every module a queued or running polish or arch fix edits, until that fix lands."""
        if not self.beside_builds():
            return set()
        n, out = self.polish_phase(), set()
        if n is not None:  # due: its phase's modules at once, then what polish_step records (plus its plan gaps)
            pol = self.polish_state()  # a lands audit (open rows still to build) locks only what its fixes edit
            mine = set() if pol.get("why") == "lands" else {f.module for f in self.phase_fns(n)}
            out = set(pol["locked"]) if "locked" in pol else mine
        fixes = [m for m, _ in self.polish_queue] + [m for fid, (m, _) in self.polish_meta.items()
                                                    if fid.startswith(("polish-", "arch-")) and self.fix_open(fid)]
        for m in fixes:
            out |= set(m.split(", ")) | set(self.polish_owners.get(m, []))
        return out

    def start_arch_audit(self, fid: str, n: int | None, out: Path) -> None:
        """One blind reviewer (audit_model) reads ARCHITECTURE.md, every module's public API and the code in scope:
        phase n's modules for a phase polish, else the whole tree."""
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        scoped = fid == ARCH_FID and n is not None
        mine = {f.module for f in self.phase_fns(n)} if scoped else {f.module for f in self.order}
        mods = sorted({f.module for f in self.order})
        listing = "\n".join(f"- `{m}`{' (in scope)' if m in mine else ''}: "
                            + ", ".join(module_paths(module_section(self.arch, m), m)) for m in mods)
        scope = (f"phase {n}: the code of the modules marked (in scope); later phases may still be building"
                 if scoped else "the whole tree: every module")
        brief = render("arch-audit.md", {
            "title": f"{fid} {out.stem}", "scope": scope, "main": self.main, "audit_file": out.as_posix(),
            "max_calls": str(self.max_calls), "scratch": (out.parent / "scratch").as_posix(), "modules": listing,
            "architecture": self.cfg["paths"]["architecture"], "commands": self.polish_commands(),
            "policies": self.policies()})
        path = self.ov / "briefs" / f"{fid}-{out.parent.name}-{out.stem}.md"
        path.write_text(brief, encoding="utf-8")
        self.arch_meta[fid] = {"phase": n, "file": out.as_posix()}
        self.start_polish_job(fid, "arch_audit", 1, path, self.prepare_worktree(fid))
        self.log(None, "arch_audit", f"start {fid}: {scope} model={self.jobs[fid].model}")

    def after_arch_audit(self, job: Job) -> None:
        meta = self.arch_meta.pop(job.fn, {})
        tokens, _, _ = read_result(job.out)
        raw = None if job.timed_out or not meta else self.read_audit(Path(meta["file"]))
        nos = None if raw is None else [{**n, "evidence": f"{n.get('evidence') or ''} (other: {n['other']})"}
                                        if n.get("other") else n for n in checklist_nos(raw) or []]
        why = "" if nos is not None else "auditor timed out" if job.timed_out else "result missing or not a JSON list"
        blocking = [n for n in nos or [] if n["severity"] in BLOCKING]
        self.log(None, "arch_audit", f"{job.fn}: " + (f"failed: {why}" if why else
                 f"{len(nos)} NOs, {len(blocking)} blocking: " + " / ".join(f"{n['id']} {where(n)}" for n in blocking)
                 )[:REASON_MAX] + f" model={job.model}", time.monotonic() - job.started, tokens)
        if job.fn == SWEEP_FID:
            self.sweep_audited(blocking, why)
            return
        n = meta.get("phase")
        pol = self.st.setdefault("phase_polish", {}).setdefault(str(n), {}) if n is not None else self.polish_state()
        if why and not pol.get("arch_retry") and pol.get("status") == "audit":  # once more, like the checklist
            pol["arch_retry"] = 1
            self.start_arch_audit(ARCH_FID, n, Path(meta.get("file") or self.polish_dir() / "arch-1.json"))
            return
        if why:
            pol["arch_failed"] = why
        pol["arch"] = blocking
        wait = pol.pop("checklist_wait", None)
        self.save()
        if wait and pol is self.polish_state() and pol.get("status") == "audit":
            self.polish_audited(wait["meta"], wait["blocking"] + blocking)

    def sweep_state(self) -> dict:
        """st["arch_sweep"]: at (proven count at the last sweep), status (idle|audit|verify|fix), open, history."""
        return self.st.setdefault("arch_sweep", {"at": 0, "status": "idle", "history": []})

    def arch_step(self) -> bool:
        """Drive the sweep one step; True if anything started."""
        sw = self.sweep_state()
        if not self.phase_audit or self.arch_every < 1:
            return False
        if sw["status"] == "fix":
            started = self.pump_polish_land()
            for item in self.polish_ready():
                self.polish_queue.remove(item)
                self.start_polish_fix(*item, fid=f"arch-r{sw['at']}-{slug(item[0])}")
                started = True
            if not self.polish_queue and not self.polish_pending():
                sw["status"] = "idle"
                self.save()
                self.log(None, "arch_sweep", f"done at {sw['at']} lands: {sw.get('open', 0)} verified NOs fixed")
            return started
        if sw["status"] != "idle":
            if SWEEP_FID not in self.jobs and SWEEP_VERIFY not in self.jobs:  # its job ended with a previous drive
                sw["status"] = "idle"
                self.save()
            return False
        recs = self.st["functions"]
        proven = sum(1 for f in self.order if recs[f.id]["status"] == "proven")
        final = self.st.get("polish") or {}
        forced = bool(sw.get("forced"))  # watchdog arch-sweep: due whatever the count and the final polish say
        if (not forced and (proven - sw["at"] < self.arch_every or proven == len(self.order)
                            or final.get("status", "idle") not in ("idle", "done"))
                or self.polish_phase() is not None or self.polish_queue):
            # (no free-slot condition: with every slot busy the sweep never started; it is one audit job beside them)
            return False
        sw.pop("forced", None)
        sw.update(at=proven, status="audit")
        self.start_arch_audit(SWEEP_FID, None, self.ov / "polish" / f"arch-sweep-{proven}.json")
        self.save()
        return True

    def sweep_audited(self, blocking: list[dict], why: str) -> None:
        sw = self.sweep_state()
        if why or not blocking:
            self.sweep_checked({"findings": []}, None if why else [])
            return
        path = self.verify_brief(SWEEP_VERIFY, self.main, blocking, sw["at"])
        self.verify_meta[SWEEP_VERIFY] = {"findings": blocking}
        self.start_polish_job(SWEEP_VERIFY, "polish_verify", sw["at"], path, self.ov / "wt" / SWEEP_FID)
        sw["status"] = "verify"
        self.save()

    def sweep_checked(self, meta: dict, real: list[dict] | None) -> None:
        """The sweep's verified NOs go to one fix per module (the caller of a duplication, its owner first)."""
        sw, found = self.sweep_state(), len(meta.get("findings") or [])
        if real is None:
            self.log(None, "arch_sweep", f"at {sw['at']} lands: no usable audit or verify result")
            sw["status"] = "idle"
            self.save()
            return
        out = self.ov / "polish" / f"arch-sweep-{sw['at']}-verified.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(real, indent=1), encoding="utf-8")
        sw["open"] = len(real)
        sw["history"].append([sw["at"], found, len(real)])
        self.log(None, "arch_sweep", f"at {sw['at']} lands: {len(real)} of {found} blocking NOs verified")
        if real:
            self.polish_queue = sorted(self.route_polish(real).items())
        sw["status"] = "fix" if real else "idle"
        self.save()

    # ---- probe (v3): mutation score and property counterexamples, one fix pass per module ----
    def probe_modules(self) -> dict[str, str]:
        """{probe fid: module} for every module that owns a function, in build order."""
        return {PROBE_PREFIX + slug(f.module): f.module
                for f in self.order}

    def probe_state(self) -> dict:
        """st["probe"][module]: status (mutating | fixing | one of PROBE_DONE), score, survivors."""
        return self.st.setdefault("probe", {})

    def probe_waiting(self, upto: int | None = None) -> bool:
        """True while a module still owes its probe (polish waits for every probe). A module is probed once, when its
        functions are all proven; the phase upto polish waits only for the modules whose functions all sit in
        phases <= upto, i.e. the modules that phase finished."""
        pst, mods = self.probe_state(), set(self.probe_modules().values())
        if upto is not None:
            mods = {m for m in mods if all(phase_key(f)[0] <= upto for f in self.order if f.module == m)}
        return self.probe_on and any(pst.get(m, {}).get("status") not in PROBE_DONE for m in mods)

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
            if self.local_jobs() >= self.max_parallel:
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
        if self.red_fail(fid, reason):  # main fails the same check: not this function's attempt
            return
        if router.on_failure(self, fid, reason):  # a plan-class cause parks at once; a load flake is not charged
            return
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
        reason = router.on_park(self, fid, reason)  # classed and, for a plan class, routed to a plan fix
        if not reason:  # a files gap the router widened the claim for: the build resumes at once instead
            return
        self.set(fid, status="needs-plan", last_reason=reason[:REASON_MAX])
        self.log(fid, "needs_plan", reason)
        self.say(f"needs-plan {fid}: {reason}")

    def summary(self) -> str:
        n = Counter(self.st["functions"][fid]["status"] for fid in self.fns)
        rest = " ".join(f"{s} {n[s]}" for s in (*IN_FLIGHT, "needs-plan") if n[s])
        later = f" deferred {len(self.deferred)}" if self.deferred else ""
        return f"proven {n['proven']}/{len(self.fns)}{later}" + (f" {rest}" if rest else "")

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
        self.disk_guard("cycle")
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
        code = 3 if parked or blocked or self.red_stop_reason else 0
        self.cycle("exit")
        if self.dirty:
            self.flush()
        for fid in parked:
            self.say(f"needs-plan {fid}: {recs[fid]['last_reason']}")
        if blocked:
            self.say(f"blocked on unproven deps: {', '.join(blocked)}")
        if self.red_stop_reason:
            self.say(f"needs-plan: {self.red_stop_reason}")
        if self.deferred and not code:
            self.say(f"{len(self.deferred)} deferred rows wait for the next plan commit; "
                     "polish, probes and the verdict leave them out")
        print(f"DRIVE EXIT {code} {self.summary()}", flush=True)
        return code

    def abort(self, owner: bool = False) -> None:
        """Kill every worker tree this run started; their functions go back to planned, marked to resume at their
        stage when the next drive starts (resume_mark). A detached build (pid file) is left running unless owner:
        the next drive re-adopts it."""
        self.periodic.stop()
        self.feas.stop()  # feasibility checks in flight
        if self.remote:
            self.remote.abort()  # what remote slots run on their host
        jobs = [*self.jobs.values(), *self.land_jobs()]
        left = {} if owner else drivelock.read_jobs(self.ov)
        kept = [j for j in jobs if j.kind in DETACH_KINDS and j.fn in left and j.proc.poll() is None]
        jobs = [j for j in jobs if j not in kept]
        for job in jobs:
            kill_tree(job.proc)
            drivelock.drop_job(self.ov, job.fn)
        for fid in [*(j.fn for j in jobs), *self.land_queue]:
            rec = self.st["functions"].get(fid)
            if rec and rec["status"] in IN_FLIGHT:
                rec["resume"], rec["status"] = rec["status"], "planned"
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
        if kept:
            self.say(f"left {len(kept)} detached build(s) running for the next drive: {', '.join(j.fn for j in kept)}")

    # ---- manager commands ----
    def reset(self, fid: str) -> None:
        """A manager's plan fix sends fid back to planned. At most PLAN_FIXES per function, so a function that
        keeps failing stops the build with a report instead of looping through manager rounds."""
        if problem := self.reset_problem(fid):
            raise DriveError(problem)
        resets = int(self.st["functions"][fid].get("resets", 0))
        self.set(fid, status="planned", attempts=0, conflicts=0, last_reason="", resets=resets + 1)
        if self.dirty:
            self.flush()
        self.log(fid, "cycle", "reset to planned, attempts 0")
        self.say(f"reset {fid}")

    def reset_problem(self, fid: str) -> str:
        """Why fid cannot be reset now ('' if it can): not a row, or already PLAN_FIXES plan fixes."""
        if fid not in self.fns:
            return f"{fid} is not an active row of {self.cfg['paths']['functions']}"
        resets = int(self.st["functions"][fid].get("resets", 0))
        if resets >= PLAN_FIXES:
            return (f"{fid} already had {resets} plan fixes and failed again: stop and report it to the owner as "
                    f"the deciding item (last reason: {self.st['functions'][fid].get('last_reason', '')[:300]})")
        return ""

    # ---- live re-plan: reset requests written by supervise.py after a plan commit is on main ----
    def requests(self) -> list[Path]:
        d = self.ov / REQUESTS_DIR
        return sorted(p for p in d.glob("re*-*") if p.is_file() and p.name.startswith(("reset-", "reload-")))             if d.is_dir() else []

    def replan_step(self) -> bool:
        """At a cycle boundary (every REQUEST_POLL_S): reload the plan on main and apply the reset requests."""
        if time.monotonic() - self.last_request_check < REQUEST_POLL_S:
            return False
        self.last_request_check = time.monotonic()
        reqs = self.requests()
        if not reqs:
            return False
        head = git(self.repo, "rev-parse", self.main).strip()
        if head == self.refused_plan:
            return False  # this plan already failed validation: wait for the next plan commit
        problem = self.reload()
        if problem:
            self.refused_plan = head
            self.log(None, "reload_refused", f"{head[:12]}: {problem}")
            self.say(f"plan reload refused ({head[:12]}): {problem}")
            return False
        for path in reqs:
            fid = path.name[len("reset-"):]
            if path.name.startswith("reload-"):
                pass  # a split replaced every fixed row: the reload above was the request
            elif fid in self.fns and self.st["functions"][fid]["status"] == "proven":
                self.log(fid, "reset_refused", "already proven")
            elif why := self.reset_problem(fid):
                self.log(fid, "reset_refused", why)
                self.say(f"reset {fid} refused: {why}")
            else:
                n = int(self.st["functions"][fid].get("resets", 0)) + 1
                self.set(fid, status="planned", attempts=0, conflicts=0, last_reason="", resets=n)
                self.log(fid, "reset_applied", f"plan {head[:12]}; reset {n} of {PLAN_FIXES}")
                self.say(f"reset {fid} (plan {head[:12]})")
            path.unlink(missing_ok=True)
        self.cycle("replan")
        return True

    def harness_requests(self) -> bool:
        """watchdog.py requests (HARNESS_REQUESTS), each applied once (event request_applied or request_refused)
        and removed; True if any was applied."""
        if time.monotonic() - self.last_harness_request < REQUEST_POLL_S:
            return False
        self.last_harness_request = time.monotonic()
        d = self.ov / REQUESTS_DIR
        names = sorted(p.name for p in d.iterdir() if p.is_file()) if d.is_dir() else []
        reqs = [d / n for n in names if n.startswith(HARNESS_REQUESTS)]
        applied = False
        for path in reqs:
            why = self.harness_request(path.name)
            self.log(None, "request_refused" if why else "request_applied", f"{path.name}" + (f": {why}" if why else ""))
            if why:
                self.say(f"request {path.name} refused: {why}")
            applied = applied or not why
            path.unlink(missing_ok=True)
        if applied:
            self.save()
        return applied

    def harness_request(self, name: str) -> str:
        """Apply one watchdog request; '' when applied, else why not."""
        if name.startswith("audit-phase-"):
            m = re.fullmatch(r"audit-phase-(\d+)", name)
            phases = {phase_key(f)[0] for f in self.order}
            if not m or int(m.group(1)) not in phases:
                return "no such phase in the plan"
            if not self.phase_audit_on():
                return "[harness] phase_audit is off"
            n, force = int(m.group(1)), self.st.setdefault("phase_force", [])
            pol = self.st.setdefault("phase_polish", {}).get(str(n))
            if n in force and (pol or {}).get("status") not in ("done", "failed"):
                return f"phase {n} audit already queued or running"
            if pol and pol.get("status") in ("done", "failed"):  # audited before: once more over today's code
                self.st["phase_polish"][str(n)] = {"round": 0, "status": "idle", "issues": None, "history": [],
                                                   "audits": int(pol.get("audits", 1)) + 1, "gaps": pol.get("gaps")}
            force[:] = [x for x in force if x != n] + [n]
            return ""
        if name == "arch-sweep":
            if not self.phase_audit or self.arch_every < 1:
                return "[harness] phase_audit or arch_audit_every_lands is off"
            sw = self.sweep_state()
            if sw["status"] != "idle":
                return f"a sweep is already {sw['status']}"
            sw["forced"] = True
            return ""
        gate = name[len("periodic-"):]
        if not self.periodic.cfg or gate not in self.periodic.cfg.get("commands", {}):
            return f"no periodic gate {gate}"
        return self.periodic.rearm(gate)

    def reload(self) -> str:
        """Re-read the plan on main without a restart: new rows join as planned, changed rows replace their Fn,
        rows now deferred or gone leave unless in flight. '' when done, else why it was refused (nothing changed)."""
        rel, arch = self.cfg["paths"]["functions"], self.cfg["paths"]["architecture"]
        fns = parse_functions(self.read_main(rel) or "")
        ids = [f.id for f in fns]
        if not fns:
            return f"{rel} on {self.main} has no function rows"
        if len(set(ids)) != len(ids):
            return f"{rel} repeats ids: {sorted({i for i in ids if ids.count(i) > 1})}"
        if problem := self.validate_static():
            return problem
        busy = self.busy()
        active = {f.id: f for f in fns if f.status != DEFERRED}
        keep = {fid: f for fid, f in self.fns.items() if fid not in active and fid in busy}  # in flight: finish
        self.fns = {**active, **keep}
        self.order = sorted(self.fns.values(), key=phase_key)
        self.deferred = {f.id for f in fns if f.status == DEFERRED} - set(keep)
        self.arch = self.read_main(arch) or ""
        self.on_main = set(git(self.repo, "ls-tree", "-r", "--name-only", self.main).splitlines())
        prot = {"tests/acceptance/", "overhaul.toml", rel, arch}
        prot |= {f"{Path(p).parent.as_posix()}/" for p in [rel, arch] + [acc_file(a) for f in self.order
                                                                         for a in f.acceptance]}
        self.protected = tuple(sorted(p for p in prot if p != "./"))
        recs, new = self.st["functions"], []
        for fid in [k for k in recs if k not in self.fns]:
            del recs[fid]
        for f in self.order:
            if f.id not in recs:
                new.append(f.id)
                recs[f.id] = {"status": "proven" if f.status == "proven" else "planned", "attempts": 0,
                              "conflicts": 0, "last_reason": "", "branch": f"fn/{f.id}", "updated": now_iso()}
        self.flush()
        for fid in new:
            if recs[fid]["status"] == "planned" and (problem := self.plan_problem(self.fns[fid])):
                self.park(fid, problem)
        self.log(None, "plan_reload", f"{len(self.fns)} rows, deferred {len(self.deferred)}"
                 + (f", new {' '.join(new)}" if new else ""))
        self.reload_slots()
        return ""

    def reload_slots(self) -> None:
        """Re-read [harness] max_parallel, land_parallel and [harness.remote] slots from overhaul.toml, so the manager moves build
        load between machines without a restart (pilot2026-11-10: 24 cold laptop slots at 94% CPU, the desktop idle).
        Running jobs finish where they are; new starts follow the new numbers."""
        try:
            har = load_config(self.repo)["harness"]
        except Exception as e:  # noqa: BLE001 - a bad edit must not stop the run; the old numbers stay
            self.log(None, "slots_reload_refused", str(e)[:300])
            return
        local = max(1, int(har.get("max_parallel") or self.max_parallel))
        rem = int((har.get("remote") or {}).get("slots", 0) or 0)
        lands = max(1, int(har.get("land_parallel") or har.get("max_lands") or self.max_lands))
        before = (self.max_parallel, self.remote.slots if self.remote else 0, self.max_lands)
        self.max_parallel, self.max_lands = local, lands
        if self.remote and rem > 0:
            self.remote.slots = rem
        after = (self.max_parallel, self.remote.slots if self.remote else 0, self.max_lands)
        if after != before:
            self.log(None, "slots_reload", f"local {before[0]}->{after[0]} remote {before[1]}->{after[1]} "
                                           f"lands {before[2]}->{after[2]}")

    def validate_static(self) -> str:
        """plan_validate (static, no --run) on the checkout holding main: '' if PLAN OK, else its line."""
        pv = self.scripts / "plan_validate.py"
        pv = pv if pv.is_file() else HERE / "plan_validate.py"
        where = land_mod.checked_out_at(str(self.repo), self.main) or str(self.repo)
        try:
            # --no-lint: the whole-tree clippy timed out at 300 s under build load and refused phases 11-12 (the pilot project
            # 2026-11-10 08:16, 08:33); lint gates every land and every plan land already
            r = run([sys.executable, str(pv), "--repo", where, "--no-lint"], self.repo, timeout=300)
        except subprocess.TimeoutExpired:
            return "plan_validate timed out after 300s"
        line = next((ln.strip() for ln in r.stdout.splitlines() if ln.startswith("PLAN ")), "")
        if r.returncode or not line.startswith("PLAN OK"):
            return f"plan_validate: {line or last_line(r.stdout + r.stderr) or f'exit {r.returncode}'}"
        return ""

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
        before = json.loads(json.dumps(self.st))
        self.st["polish"] = new
        for ph in (self.st.get("polish_phases") or {}).values():  # per-phase polish: audit each phase again
            ph.update(round=0, status="idle", issues=None, best=None, audit_retry=None)
        if not self.save():  # a run (or its land) holds the lock: never wait, never race its state write
            self.st = before
            raise DriveError(f"{lock_path(self.repo).as_posix()} is held by {lock_holder(self.repo) or '?'}: "
                             "stop the run (or let its land finish), then --repolish")
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


def install_guard(repo: Path) -> None:
    """Install guard/reference-transaction into the repo's git hooks (shared by every worktree): outside the harness,
    main may only move forward with plan data and configuration (spec/, tests/acceptance/, overhaul.toml, ...)."""
    src = HERE / "guard" / "reference-transaction"
    common = Path(git(repo, "rev-parse", "--git-common-dir").strip())
    hooks = (common if common.is_absolute() else repo / common) / "hooks"
    dst = hooks / "reference-transaction"
    body = src.read_bytes()
    if not dst.is_file() or dst.read_bytes() != body:
        hooks.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(body)
        dst.chmod(0o755)


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
    ap.add_argument("--stop", action="store_true",
                    help="the owner's stop: kill the running supervise/drive and every builder it started, then exit")
    ap.add_argument("--keep-builders", action="store_true",
                    help="with --stop: leave detached builders running for the next drive to re-adopt (a restart)")
    args = ap.parse_args(argv)
    repo = Path(os.path.abspath(args.repo))
    if args.stop:
        print(f"drive: {stop(repo, args.keep_builders)}", flush=True)
        return 0
    admin = next((f for f, on in (("--reset", args.reset), ("--mark-proven", args.mark_proven),
                                  ("--repolish", args.repolish)) if on), "")
    held = ""
    try:
        held = take_drive_lock(repo, admin)
        drv = Driver(repo, load_config(repo))
        install_guard(repo)
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
        # every child inherits the cap, and dies with drive (one job, both limits)
        cap = memcap.cap_self(float(drv.cfg["limits"].get("proc_mem_gb") or 0), kill_on_close=True)
        if cap:
            print(f"drive: {cap}", flush=True)
        drv.single_start()
        return drv.run()
    except DriveError as e:
        print(f"drive: error: {e}", flush=True)
    except KeyboardInterrupt:
        print("drive: interrupted", flush=True)
    except Exception:  # an unexpected bug: show where
        traceback.print_exc()
    finally:
        if held == "taken":
            drivelock.release(repo)
    return 1


def stop(repo: Path, keep_builders: bool = False) -> str:
    """Kill the lock holder (supervise.py or drive.py) and, unless keep_builders, every detached builder named in
    .overhaul/jobs (they outlive a crashed or restarted drive on purpose). keep_builders kills the holder by pid only:
    its kill-on-close job takes its attached children; detached builders run on and the next drive re-adopts them."""
    ov = repo / ".overhaul"
    rec = drivelock.holder(repo)
    left = drivelock.read_jobs(ov)
    live = {fid: job for fid, job in left.items() if drivelock.job_live(job)}  # before the holder's tree goes
    said = []
    if rec and drivelock.live(int(rec.get("pid") or 0), str(rec.get("start") or "")):
        drivelock.kill_pid(int(rec["pid"]), tree=not keep_builders)
        said.append(f"killed {rec.get('owner') or 'drive'} pid {rec['pid']}")
    else:
        said.append("no drive running")
    if keep_builders:
        said.append(f"{len(live)} detached builder(s) left running for the next drive: {', '.join(live) or '-'}")
    else:
        for fid, job in left.items():
            if fid in live and drivelock.job_live(job):
                drivelock.kill_pid(int(job["pid"]))
            drivelock.drop_job(ov, fid)
        said.append(f"killed {len(live)} detached builder(s)" + (f": {', '.join(live)}" if live else ""))
    return "; ".join(said)


def take_drive_lock(repo: Path, admin: str = "") -> str:
    """One drive per repo (.overhaul/drive.lock): 'taken', or 'inherited' under the supervise.py that started this
    drive. A second start, or --reset/--mark-proven/--repolish while another drive runs, is a DriveError naming the
    running pid."""
    try:
        return drivelock.acquire(repo, f"drive.py {admin}".strip())
    except drivelock.Held as e:
        if admin:
            raise DriveError(drivelock.refuse_admin(repo, admin, hasattr(Driver, "replan_step"))) from e
        raise DriveError(f"{e}; this start exits, the running drive keeps going") from e


if __name__ == "__main__":
    sys.exit(main())
