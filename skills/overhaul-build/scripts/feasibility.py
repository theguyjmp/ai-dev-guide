"""feasibility.py: is a FUNCTIONS.md row buildable, checked before a builder spends 10-30 min on it.

One cheap agent call per row ([harness] feasibility_model, default sonnet; lean read-only worker flags from worker.py;
prompt caching on, the fixed instructions first so every call shares one cached prefix) gets the row, its acceptance
test, its module's ARCHITECTURE section, the rows it may call and a grep map of main (the definitions in its module
and the modules it depends on). It answers JSON:

  files  every file the change must touch
  needs  each capability the row calls -> the proven row or dep row that provides it, `main`, or MISSING
  fits   {"ok": bool, "why": str}: one 30-min builder finishes it

The answer is cached in .overhaul/feasibility/<id>.json, valid while the row text and the blobs on main of every file
it names (files cell, acceptance files, answered files) are unchanged. verdict() turns it into problems and widened
files: a MISSING capability, a provider row that is neither proven nor a dep, a file outside the row's module and the
modules it depends on, or fits=no is a problem; a file outside the files cell but inside its module or a dep module
widens the claim.

drive.py (Gate): before a row's FIRST build (attempts 0, not a resume). A problem parks the row needs-plan with router
class `infeasible` and the exact evidence, before any build; widened files go to state `files_extra` (event
`files_widened`), which claims() and the allowed paths include. The check runs in a daemon thread and the row counts
as busy meanwhile, so the drive loop never waits on it. A failed check (agent error, unparsable answer) logs
`feasibility_error` and the build starts as before. `[harness] feasibility = false` turns it off; a project with a
custom worker_cmd and no feasibility_cmd has no agent to ask and skips it.

The answer also holds a WORK PACKET: per file the exact symbol or line to edit after, the signatures to implement,
the existing helpers to call (path::name, signature copied from the code) and the exact test command. drive.py puts it
under the build brief's title ("start here; do not explore beyond these files unless a test fails") and the first
attempt of a row with a packet runs with --max-turns [limits] packet_max_calls (default 20); a retry keeps the packet
and gets the full builder_max_calls. Why (owner 2026-11-10): builders explored, median 24 turns, 18 min and 940k
tokens per build.

plan_validate.py --feasibility (planners, before a plan commit): the same check for every planned row; a problem or a
file outside the files cell is a PLAN FAIL line.

Why (pilot 2026-11-09 overnight, 248 builds): 69 went back to planning after the build, 16 for a file outside the
files cell, 13 BLOCKED on a capability no row provides, 8 real timeouts on rows too big for one builder.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import router
import worker

MODEL = "sonnet"
MAX_TURNS = 8
TIMEOUT_S = 300.0
PARALLEL = 4
FRESH_S = 60.0  # prefetch looks a row's valid cached answer up again after this long
PACKET_CALLS = 20  # default [limits] packet_max_calls: a first build that starts from a work packet
PREFETCH = 8  # rows checked ahead of a free slot, so a launch rarely waits on its check
MAP_LINES = 300
SECTION_MAX = 6000
ACCEPT_MAX = 12000
PLAN_ROWS_MAX = 120
DIR = "feasibility"
MISSING = "MISSING"
CACHE_ENV = ("DISABLE_PROMPT_CACHING", "DISABLE_PROMPT_CACHING_SONNET", "DISABLE_PROMPT_CACHING_OPUS",
             "DISABLE_PROMPT_CACHING_HAIKU")
SYMBOL = (r"^[[:space:]]*(pub(\([^)]*\))?[[:space:]]+)?(async[[:space:]]+)?(def|fn|class|struct|enum|trait|impl|mod|"
          r"type|export[[:space:]]+(default[[:space:]]+)?(async[[:space:]]+)?(function|class|const|interface|type))"
          r"([^A-Za-z0-9_]|$)")  # POSIX ERE: git grep -E has no (?: or \s

INSTRUCTIONS = """You check ONE planned function row of a rebuild before a builder spends 30 minutes on it. Edit nothing.
The sections below hold the row, its acceptance test, its module's ARCHITECTURE section, the rows it may call and a
map of the definitions on main. Read code yourself only where the map is not enough (a few Grep or Read calls at most).

Answer with ONE JSON object and nothing else:
{"files": ["every repo-relative product file the change must create or edit"],
 "needs": {"<capability the row's code must call>": "<row id that provides it | main | MISSING>"},
 "fits": {"ok": true, "why": "<one line>"},
 "packet": {"edits": [{"file": "<path>", "after": "<exact symbol or line to edit after, or NEW FILE>",
                       "what": "<what to add or change there, one line>"}],
            "implement": ["<exact signature of each function, method or type to write>"],
            "helpers": [{"ref": "<path>::<name>", "signature": "<its signature copied from the code>"}],
            "test": "<the exact command that runs the acceptance test>"}}

- files: every file the builder must create or edit, including ones outside the row's files cell (registering a
  verb, extending a shim or a shared store). Not files it only reads, not tests/acceptance or spec files.
- needs: each capability the row calls that it does not build itself. The provider is the proven row or the dep row
  that builds it, `main` when the code on main already does it, or MISSING when nothing on main and no listed row
  provides it (for example the store cannot add records yet and no row adds that).
- fits: ok is false when one builder cannot finish in 30 minutes and ~60 tool calls: several user-visible verbs at
  once, a fixture of ~1M rows, or more than ~6 files of new logic. `why` names the reason in one line.
- packet: the builder's work packet, so it starts editing instead of exploring. One edit per place in each file
  (the symbol or line to edit after, as it reads on main), the signatures to implement, every existing helper it
  should call (path::name with the signature copied from the code, never guessed) and the exact test command from
  the commands section. Only facts you checked in the map or the code.
"""


@dataclass
class Row:
    """What one check needs, built by drive.py (Gate.row) or plan_validate.py."""
    id: str
    module: str
    deps: list[str]
    table: str  # the row with its header
    acceptance: str  # the acceptance test source
    section: str  # the module's ARCHITECTURE section
    cell: list[str]  # the row's claim: files cell (or module paths) plus its module's shared files
    own: list[str]  # its module's paths
    dep_paths: list[str]  # the paths of the modules its module depends on
    protected: tuple[str, ...] = ()  # tests/acceptance, spec: never product files
    unit: list[str] = field(default_factory=list)  # unit test dirs, always allowed
    names: list[str] = field(default_factory=list)  # files whose blobs key the cache: cell + acceptance files
    plan: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # id -> (module, status, entry)
    commands: str = ""  # the exact gate commands (drive.commands_text): the packet's test command comes from here

    @property
    def key(self) -> str:
        return row_key(self.table, self.section)


def row_key(table: str, section: str) -> str:
    return hashlib.sha256((table + "\n" + section).encode("utf-8")).hexdigest()[:16]


# ---- the agent call ------------------------------------------------------------------------------------------------
def agent_cmd(har: dict) -> list[str] | None:
    """The check's command without the prompt (read on stdin), or None when there is no agent to ask."""
    if not har.get("feasibility", True):
        return None
    custom = har.get("feasibility_cmd")
    if custom:
        return [str(c) for c in custom] if isinstance(custom, list) else shlex.split(str(custom))
    if har.get("worker_cmd"):  # the project replaced claude (a fake or another agent): nothing to ask
        return None
    exe = shutil.which("claude")
    if not exe:
        return None
    cmd = [exe, "-p", "--output-format", "json", *worker.lean_flags(exe, "review")]
    if "--max-turns" in worker.help_text(exe):
        cmd += ["--max-turns", str(int(har.get("feasibility_max_turns") or MAX_TURNS))]
    return cmd + ["--model", str(har.get("feasibility_model") or MODEL)]


def agent_env(cmd: list[str]) -> dict[str, str]:
    """Lean env for a lean claude command, prompt caching never disabled."""
    env = dict(worker.env(cmd, None) or os.environ)
    for k in CACHE_ENV:
        env.pop(k, None)
    return env


def run_agent(cmd: list[str], prompt: str, cwd: Path, timeout: float,
              procs: list | None = None) -> tuple[dict | None, dict, str]:
    """(answer, usage, error). The prompt goes on stdin: no command-line length limit, no Read turn."""
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=agent_env(cmd), text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        return None, {}, f"cannot start {cmd[0]}: {e}"
    if procs is not None:
        procs.append(p)
    try:
        out, err = p.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        return None, {}, f"no answer in {timeout:g} s"
    finally:
        if procs is not None and p in procs:
            procs.remove(p)
    ans, usage = parse(out)
    if ans is None:
        tail = " ".join((out or err or "").split())[-300:]
        return None, usage, f"rc={p.returncode}, no JSON answer: {tail}"
    return ans, usage, ""


def json_objects(text: str) -> list[dict]:
    dec, out, i = json.JSONDecoder(), [], 0
    while (i := text.find("{", i)) >= 0:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


def parse(out: str) -> tuple[dict | None, dict]:
    """The normalized answer and the usage from `claude -p --output-format json` stdout, or from a bare answer."""
    text, usage = out or "", {}
    for doc in json_objects(text):
        if isinstance(doc.get("result"), str):  # claude's result object: the answer is in its text
            text, usage = doc["result"], doc.get("usage") or {}
            break
    for obj in reversed(json_objects(text)):
        if "files" in obj and "fits" in obj:
            return normalize(obj), usage
    return None, usage


def clean_path(p: object) -> str:
    return str(p).strip().strip("`").removeprefix("./").replace("\\", "/")


def normalize(obj: dict) -> dict:
    files = obj.get("files") or []
    files = [clean_path(p) for p in (files if isinstance(files, list) else [files]) if clean_path(p)]
    raw = obj.get("needs") or {}
    if isinstance(raw, list):  # [{"capability": .., "provider": ..}]
        raw = {str(n.get("capability") or n.get("name") or i): n.get("provider") or n.get("row") or ""
               for i, n in enumerate(raw) if isinstance(n, dict)}
    needs = {str(k): str(v).strip() for k, v in (raw.items() if isinstance(raw, dict) else [])}
    fits = obj.get("fits")
    if isinstance(fits, dict):
        val = next((fits[k] for k in ("ok", "fits", "answer", "yes") if k in fits), True)
        why = str(fits.get("why") or fits.get("reason") or "")
    else:
        val, why = fits, str(obj.get("why") or "")
    ok = val if isinstance(val, bool) else str(val).strip().lower() not in ("no", "false", "0", "")
    return {"files": list(dict.fromkeys(files)), "needs": needs, "fits": {"ok": ok, "why": why},
            "packet": norm_packet(obj.get("packet"))}


def norm_packet(raw: object) -> dict:
    """The work packet, every part a list of plain strings or dicts; {} when it holds no edit."""
    if not isinstance(raw, dict):
        return {}
    edits = [{"file": clean_path(e.get("file") or ""), "after": str(e.get("after") or "").strip(),
              "what": str(e.get("what") or "").strip()}
             for e in (raw.get("edits") or []) if isinstance(e, dict) and e.get("file")]
    impl = [str(s).strip() for s in (raw.get("implement") or []) if str(s).strip()]
    helpers = [{"ref": str(h.get("ref") or "").strip(), "signature": str(h.get("signature") or "").strip()}
               for h in (raw.get("helpers") or []) if isinstance(h, dict) and h.get("ref")]
    test = str(raw.get("test") or "").strip()
    return {"edits": edits, "implement": impl, "helpers": helpers, "test": test} if edits else {}


def packet_text(doc: dict, budget: int) -> str:
    """The `## Work packet` section a build brief starts with, or '' when the answer has no packet."""
    pk = (doc.get("answer") or {}).get("packet") or {}
    if not pk:
        return ""
    lines = ["## Work packet: start here",
             (f"Checked against main by the feasibility check ({cache_path_rel(doc)}). Start here; do not explore "
              "beyond these files unless a test fails. Read each file only around the place named. "
              f"Tool-call budget: {budget}."), "", "Edits:"]
    lines += [f"- `{e['file']}` after `{e['after'] or 'the end'}`: {e['what']}" for e in pk["edits"]]
    if pk["implement"]:
        lines += ["", "Implement:"] + [f"- `{s}`" for s in pk["implement"]]
    if pk["helpers"]:
        lines += ["", "Existing helpers to call (do not rewrite them):"]
        lines += [f"- `{h['ref']}`" + (f": `{h['signature']}`" if h["signature"] else "") for h in pk["helpers"]]
    if pk["test"]:
        lines += ["", f"Test: `{pk['test']}`"]
    return "\n".join(lines) + "\n"


def cache_path_rel(doc: dict) -> str:
    return f".overhaul/{DIR}/{doc.get('id', '?')}.json"


def embed(brief: str, packet: str) -> str:
    """The packet right under the brief's title line."""
    if not packet:
        return brief
    title, sep, rest = brief.partition("\n")
    return f"{title}\n\n{packet}{sep}{rest}" if sep else f"{packet}\n{brief}"


# ---- the prompt ------------------------------------------------------------------------------------------------------
def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       stdin=subprocess.DEVNULL, check=False)
    return r.stdout if r.returncode in (0, 1) else ""


def grep_map(repo: Path, ref: str, paths: list[str], limit: int = MAP_LINES) -> str:
    """`path:line: definition` for every definition on ref under paths (all of ref when paths is empty)."""
    out = git(repo, "grep", "-n", "-I", "-E", SYMBOL, ref, "--", *(paths or ["."]))
    lines = [ln.removeprefix(f"{ref}:").strip()[:160] for ln in out.splitlines() if ln.strip()]
    more = len(lines) - limit
    return "\n".join(lines[:limit]) + (f"\n... {more} more (grep yourself)" if more > 0 else "") or "(none)"


def plan_rows(row: Row) -> str:
    """The rows it may call: proven ones, its deps and its own module's, one line each."""
    out = []
    for rid, (module, status, entry) in row.plan.items():
        if rid != row.id and (status == "proven" or rid in row.deps or module == row.module):
            dep = " (dep)" if rid in row.deps else ""
            out.append(f"- {rid} {status}{dep}: module {module}, entry {entry}")
    more = len(out) - PLAN_ROWS_MAX
    return "\n".join(out[:PLAN_ROWS_MAX]) + (f"\n... {more} more" if more > 0 else "") or "(none)"


def prompt(row: Row, repo: Path, ref: str) -> str:
    """The fixed instructions first (one cached prefix across calls), then this row."""
    cell = ", ".join(f"`{p}`" for p in row.cell) or "(none)"
    return "\n\n".join([
        INSTRUCTIONS,
        (f"## Row {row.id} (module {row.module}; deps {', '.join(row.deps) or 'none'})\n{row.table}\n\n"
         f"Its files cell (the claim): {cell}"),
        f"## Acceptance test\n{row.acceptance[:ACCEPT_MAX]}",
        f"## ARCHITECTURE section of module {row.module}\n{row.section[:SECTION_MAX] or '(none)'}",
        f"## Rows it may call (proven, its deps, its module)\n{plan_rows(row)}",
        f"## Commands (the packet's test command is one of these)\n{row.commands or '(see overhaul.toml)'}",
        f"## Map of {ref} (definitions in its module and the modules it depends on)\n"
        + grep_map(repo, ref, list(dict.fromkeys(row.own + row.dep_paths))),
    ])


# ---- the cache -------------------------------------------------------------------------------------------------------
def literal(p: str) -> str:
    return re.split(r"[*?\[]", p, maxsplit=1)[0] or "."


def blobs(repo: Path, ref: str, paths: list[str]) -> dict[str, str]:
    """{path: blob sha on ref} for every file under paths; a named file absent on ref maps to ''."""
    paths = list(dict.fromkeys(literal(p) for p in paths if p))
    if not paths:
        return {}
    found = {}
    for ln in git(repo, "ls-tree", "-r", ref, "--", *paths).splitlines():
        meta, _, name = ln.partition("\t")
        if name:
            found[name] = meta.split()[-1]
    return {**{p: "" for p in paths if not p.endswith("/") and p != "."}, **found}


def cache_path(ov: Path, fid: str) -> Path:
    return ov / DIR / f"{fid}.json"


def cached(ov: Path, row: Row, repo: Path, ref: str) -> dict | None:
    """The cached answer while the row text and the blobs of every file it named are unchanged, else None."""
    try:
        doc = json.loads(cache_path(ov, row.id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("row") != row.key or not isinstance(doc.get("answer"), dict):
        return None
    named = doc.get("blobs") or {}
    return doc if blobs(repo, ref, list(named)) == named else None


def check(row: Row, cmd: list[str], repo: Path, ov: Path, ref: str, timeout: float = TIMEOUT_S,
          procs: list | None = None) -> dict:
    """Ask the agent about one row and cache the answer; the cache doc, or {"error": why} (never cached)."""
    started = time.monotonic()
    ans, usage, err = run_agent(cmd, prompt(row, repo, ref), repo, timeout, procs)
    if ans is None:
        return {"error": err, "usage": usage}
    doc = {"id": row.id, "row": row.key, "t": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
           "seconds": round(time.monotonic() - started, 1), "blobs": blobs(repo, ref, row.names + ans["files"]),
           "answer": ans, "usage": {k: int(usage.get(k) or 0) for k in (
               "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")}}
    path = cache_path(ov, row.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return doc


# ---- the verdict -----------------------------------------------------------------------------------------------------
def verdict(ans: dict, row: Row, overlap: Callable[[list[str], list[str]], bool]) -> tuple[list[str], list[str]]:
    """(problems, widen): what makes the row unbuildable as planned, and the files its claim must grow by."""
    problems, widen = [], []
    for cap, prov in ans.get("needs", {}).items():
        rid = prov.strip().strip("`")
        if rid.upper().startswith(MISSING):
            problems.append(f"needs `{cap}`: MISSING, no proven or dep row provides it")
        elif rid in row.plan and rid != row.id and row.plan[rid][1] != "proven" and rid not in row.deps:
            problems.append(f"needs `{cap}` from {rid}, which is neither proven nor a dep of {row.id}")
    fits = ans.get("fits") or {}
    if not fits.get("ok", True):
        problems.append(f"does not fit one 30-min builder: {fits.get('why') or 'no reason given'}")
    for p in ans.get("files", []):
        if p.startswith(row.protected) or overlap([p], row.unit) or overlap([p], row.cell):
            continue
        if overlap([p], row.own + row.dep_paths):
            widen.append(p)
        else:
            problems.append(f"must change `{p}`, outside module {row.module} and the modules it depends on")
    return problems, widen


def summary(doc: dict) -> str:
    u = doc.get("usage") or {}
    return (f"{doc.get('seconds', 0)} s, files {len(doc['answer']['files'])}, needs {len(doc['answer']['needs'])}, "
            f"packet edits {len((doc['answer'].get('packet') or {}).get('edits') or [])}, "
            f"cache_read {u.get('cache_read_input_tokens', 0)}")


# ---- drive.py --------------------------------------------------------------------------------------------------------
class Gate:
    """drive.py's hook. busy() joins Driver.busy(); collect() and prefetch() run in launch(); ok() in start_build()."""

    def __init__(self, d):
        har = d.cfg["harness"]
        self.d = d
        self.cmd = agent_cmd(har)
        self.timeout = float(har.get("feasibility_timeout_s") or TIMEOUT_S)
        self.slots = threading.BoundedSemaphore(max(1, int(har.get("feasibility_parallel") or PARALLEL)))
        self.running: dict[str, threading.Thread] = {}
        self.results: dict[str, dict] = {}
        self.procs: list[subprocess.Popen] = []
        self.lock = threading.Lock()
        self.fresh: dict[str, tuple[str, float]] = {}  # fid -> (row key, when its cache was last found valid)
        self.docs: dict[str, dict] = {}  # fid -> the answer ok() just accepted, for packet()
        self.packet_calls = max(1, int(d.cfg["limits"].get("packet_max_calls") or PACKET_CALLS))
        # drive.py's module (also when it runs as __main__): its path helpers, without importing it twice
        self.dm = sys.modules[type(d).__module__]

    def busy(self) -> set[str]:
        """Rows whose check runs or has not been collected yet: drive must not finish under them."""
        with self.lock:
            return set(self.running)

    def wanted(self, f) -> bool:
        rec = self.d.st["functions"].get(f.id) or {}
        return bool(self.cmd) and not rec.get("attempts") and not rec.get("resume") and rec.get(
            "status") == "planned" and rec.get("feasible") != self.key(f)

    def key(self, f) -> str:
        return row_key(self.d.row_table(f), self.dm.module_section(self.d.arch, f.module))

    def row(self, f) -> Row:
        d, dm = self.d, self.dm
        section = dm.module_section(d.arch, f.module)
        paths, unit = d.allowed_lists(f)
        accept = list(dict.fromkeys(dm.acc_file(a) for a in f.acceptance))
        plan = {g.id: (g.module, (d.st["functions"].get(g.id) or {}).get("status", g.status), g.entry)
                for g in d.order}
        return Row(f.id, f.module, list(f.deps), d.row_table(f), d.acceptance_text(f), section, paths,
                   dm.module_paths(section, f.module), d.dep_paths([f.module]), tuple(d.protected), unit,
                   list(dict.fromkeys(paths + accept)), plan, d.commands_text(f))

    def packet(self, f, attempt: int) -> tuple[str, int]:
        """(the work packet section for f's build brief, its tool-call budget). The first attempt of a row with a
        packet gets [limits] packet_max_calls; a retry keeps the packet but gets the full builder_max_calls, since the
        packet was not enough. No packet: ('', builder_max_calls)."""
        full = int(self.d.max_calls)
        if not self.cmd:
            return "", full
        doc = self.docs.pop(f.id, None) or cached(self.d.ov, self.row(f), self.d.repo, self.d.main)
        if not doc or not (doc.get("answer") or {}).get("packet"):
            return "", full
        budget = min(full, self.packet_calls) if attempt <= 1 else full
        return packet_text(doc, budget), budget

    def collect(self) -> None:
        with self.lock:
            for fid in [k for k, t in self.running.items() if not t.is_alive()]:
                self.running.pop(fid)

    def submit(self, f) -> None:
        row, d = self.row(f), self.d

        def go() -> None:
            with self.slots:
                try:
                    res = check(row, self.cmd, d.repo, d.ov, d.main, self.timeout, self.procs)
                except Exception as e:  # noqa: BLE001 - a broken check never stops the drive
                    res = {"error": f"{type(e).__name__}: {e}"}
            with self.lock:
                self.results[row.id] = res
        t = threading.Thread(target=go, name=f"feasibility-{f.id}", daemon=True)
        with self.lock:
            self.running[f.id] = t
        t.start()

    def prefetch(self, rows: list) -> None:
        """Start the checks of the next ready rows that have no valid cached answer."""
        n = 0
        for f in rows:
            if n >= PREFETCH:
                break
            if f.id in self.running or f.id in self.results or not self.wanted(f):
                continue
            seen = self.fresh.get(f.id)
            if seen and seen[0] == self.key(f) and time.monotonic() - seen[1] < FRESH_S:
                continue
            if cached(self.d.ov, self.row(f), self.d.repo, self.d.main) is None:
                self.submit(f)
                n += 1
            else:
                self.fresh[f.id] = (self.key(f), time.monotonic())

    def ok(self, f) -> bool:
        """True: build f now. False: its check is running, it was parked infeasible, or its widened claim now
        overlaps a row in flight (it starts on a later pass)."""
        if not self.wanted(f):
            return True
        d, row = self.d, self.row(f)
        if f.id in self.running:
            return False
        with self.lock:
            res = self.results.pop(f.id, None)
        doc = cached(d.ov, row, d.repo, d.main)
        if doc is None and res is None:
            self.submit(f)
            return False
        if doc is None:  # the check failed: build as before
            d.log(f.id, "feasibility_error", str(res.get("error"))[:600])
            d.set(f.id, feasible=row.key)
            return True
        problems, widen = verdict(doc["answer"], row, self.dm.paths_overlap)
        if problems:
            evidence = "; ".join(problems) + f" (feasibility check {cache_path(d.ov, f.id).as_posix()})"
            router.record(d, f.id, "infeasible", router.want_plan_fix(d, f.id, "infeasible", evidence), evidence)
            d.park(f.id, f"router infeasible: {router.WORDS['infeasible']}; {router.HINT['infeasible']}: {evidence}")
            return False
        d.log(f.id, "feasibility_ok", summary(doc))
        self.docs[f.id] = doc  # packet() reads it right after, without a second git lookup
        d.set(f.id, feasible=row.key)
        if widen:
            router.add_files(d, f.id, widen)
            busy = d.busy() - {f.id}
            if any(self.dm.paths_overlap(d.claims(f), d.claims(d.fns[o])) for o in busy if o in d.fns):
                return False
        return True

    def stop(self) -> None:
        for p in list(self.procs):
            try:
                p.kill()
            except OSError:
                pass


# ---- plan_validate.py ------------------------------------------------------------------------------------------------
def plan_problems(repo: Path, rows: list[Row], har: dict, ref: str = "HEAD",
                  overlap: Callable[[list[str], list[str]], bool] | None = None) -> list[str]:
    """One line per planned row the check finds unbuildable or whose files cell misses a file (planners run this
    before a plan commit). No agent to ask: one line saying so, so a planner never thinks it passed."""
    cmd = agent_cmd({**har, "feasibility": True})
    if not cmd:
        return ["feasibility: no agent to ask (no claude on PATH and no [harness] feasibility_cmd)"]
    ov, out, lock = repo / ".overhaul", [], threading.Lock()
    slots = threading.BoundedSemaphore(max(1, int(har.get("feasibility_parallel") or PARALLEL)))
    timeout = float(har.get("feasibility_timeout_s") or TIMEOUT_S)

    def one(row: Row) -> None:
        with slots:
            doc = cached(ov, row, repo, ref) or check(row, cmd, repo, ov, ref, timeout)
        if "error" in doc:
            lines = [f"{row.id}: feasibility check failed: {doc['error']}"]
        else:
            problems, widen = verdict(doc["answer"], row, overlap or _overlap)
            lines = [f"{row.id}: infeasible: {p}" for p in problems]
            lines += [f"{row.id}: files cell lacks `{p}` (the change must edit it)" for p in widen]
        with lock:
            out.extend(lines)
    threads = [threading.Thread(target=one, args=(r,), daemon=True) for r in rows]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(out)


def _overlap(a: list[str], b: list[str]) -> bool:
    return any(p == q or (q.endswith("/") and p.startswith(q)) or (p.endswith("/") and q.startswith(p))
               for p in a for q in b)
