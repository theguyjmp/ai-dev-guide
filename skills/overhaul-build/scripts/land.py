"""Land one function's branch on main (overhaul v2). Deterministic, no LLM, Python stdlib only.

    py -3 land.py --repo R --fn ID [--main main] [--wait 30] [--timeout 1800] [--polish] [--parallel] [--target-dir D]

Sequential under the lock file R/.overhaul/lock (created with O_EXCL, removed on exit; drive.py takes the same lock to
write state.json and can import acquire_lock/release_lock from here). All work happens in the worktree
R/.overhaul/wt/land, never in R's own working tree. --parallel (what drive.py runs) gates steps 1-5 on main outside
the lock in R/.overhaul/wt/land-<ID> and takes the lock only for step 6; if main moved meanwhile it re-merges main
and reruns the whole-tree commands, its own acceptance and only the proven rows the newly landed work can affect
(regate_rows: newly proven rows, rows of touched modules and their `Depends on:` dependents; every row when a newly
landed file lies outside every module's Paths). [harness] heavy_tests (globs) run through [harness] heavy_runner
(placeholders {sha} {stem} {test} {file} {node} {slot}; exit 0 = pass; an unreachable host retries once, then runs
locally). [harness] land_timeout_min (default 20, 0 = off) stops the land: event land_timeout names the slowest test.

  1. The branch fn/<ID> must not edit plan files (spec/, tests/acceptance/, overhaul.toml): those change only in a plan
     commit on main. Planned tests are anchors (anchors.py): a test file that was in the plan commit may not be
     modified, renamed or deleted; new test files are fine.
  2. RED BEFORE: the function's acceptance must FAIL on main. If it passes: `acceptance already green before change`.
  3. Merge main into fn/<ID> (no force, no rewrite).
  4. The whole-tree commands test, lint, dup, literals, shape, cycles and entry_lint from overhaul.toml [commands] must
     pass on the merged tree, then every [commands.extra] command (name = command: project lints such as stylelint
     or an i18n check). A pytest exit of 5 (no tests collected) counts as pass.
  5. GREEN AFTER: the function's acceptance passes on the merged tree, and every row already `proven` still passes.
     Tests run batched: one command per test target (cargo `--test <stem>` with every exact name, one pytest call),
     failures mapped back to test ids; the first failing id then reruns alone so land.out holds its own output.
     A whole-tree `test` failure or a regression outside the branch's footprint reruns alone on clean main up to 3
     times; if it fails there too the reason starts `main flake: test=.. path=.. module=..` and drive.py sends it
     to a fix in the owning module instead of failing the function. A proven test whose file matches a [tiers]
     glob (`scale = ["tests/acceptance/scale_*.rs"]`) reruns only when its row's module is the function's own or a
     module whose ARCHITECTURE.md Paths the branch changes (or a stacked row's); the function's own always run.
     A tier test that fails reruns alone, serially after the batch, up to 2 more times before it is a regression
     (a timing bench starved by builders); a pass prints `tier rerun passed`. Acceptance runs at ABOVE_NORMAL.
  6. One commit sets the row's status cell to `proven` in FUNCTIONS.md; main fast-forwards to it (and fn/<ID> too).

FUNCTIONS.md rows: | id | phase | module | deps | entry | acceptance | status |, acceptance =
`tests/acceptance/<f>.py::<test>` (or `<f>.rs`) items separated by `;`. The acceptance command template takes {test}
(the test name), {file} (the test file), {stem} (the file name without suffix: a Rust [[test]] target) or {node}
(file::test); `<skill>` in any command is this skill's directory. [commands.acceptance_by] maps a glob on the test
file to its own template (`"tests/acceptance/scale_*.rs" = "cargo test --release --test {stem} -- --exact {test}
--test-threads=1"`); the first matching glob wins, else [commands] acceptance.

Writes the untracked R/.overhaul/state.json (functions[ID]: status proven on success, last_reason on failure; attempts
are drive.py's) and appends one line to R/.overhaul/log.jsonl. Any failure prints one reason line, exits 1 and leaves
main untouched. The failing command's full output stays in R/.overhaul/land.out. CARGO_TARGET_DIR is set to
--target-dir (default R/.overhaul/target/land), reused by every land and never shared with a concurrent one.

--polish lands a polish fix branch fn/<ID> (no FUNCTIONS row, no red-before): steps 1, 3 and 4, then every proven row
still passes and main fast-forwards to the merged commit. It logs polish_land_ok or polish_land_fail and writes no
state.json entry.
"""
import argparse
import datetime
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import PurePosixPath

import anchors  # beside this script
import index_api  # beside this script: the capability INDEX files and the semantic duplicate gate
import memcap  # beside this script: [limits] proc_mem_gb
import priority  # beside this script: Windows process priority classes
os.environ.setdefault("PROPTEST_DISABLE_FAILURE_PERSISTENCE", "1")  # no .proptest-regressions files (vc1)
os.environ.setdefault("PROPTEST_RNG_SEED", "0")  # fixed seed: a property green at land stays green on main (vc1)
os.environ.setdefault("OVERHAUL_HARNESS", "land")  # the main guard lets only the harness move main's code
# Every git call here names its repo by cwd, so an inherited GIT_DIR (set by git for hooks and `rebase -x`) is always
# wrong: it sent a test's `git init` into the caller's repo, set core.bare=true there and installed the guard (2026-11-10).
GIT_LOCAL_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_NO_REPLACE_OBJECTS",
                 "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX", "GIT_SHALLOW_FILE", "GIT_NAMESPACE")
for _var in GIT_LOCAL_ENV:  # drive.py imports land first, so this covers drive and every child it starts
    os.environ.pop(_var, None)

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHOLE_TREE = ("test", "lint", "dup", "literals", "shape", "cycles", "entry_lint")
PLAN_PATHS = ("spec/", "tests/acceptance/", "overhaul.toml")
GREEN_BEFORE = "acceptance already green before change"


class LandFail(Exception):
    """One reason line; the land stops and main stays untouched."""


class LandTimeout(LandFail):
    """The land ran past [harness] land_timeout_min: logged as land_timeout, naming the slowest test."""


LAND_TIMEOUT = "land timeout:"
LAND_TIMEOUT_MIN = 20.0  # [harness] land_timeout_min default; 0 = no budget
# The land's time budget: `end` (time.monotonic) or None, and (seconds, label) of every acceptance run so far.
BUDGET = {"end": None, "minutes": 0.0, "times": []}


def start_budget(minutes):
    BUDGET.update(end=time.monotonic() + minutes * 60 if minutes > 0 else None, minutes=minutes, times=[])


def pause_budget(seconds):
    """Time spent waiting (for the lock, for stacked lands) is not the land's own: push the deadline back."""
    if BUDGET["end"] is not None:
        BUDGET["end"] += max(0.0, seconds)


def slowest():
    """(label, seconds) of the slowest acceptance run so far, or ("", 0)."""
    return max(((lbl, s) for s, lbl in BUDGET["times"]), key=lambda x: x[1], default=("", 0.0))


def timeout_reason():
    lbl, secs = slowest()
    return (f"{LAND_TIMEOUT} over {BUDGET['minutes']:g} min ([harness] land_timeout_min); slowest test "
            f"{lbl or '?'} ({secs:.0f}s): split it or move it to [harness] heavy_tests")


def land_timeout_min(conf):
    try:
        return float((conf.get("harness") or {}).get("land_timeout_min", LAND_TIMEOUT_MIN) or 0)
    except (TypeError, ValueError):
        return LAND_TIMEOUT_MIN


def check_anchors(repo, base, head, branch, fpath):
    """Planned tests are anchors: a branch may add tests but never modify, rename or delete one (anchors.py)."""
    bad = anchors.touched_anchors(repo, base, head, fpath)
    if bad:
        raise LandFail(f"{branch} modifies planned tests (anchors): {', '.join(bad[:5])}")


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def git(cwd, *args, check=True):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        msg = [ln.strip() for ln in (p.stderr.strip() or p.stdout.strip()).splitlines() if ln.strip()]
        text = " / ".join(msg)[:600] if msg else "exit %d" % p.returncode  # git's cause comes before "Aborting"
        raise LandFail(f"git {args[0]} failed: {text}")
    return p


# ---- lock -----------------------------------------------------------------------------------------------------------

def lock_path(repo, name="lock"):
    return os.path.join(repo, ".overhaul", name)


def pid_alive(pid):
    """Whether this pid is running. Never signals it: on Windows os.kill would terminate the process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.GetLastError() == 5  # access denied: the process exists
        code = ctypes.c_ulong()
        known = k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(handle))
        return bool(known) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_is_stale(path):
    """True if the recorded holder pid is gone (a holder killed hard never removes its lock), or if the lock was
    created but never written for a minute."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = re.search(r"\bpid (\d+)", f.read())
        if m:
            return not pid_alive(int(m.group(1)))
        return time.time() - os.path.getmtime(path) > 60
    except OSError:
        return False


def acquire_lock(repo, wait=30.0, owner="land.py", name="lock", on_wait=None, every=60.0):
    """Create .overhaul/lock exclusively, waiting up to `wait` seconds. Returns False if another holder keeps it.
    A lock whose holder is dead is broken."""
    path = lock_path(repo, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.monotonic() + max(wait, 0)
    next_note = time.monotonic()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if lock_is_stale(path):
                try:
                    os.remove(path)
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                return False
            if on_wait and time.monotonic() >= next_note:
                next_note = time.monotonic() + every
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        on_wait(f.read().strip())
                except OSError:
                    pass
            time.sleep(0.2)
            continue
        with os.fdopen(fd, "w") as f:
            f.write(f"{owner} pid {os.getpid()} since {now_iso()}\n")
        return True


def release_lock(repo, name="lock"):
    try:
        os.remove(lock_path(repo, name))
    except FileNotFoundError:
        pass


# ---- FUNCTIONS.md ---------------------------------------------------------------------------------------------------

def _clean(cell):
    return cell.strip().strip("`").strip()


def _split(line):
    """(body, pipe positions, cells) for a table row, else None."""
    body = line.rstrip("\r\n")
    if not body.lstrip().startswith("|"):
        return None
    pipes = [i for i, ch in enumerate(body) if ch == "|"]
    if not body.rstrip().endswith("|"):
        pipes.append(len(body))
    return body, pipes, [body[pipes[k] + 1:pipes[k + 1]] for k in range(len(pipes) - 1)]


def parse_functions(text):
    """{id: {"acceptance": [nodes], "status": str, "line": index, "status_col": k}} from the markdown tables."""
    cols = {"id": 0, "module": 2, "acceptance": 5, "status": 6}
    rows = {}
    for n, line in enumerate(text.splitlines(keepends=True)):
        parts = _split(line)
        if not parts:
            continue
        cells = [_clean(c) for c in parts[2]]
        low = [c.lower() for c in cells]
        if {"id", "acceptance", "status"} <= set(low):
            cols = {name: low.index(name) if name in low else cols[name] for name in cols}
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c) or len(cells) <= max(cols.values()):
            continue
        fid = cells[cols["id"]]
        if not fid:
            continue
        nodes = [x.strip().strip("`").strip() for x in cells[cols["acceptance"]].split(";")]
        rows[fid] = {"acceptance": [x for x in nodes if x and x != "-"], "status": cells[cols["status"]].lower(),
                     "line": n, "status_col": cols["status"],
                     "module": cells[cols["module"]] if len(cells) > cols["module"] else ""}
    return rows


def set_status(text, row, status="proven"):
    lines = text.splitlines(keepends=True)
    line = lines[row["line"]]
    body, pipes, cells = _split(line)
    k = row["status_col"]
    width = len(cells[k])
    new = f" {status} " if width <= len(status) + 2 else f" {status}".ljust(width)
    lines[row["line"]] = body[:pipes[k] + 1] + new + body[pipes[k + 1]:] + line[len(body):]
    return "".join(lines)


def commit_status(repo, main, fpath, fid, message):
    """Set fid's status cell to proven in one commit on main, as a land does (a manager's --mark-proven: without it
    the row says planned while the state says proven, and plan_validate fails every later plan fix; vc1 F05)."""
    wt = os.path.join(repo, ".overhaul", "wt", "mark")
    prepare_worktree(repo, wt)
    main_sha = git(repo, "rev-parse", main).stdout.strip()
    git(wt, "checkout", "-q", "--detach", main_sha)
    fp = os.path.join(wt, fpath)
    with open(fp, encoding="utf-8", newline="") as f:
        text = f.read()
    rows = parse_functions(text)
    if fid not in rows:
        raise LandFail(f"{fid} is not a row of {fpath}")
    if rows[fid]["status"] == "proven":
        return main_sha
    with open(fp, "w", encoding="utf-8", newline="") as f:
        f.write(set_status(text, rows[fid]))
    git(wt, "add", "--", fpath)
    git(wt, "commit", "-q", "-m", message)
    new = git(wt, "rev-parse", "HEAD").stdout.strip()
    advance(repo, main, new, main_sha)
    return new


# ---- commands -------------------------------------------------------------------------------------------------------

def passes(cmd, code):
    """Exit 0 passes; a pytest command that collected no tests (exit 5) passes too."""
    return code == 0 or (code == 5 and "pytest" in cmd)


def fill(template, **values):
    skill = SKILL_DIR.replace("\\", "/")
    out = template.replace("<skill>", f'"{skill}"' if " " in skill else skill)
    for key, val in values.items():
        out = out.replace("{%s}" % key, val)
    return out


def kill_tree(proc):
    """Kill a shell command and its children (cargo under cmd.exe), by PID."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.kill()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def run(cmd, cwd, out_path, timeout, level=priority.NORMAL, label=""):
    """Run one shell command in cwd at priority `level` (Windows); output goes to out_path. Returns (exit code,
    last output line). Inside the land's time budget (BUDGET) a command still running at its end is killed and
    LandTimeout raised. `label` (a test id) records the run's seconds for the land_timeout event."""
    end = BUDGET["end"]
    left = None if end is None else end - time.monotonic()
    if left is not None and left <= 0:
        raise LandTimeout(timeout_reason())
    wait = timeout if left is None else min(timeout, left)
    t0 = time.monotonic()
    with open(out_path, "wb") as out:
        out.write(f"$ {cmd}\n".encode())
        out.flush()
        proc = subprocess.Popen(cmd, cwd=cwd, shell=True, stdout=out, stderr=subprocess.STDOUT,
                                **priority.popen_kwargs(level))
        try:
            code = proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            if label:
                BUDGET["times"].append((time.monotonic() - t0, label))
            if wait < timeout:
                raise LandTimeout(timeout_reason())
            return -1, f"timeout after {timeout}s"
    if label:
        BUDGET["times"].append((time.monotonic() - t0, label))
    with open(out_path, "rb") as f:
        text = f.read().decode("utf-8", "replace")
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    tail = lines[-1] if len(lines) > 1 else ""
    return code, (memcap.note(text, tail[:200]) if code else tail[:200])


def acceptance_template(commands):
    """The acceptance template of a [commands] table: the plain string, or with [commands.acceptance_by] or a
    heavy runner (load_commands) a dict {"default": str, "by": [(glob, str)], "heavy": {...} or None} that
    tmpl_for resolves per test file."""
    by = commands.get("acceptance_by")
    by = [(str(g), str(t)) for g, t in by.items() if str(t).strip()] if isinstance(by, dict) else []
    default = str(commands.get("acceptance") or "")
    heavy = commands.get(HEAVY_KEY)
    return {"default": default, "by": by, "heavy": heavy} if by or heavy else default


# ---- heavy tests: [harness] heavy_tests run through [harness] heavy_runner on another machine ----------------------

HEAVY_KEY = "_heavy"  # load_commands puts {"globs", "runner"} under this key of the commands table
# A runner that could not reach its host: ssh's own exit code, or git/ssh connection errors in its output.
UNREACHABLE = re.compile(r"Could not resolve hostname|ssh: connect to host|Connection (?:timed out|refused|reset|"
                         r"closed)|No route to host|Network is unreachable|Could not read from remote repository|"
                         r"kex_exchange_identification|Host key verification failed", re.I)


def load_commands(conf):
    """[commands] of overhaul.toml, plus the heavy runner from [harness] heavy_tests (globs on test files) and
    heavy_runner (a command template) when both are set."""
    commands = dict(conf.get("commands", {}))
    har = conf.get("harness") or {}
    globs = har.get("heavy_tests") or []
    globs = [str(g) for g in ([globs] if isinstance(globs, str) else globs) if str(g).strip()]
    runner = str(har.get("heavy_runner") or "").strip()
    if globs and runner:
        commands[HEAVY_KEY] = {"globs": globs, "runner": runner}
    return commands


def heavy_of(template, node):
    """The heavy runner config when node's test file matches [harness] heavy_tests, else None."""
    heavy = template.get("heavy") if isinstance(template, dict) else None
    return heavy if heavy and under(node.partition("::")[0].replace("\\", "/"), heavy["globs"]) else None


def heavy_run(template, node, wt, out_path, timeout):
    """Run one heavy test through the heavy runner: {sha} (wt's HEAD, the tree under test), {test}, {file},
    {stem}, {node} and {slot} (this land's target dir name, so concurrent lands use separate remote checkouts).
    Exit 0 passes, like a local run. A runner that cannot reach its host retries once, then the test runs
    locally. Returns (code, tail)."""
    heavy = heavy_of(template, node)
    file, _, name = node.partition("::")
    sha = git(wt, "rev-parse", "HEAD").stdout.strip()
    slot = os.path.basename(os.environ.get("CARGO_TARGET_DIR", "") or "land") or "land"
    cmd = fill(heavy["runner"], sha=sha, test=name or file, file=file, stem=PurePosixPath(file).stem, node=node,
               slot=slot)
    for k in (1, 2):
        code, tail = run(cmd, wt, out_path, timeout, ACCEPTANCE_PRIORITY, label=node)
        if not (code == 255 or (code not in (0, -1) and UNREACHABLE.search(read_out(out_path)))):
            return code, tail
        note = f"heavy runner unreachable (exit {code}, try {k}/2): {tail}"
        print(note, flush=True)
    code, tail = run(acceptance_cmds(template, [node])[0][1], wt, out_path, timeout, ACCEPTANCE_PRIORITY,
                     label=node)
    print(f"heavy test {node} ran locally (exit {code})", flush=True)
    return code, tail


def run_node(template, node, wt, out_path, timeout):
    """One test id alone: through the heavy runner when it is a heavy test, else its own local command."""
    if heavy_of(template, node):
        return heavy_run(template, node, wt, out_path, timeout)
    return run(acceptance_cmds(template, [node])[0][1], wt, out_path, timeout, ACCEPTANCE_PRIORITY, label=node)


def tmpl_for(template, node):
    """The command template for one test id: the first acceptance_by glob matching its file, else the default."""
    if isinstance(template, str):
        return template
    file = node.partition("::")[0].replace("\\", "/")
    return next((t for g, t in template["by"] if fnmatch.fnmatchcase(file, g)), template["default"])


def extra_cmds(commands):
    """[(name, command)] of [commands.extra]: project gates run after the whole-tree commands."""
    extra = commands.get("extra")
    return [(f"extra.{k}", str(v)) for k, v in extra.items() if str(v).strip()] if isinstance(extra, dict) else []


def gate_cmds(commands, names=WHOLE_TREE):
    """[(name, command)] the whole-tree gate runs: the named commands that are set, then [commands.extra]."""
    return [(n, str(commands[n])) for n in names if isinstance(commands.get(n), str) and commands[n]] +         extra_cmds(commands)


def tier_globs(conf):
    """Every glob of every [tiers] list (scale, gpu, ...): test files whose proven tests rerun only by footprint."""
    tiers = conf.get("tiers")
    if not isinstance(tiers, dict):
        return []
    return [str(g) for v in tiers.values() for g in (v if isinstance(v, list) else [v]) if str(g).strip()]


def module_paths(arch):
    """{module: [paths]} from the `## Module <name>` sections' Paths lines of ARCHITECTURE.md."""
    mods, cur = {}, None
    for line in arch.splitlines():
        m = re.match(r"^##\s+Module\s*:?\s+(\S+)", line, re.I)
        if m:
            cur = m.group(1).strip("`:*")
            mods.setdefault(cur, [])
            continue
        if re.match(r"^#{1,2}\s", line):
            cur = None
        m = re.match(r"^[\s>*-]*\**\s*Paths\s*:\s*\**\s*(.*)$", line, re.I) if cur else None
        if m:
            mods[cur] = [x.strip().strip("`").strip() for x in re.split(r"[,;]", m.group(1).strip().strip("*"))
                         if x.strip().strip("`").strip()]
    return mods


def under(path, pats):
    """Whether a repo path lies in one of pats (a file, a directory with or without /, or a glob)."""
    for p in pats:
        if any(c in p for c in "*?["):
            if fnmatch.fnmatchcase(path, p):
                return True
        elif path == p or path.startswith(p.rstrip("/") + "/"):
            return True
    return False


def touched_modules(arch, touched):
    """The modules whose Paths hold at least one touched file."""
    return {m for m, pats in module_paths(arch).items() if any(under(t, pats) for t in touched)}


def module_deps(arch):
    """{module: {modules it depends on}} from the `Depends on:` lines of ARCHITECTURE.md's module sections."""
    deps, cur = {}, None
    for line in arch.splitlines():
        m = re.match(r"^##\s+Module\s*:?\s+(\S+)", line, re.I)
        if m:
            cur = m.group(1).strip("`:*")
            deps.setdefault(cur, set())
            continue
        if re.match(r"^#{1,2}\s", line):
            cur = None
        m = re.match(r"^[\s>*-]*\**\s*Depends on\s*:\s*\**\s*(.*)$", line, re.I) if cur else None
        if m:
            deps[cur] = {x.strip().strip("`*").strip() for x in re.split(r"[,;]", m.group(1))
                         if x.strip().strip("`*").strip() not in ("", "-", "none", "None")}
    return deps


def dependents(arch, mods):
    """mods plus every module that depends on one of them, directly or through others."""
    deps, out = module_deps(arch), set(mods)
    grew = True
    while grew:
        grew = False
        for m, ds in deps.items():
            if m not in out and ds & out:
                out.add(m)
                grew = True
    return out


def regate_rows(repo, old, new, fpath, apath, rows_now, fid):
    """What a land whose base moved from `old` to `new` must rerun on the re-merged tree: (row ids, newly proven
    row ids, modules) or None for every proven row. The rows are those newly proven between old and new, every
    proven row of a module the newly landed changes touch or of a module depending on one (ARCHITECTURE.md
    `Depends on:`), and every row whose module ARCHITECTURE.md does not list. A newly landed file outside every
    module's Paths (a lock file, a test, overhaul.toml), other than spec/*.md, means every row."""
    files = [f for f in git(repo, "diff", "--name-only", old, new).stdout.splitlines() if f and f != fpath]
    old_rows = parse_functions(git(repo, "show", f"{old}:{fpath}", check=False).stdout)
    proven = {r for r, row in rows_now.items() if row["status"] == "proven" and r != fid}
    newly = {r for r in proven if old_rows.get(r, {}).get("status") != "proven"}
    arch = git(repo, "show", f"{new}:{apath}", check=False).stdout
    paths = module_paths(arch)
    hit = set()
    for f in files:
        mods = {m for m, pats in paths.items() if under(f, pats)}
        if not mods and not (f.startswith("spec/") and f.endswith(".md")):
            return None
        hit |= mods
    return newly | footprint_rows(arch, rows_now, hit, fid), newly, hit


def footprint_rows(arch, rows, mods, fid=""):
    """The proven rows (fid aside) a change to modules `mods` can break: rows of those modules or of a module
    depending on one (ARCHITECTURE.md `Depends on:`), and every row whose module ARCHITECTURE.md does not list.
    regate_rows, and drive.py's build brief and pre-review gate, all pick a footprint this way."""
    paths, affected = module_paths(arch), dependents(arch, mods)
    return {r for r, row in rows.items() if row["status"] == "proven" and r != fid
            and (row.get("module", "") in affected or row.get("module", "") not in paths)}


def acceptance_cmds(template, nodes):
    """[(node, command)], one per test id; template is a string or an acceptance_template dict."""
    cmds = []
    for node in nodes:
        file, _, name = node.partition("::")
        tmpl = tmpl_for(template, node)
        if not any(p in tmpl for p in ("{test}", "{file}", "{node}", "{stem}")):
            cmds.append((node, f"{fill(tmpl)} {node}"))
        else:
            cmds.append((node, fill(tmpl, test=name or file, file=file, node=node, stem=PurePosixPath(file).stem)))
    return cmds


# ---- batched runs: one command per test target, failures mapped back to test ids -------------------------------------

CARGO_TEST = re.compile(r"\bcargo\s+(?:\+\S+\s+)?test\b")
PYTEST = re.compile(r"(?:-m\s+pytest|\bpytest(?:\.exe)?)(?![\w.])")
BATCH_MAX = 6000  # characters per batched command line (cmd.exe stops at 8191)
LIBTEST_FAIL = re.compile(r"^(?:---- (\S+) stdout ----|test (\S+) \.\.\. FAILED)", re.M)
LIBTEST_LIST = re.compile(r"^failures:\r?\n((?:    \S+\r?\n?)+)", re.M)
PYTEST_FAIL = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
SUMMARY = re.compile(r"^test result: |\b\d+ (?:passed|failed|errors?)\b.* in [\d.]+s", re.M)


def batchable(template):
    """Whether one command can run several tests of a target: cargo test with {test} (libtest takes several exact
    filters) or pytest with {test} or {node}. Any other runner keeps one command per test id."""
    if CARGO_TEST.search(template):
        return "{test}" in template and "{node}" not in template
    return bool(PYTEST.search(template)) and ("{test}" in template or "{node}" in template)


def _joined(template, nodes):
    """The one command for nodes of one target: {file} and {stem} from the first, the test names joined."""
    file = nodes[0].partition("::")[0]
    key = fill(template, file=file, stem=PurePosixPath(file).stem)
    names = [n.partition("::")[2] or n for n in nodes]
    if "{node}" in key:
        key = key.replace("{node}", " ".join(nodes))
    if "-k {test}" in key:
        key = key.replace("-k {test}", '-k "' + " or ".join(names) + '"')
    return key.replace("{test}", " ".join(names))


def batch_cmds(template, nodes):
    """[(nodes, command)]: the nodes of one test target (the same command but for the test names) share one
    command, split only to keep a command line under BATCH_MAX. A lone node gets exactly its per-id command."""
    nodes = list(dict.fromkeys(nodes))
    if not isinstance(template, str):  # acceptance_by: each template batches its own nodes
        per = {}
        for node in nodes:
            per.setdefault(tmpl_for(template, node), []).append(node)
        return [x for t, group in per.items() for x in batch_cmds(t, group)]
    if not batchable(template) or len(nodes) < 2:
        return [([n], c) for n, c in acceptance_cmds(template, nodes)]
    groups = {}
    for node in nodes:
        file = node.partition("::")[0]
        groups.setdefault(fill(template, file=file, stem=PurePosixPath(file).stem), []).append(node)
    chunks = []
    for group in groups.values():
        chunk = []
        for node in group:
            if chunk and len(_joined(template, chunk + [node])) > BATCH_MAX:
                chunks.append(chunk)
                chunk = []
            chunk.append(node)
        chunks.append(chunk)
    return [(c, acceptance_cmds(template, c)[0][1] if len(c) == 1 else _joined(template, c)) for c in chunks]


def failed_names(output):
    """Test names a libtest or pytest run reports as failed."""
    names = {a or b for a, b in LIBTEST_FAIL.findall(output)}
    for block in LIBTEST_LIST.findall(output):
        names.update(x.strip() for x in block.splitlines() if x.strip())
    names.update(PYTEST_FAIL.findall(output))
    return names


def _hit(node, names):
    """Whether a failed test name belongs to node (file::test): a libtest name may carry a module path, a pytest
    node id a path and parameters, and a pytest -k name matches as a substring."""
    file, _, name = node.partition("::")
    name = name or file
    for got in names:
        last = got.split("::")[-1].split("[")[0]
        if got in (node, name) or got.endswith("::" + name) or last == name or (".py::" in got and name in last):
            return True
    return False


def batch_results(template, nodes, runner):
    """{node: (exit code, tail, alone)} for every node; runner(cmd) -> (code, tail, full output). A batch that exits
    0 passes every node in it; a failed batch fails the nodes its output names and passes the rest. When the
    output names none of them or has no summary line (a compile error, a crash), each node reruns alone. A timeout
    fails the whole batch."""
    res = {}
    for group, cmd in batch_cmds(template, nodes):
        code, tail, output = runner(cmd)
        if len(group) == 1 or code == 0 or code in (-1, 124):
            res.update({n: (code, tail, len(group) == 1) for n in group})
            continue
        hits = [n for n in group if _hit(n, failed_names(output))]
        if hits and SUMMARY.search(output):
            res.update({n: (code, tail, False) if n in hits else (0, "", False) for n in group})
            continue
        for node, one in acceptance_cmds(template, group):
            c, t, _ = runner(one)
            res[node] = (c, t, True)
    return res


def read_out(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def red_nodes(template, nodes, wt, out_path, timeout, alone=True):
    """[(node, code, tail)] of the nodes that fail in wt, in order, batched by test target. With `alone`, the first
    one found in a batch reruns by itself so out_path holds its own output (the batch verdict stands). Heavy tests
    ([harness] heavy_tests) run one by one through the heavy runner in a thread beside the local batches; their
    output goes to <out_path>-heavy.out and is copied to out_path when one is the first failure."""
    nodes = list(dict.fromkeys(nodes))
    heavy = [n for n in nodes if heavy_of(template, n)]
    local = [n for n in nodes if n not in heavy]
    labels = {c: (g[0] if len(g) == 1 else f"{g[0].partition('::')[0]} ({len(g)} tests)")
              for g, c in batch_cmds(template, local)} if local else {}
    labels.update({c: n for n, c in acceptance_cmds(template, local)})

    def runner(cmd):
        code, tail = run(cmd, wt, out_path, timeout, ACCEPTANCE_PRIORITY, label=labels.get(cmd, ""))
        return code, tail, read_out(out_path)
    res, errors, heavy_out = {}, [], os.path.splitext(out_path)[0] + "-heavy.out"

    def remote():
        try:
            for n in heavy:
                code, tail = heavy_run(template, n, wt, heavy_out, timeout)
                res[n] = (code, tail, True)
        except BaseException as e:  # noqa: BLE001 - re-raised on the calling thread
            errors.append(e)
    thread = None
    if heavy:
        thread = threading.Thread(target=remote, daemon=True)
        thread.start()
    try:
        if local:
            res.update(batch_results(template, local, runner))
    finally:
        if thread:
            thread.join()
    if errors:
        raise errors[0]
    bad = [(n, res[n][0], res[n][1]) for n in nodes if res[n][0] != 0]
    if bad and bad[0][0] in heavy:
        shutil.copyfile(heavy_out, out_path)
    elif bad and alone and not res[bad[0][0]][2]:
        code, tail = run_node(template, bad[0][0], wt, out_path, timeout)
        if code != 0:
            bad[0] = (bad[0][0], code, tail)
    return bad


# ---- the land -------------------------------------------------------------------------------------------------------

def prepare_worktree(repo, wt):
    if os.path.isdir(wt):
        top = git(wt, "rev-parse", "--show-toplevel", check=False).stdout.strip()
        if top and os.path.normcase(os.path.realpath(top)) == os.path.normcase(os.path.realpath(wt)):
            git(wt, "merge", "--abort", check=False)
            git(wt, "reset", "-q", "--hard")
            git(wt, "clean", "-ffdq")
            return
        shutil.rmtree(wt, ignore_errors=True)
    git(repo, "worktree", "prune")
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    git(repo, "worktree", "add", "-q", "--detach", wt, "HEAD")


DIRTY_MAIN = "uncommitted changes to tracked files in the main checkout"


def dirty_tracked(wt):
    """Tracked files with uncommitted changes in a worktree (untracked files do not count)."""
    out = git(wt, "status", "--porcelain", "--untracked-files=no", check=False).stdout
    return [ln[3:].strip() for ln in out.splitlines() if ln.strip()]


def checked_out_at(repo, branch):
    """Path of the worktree that has `branch` checked out, else None."""
    path = None
    for line in git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def advance(repo, branch, new, old):
    """Fast-forward `branch` from old to new; the worktree holding it (if any) moves with it."""
    if git(repo, "rev-parse", f"refs/heads/{branch}").stdout.strip() != old:
        raise LandFail(f"{branch} moved during land")
    wt = checked_out_at(repo, branch)
    if wt:
        dirty = dirty_tracked(wt)
        if dirty:
            raise LandFail(f"{DIRTY_MAIN} {wt}: {', '.join(dirty[:5])}")
        git(wt, "merge", "--ff-only", "-q", new)
    else:
        git(repo, "update-ref", f"refs/heads/{branch}", new, old)


def land(repo, fid, main, timeout):
    """The whole land under the caller's lock: gate, then commit and fast-forward."""
    return commit_land(repo, gate_branch(repo, fid, main, timeout), main, timeout)


def gate_branch(repo, fid, main, timeout, stack=(), own=False):
    """Steps 1 to 5 on main as it is now; returns the context commit_land needs. With `stack` (functions whose lands
    are in flight, footprints disjoint from this one) the gate runs on main + their branches + this branch, so this
    land can commit right after theirs without a second gate. Runs without the lock when stacked or `own` (a
    parallel land: its own worktree and output file, gated on main alone)."""
    ovh = os.path.join(repo, ".overhaul")
    own = own or bool(stack)
    out_path = os.path.join(ovh, f"land-{fid}.out" if own else "land.out")
    wt = os.path.join(ovh, "wt", f"land-{fid}" if own else "land")
    branch = f"fn/{fid}"

    main_sha = git(repo, "rev-parse", "--verify", f"refs/heads/{main}^{{commit}}").stdout.strip()
    fn = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", check=False)
    if fn.returncode != 0:
        raise LandFail(f"no branch {branch}")
    fn_sha = fn.stdout.strip()

    toml_src = git(repo, "show", f"{main_sha}:overhaul.toml", check=False)
    if toml_src.returncode != 0:
        raise LandFail(f"overhaul.toml not on {main}")
    try:
        conf = tomllib.loads(toml_src.stdout)
    except tomllib.TOMLDecodeError as e:
        raise LandFail(f"overhaul.toml unreadable: {e}")
    commands = load_commands(conf)
    fpath = conf.get("paths", {}).get("functions", "spec/FUNCTIONS.md")
    if not commands.get("acceptance"):
        raise LandFail("overhaul.toml has no [commands] acceptance")

    ftext = git(repo, "show", f"{main_sha}:{fpath}", check=False)
    if ftext.returncode != 0:
        raise LandFail(f"{fpath} not on {main}")
    rows = parse_functions(ftext.stdout)
    if fid not in rows:
        raise LandFail(f"unknown function {fid} in {fpath}")
    if rows[fid]["status"] == "proven":
        raise LandFail(f"function {fid} already proven")
    if not rows[fid]["acceptance"]:
        raise LandFail(f"no acceptance for {fid} in {fpath}")
    acc = acceptance_template(commands)
    mine = acceptance_cmds(acc, rows[fid]["acceptance"])

    touched = git(repo, "diff", "--name-only", f"{main_sha}...{fn_sha}").stdout.splitlines()
    bad = [p for p in touched if p.startswith(PLAN_PATHS) or p == fpath]
    if bad:
        raise LandFail(f"{branch} edits plan files: {', '.join(bad[:5])}")
    check_anchors(repo, main_sha, fn_sha, branch, fpath)
    if dup := semantic_dup(repo, conf, main_sha, fn_sha, touched):
        raise LandFail(dup)

    prepare_worktree(repo, wt)

    # RED BEFORE, on main's tree
    git(wt, "checkout", "-q", "--detach", main_sha)
    if not red_nodes(acc, rows[fid]["acceptance"], wt, out_path, timeout, alone=False):
        raise LandFail(GREEN_BEFORE)

    # Merge main into the branch
    git(wt, "checkout", "-q", "--detach", fn_sha)
    merged = git(wt, "merge", "-q", "--no-edit", "-m", f"land {fid}: merge {main} into {branch}", main_sha, check=False)
    if merged.returncode != 0:
        git(wt, "merge", "--abort", check=False)
        raise LandFail(f"merge conflict merging {main} into {branch}")
    plain = git(wt, "rev-parse", "HEAD").stdout.strip()
    stacked = [s for s in stack if s in rows and rows[s]["status"] != "proven"]
    for sid in stacked:
        got = git(wt, "merge", "-q", "--no-edit", "-m", f"land {fid}: stack fn/{sid}", f"refs/heads/fn/{sid}",
                  check=False)
        if got.returncode != 0:  # overlaps after all: land unstacked, commit_land regates on the new main
            git(wt, "merge", "--abort", check=False)
            git(wt, "checkout", "-q", "--detach", plain)
            stacked = []
            break
    ctx = dict(fid=fid, wt=wt, out_path=out_path, branch=branch, main_sha=main_sha, fn_sha=fn_sha,
               commands=commands, fpath=fpath, apath=arch_path(conf), rows=rows, mine=mine, stacked=stacked,
               touched=touched,
               tiers=tier_globs(conf), near=footprint_modules(repo, main_sha, conf, touched))
    gate_tree(ctx, rows, stacked, timeout)
    ctx["gated"] = git(wt, "rev-parse", "HEAD").stdout.strip()
    return ctx


def gate_tree(ctx, rows, extra, timeout, only=None, near_add=()):
    """Steps 4 and 5 on the tree checked out in ctx's worktree: whole-tree commands, this function's acceptance,
    then every proven row (and every stacked row in `extra`) still passes. With `only` (a re-gate after main
    moved) just those proven rows rerun, with `near_add` modules added to the [tiers] footprint."""
    wt, out_path, commands, fid = ctx["wt"], ctx["out_path"], ctx["commands"], ctx["fid"]
    for name, cmd in gate_cmds(commands):
        cmd = fill(cmd)
        code, tail = run(cmd, wt, out_path, timeout)
        if not passes(cmd, code):
            reason = f"{name} failed (exit {code}) on merged tree: {tail}"
            if name == "test":
                flake = unit_flake(ctx, cmd, read_out(out_path), timeout)
                if flake:
                    raise LandFail(f"{flake}; on the merged tree: {reason}")
            raise LandFail(reason)

    # GREEN AFTER, then no regression of proven rows
    acc = acceptance_template(commands)
    bad = red_nodes(acc, [n for n, _ in ctx["mine"]], wt, out_path, timeout)
    if bad:
        node, code, tail = bad[0]
        raise LandFail(f"acceptance still red after merge: {node} (exit {code}): {tail}")
    keep = [rid for rid, row in rows.items() if (row["status"] == "proven" or rid in extra) and rid != fid
            and (only is None or rid in only or rid in extra)]
    near = (set(ctx.get("near", ())) | {rows[fid].get("module", "")} | {rows[r].get("module", "") for r in extra}
            | set(near_add))
    hit = regression(rows, keep, commands, wt, out_path, timeout, ctx.get("tiers", ()), near, extra)
    if hit:
        rid, node, reason = hit
        row = rows[rid]
        if row.get("module") != rows[fid].get("module"):
            flake = flake_on_main(ctx, node, node.partition("::")[0], row.get("module", ""),
                                  acceptance_cmds(acc, [node])[0][1], timeout, acc)
            if flake:
                raise LandFail(f"{flake}; on the merged tree: {reason}")
        raise LandFail(reason)


def footprint_modules(repo, main_sha, conf, touched):
    """The modules whose ARCHITECTURE.md Paths hold a file the branch changes (empty without [tiers])."""
    if not tier_globs(conf):
        return set()
    apath = conf.get("paths", {}).get("architecture", "spec/ARCHITECTURE.md")
    arch = git(repo, "show", f"{main_sha}:{apath}", check=False).stdout
    return touched_modules(arch, touched)


def arch_path(conf):
    return conf.get("paths", {}).get("architecture", "spec/ARCHITECTURE.md")


def semantic_dup(repo, conf, main_sha, fn_sha, touched):
    """The semantic duplicate gate (index_api) on what the branch adds against main's tree, or ''. [harness]
    dup_semantic_min (0 turns it off) is the threshold."""
    thr = float(conf.get("harness", {}).get("dup_semantic_min", index_api.DUP_SEMANTIC_MIN))
    if thr <= 0:
        return ""
    mods = module_paths(git(repo, "show", f"{main_sha}:{arch_path(conf)}", check=False).stdout)
    return index_api.semantic_problem(repo, main_sha, fn_sha, touched, mods, thr)


def stage_index(wt, apath):
    """Regenerate the capability INDEX files on the merged tree in wt and stage them (S28: generated, never edited
    by hand). True when they changed."""
    arch = os.path.join(wt, apath)
    if not os.path.isfile(arch):
        return False
    with open(arch, encoding="utf-8") as f:
        index_api.regen_and_stage(wt, module_paths(f.read()))
    return git(wt, "diff", "--cached", "--quiet", "--", index_api.INDEX_DIR, index_api.SUMMARY,
               check=False).returncode != 0


def commit_index(wt, apath, msg):
    """stage_index, then commit the INDEX files alone when they changed: a polish land, and plan_land.py on a plan
    branch. True when it committed."""
    if not stage_index(wt, apath):
        return False
    git(wt, "commit", "-q", "-m", msg, "--", index_api.INDEX_DIR, index_api.SUMMARY)
    return True


def regression(rows, ids, commands, wt, out_path, timeout, tiers=(), near=None, extra=()):
    """(row id, node, reason) of the first of `ids` whose acceptance fails in wt, or None. All their tests run
    batched by test target, not one command per test. With `near` (a module set), a test whose file matches a
    [tiers] glob runs only when its row is in `extra` or its module is in `near`."""
    owner = {}
    for rid in ids:
        for node in rows[rid]["acceptance"]:
            if (near is not None and rid not in extra and rows[rid].get("module", "") not in near
                    and under(node.partition("::")[0], tiers)):
                continue
            owner.setdefault(node, rid)
    if not owner:
        return None
    bad = red_nodes(acceptance_template(commands), list(owner), wt, out_path, timeout)
    bad = tier_recovered(bad, acceptance_template(commands), tiers, owner, wt, out_path, timeout)
    if not bad:
        return None
    node, code, tail = bad[0]
    return owner[node], node, f"regression: proven {owner[node]} {node} fails (exit {code}): {tail}"


TIER_RERUNS = 2  # a failed [tiers] test (a timing bench) reruns alone this many more times before it counts
ACCEPTANCE_PRIORITY = priority.ABOVE_NORMAL  # land's acceptance runs: ahead of the BELOW_NORMAL builders


def tier_recovered(bad, template, tiers, owner, wt, out_path, timeout):
    """bad without the [tiers] tests that pass when rerun alone. A tier test runs only inside the footprint, and
    a timing bench starved by 16 builders fails a land it passes alone (pilotS123 on S02, 2026-11-09): each
    failed one reruns by itself, serially after the batch, up to TIER_RERUNS times; a pass drops it and writes
    `tier rerun passed` to out_path (land.out) and stdout. Reruns write <out_path>-tier.out, so land.out keeps a
    failing test's own output. Other proven tests keep the batch verdict."""
    keep, rerun_out = [], os.path.splitext(out_path)[0] + "-tier.out"
    for node, code, tail in bad:
        if not under(node.partition("::")[0], tiers):
            keep.append((node, code, tail))
            continue
        for k in range(1, TIER_RERUNS + 1):
            c, t = run_node(template, node, wt, rerun_out, timeout)
            if c == 0:
                note = (f"tier rerun passed: proven {owner.get(node, '?')} {node} failed (exit {code}) in the "
                        f"batch, passed alone on rerun {k}/{TIER_RERUNS} (output {rerun_out})")
                with open(out_path, "a", encoding="utf-8") as f:
                    f.write(note + "\n")
                print(note, flush=True)
                break
            code, tail = c, t
        else:
            keep.append((node, code, tail))
    return keep


# ---- main flakes: a test outside the function's footprint that fails on clean main too ------------------------------

MAIN_FLAKE = "main flake:"
FLAKE_RUNS = 3
RUNNING = re.compile(r"^\s*Running (?:unittests )?(\S+) \((?:.*[\\/])?([A-Za-z0-9_]+)-[0-9a-f]+(?:\.exe)?\)", re.M)


def flake_on_main(ctx, test, path, module, cmd, timeout, template=None):
    """A test that failed on the merged tree, outside the function's footprint (its file not among the branch's
    changes), reruns alone on clean main up to FLAKE_RUNS times. If it fails there too the branch did not cause
    it: returns the `main flake:` reason drive.py routes to a fix in the owning module; else ''."""
    if not test or (path and path in ctx.get("touched", ())):
        return ""
    wt, main = ctx["wt"], ctx.get("main_now") or ctx["main_sha"]
    out = os.path.join(os.path.dirname(ctx["out_path"]), f"land-{ctx['fid']}-flake.out")
    if git(wt, "checkout", "-q", "--detach", main, check=False).returncode != 0:
        return ""
    for k in range(1, FLAKE_RUNS + 1):
        if template is not None and heavy_of(template, test):
            code, _ = heavy_run(template, test, wt, out, timeout)
        else:
            code, _ = run(cmd, wt, out, timeout, ACCEPTANCE_PRIORITY)
        if code != 0:
            return (f"{MAIN_FLAKE} test={test} path={path or '?'} module={module or '?'} fails on clean main "
                    f"{main[:12]} too (run {k}/{FLAKE_RUNS}, output {out})")
    return ""


def unit_test_cmd(cmd, name):
    """The whole-tree test command narrowed to one failed test, when the runner allows it."""
    if CARGO_TEST.search(cmd):
        return f"{cmd} {'' if ' -- ' in cmd else '-- '}--exact {name}"
    m = PYTEST.search(cmd)
    return f"{cmd[:m.end()]} -q {name}" if m and "::" in name else cmd


def crate_dir(wt, crate):
    """The package directory (relative, posix) whose Cargo.toml names `crate` (- and _ alike), or ''."""
    want = crate.replace("-", "_")
    for root, dirs, files in os.walk(wt):
        dirs[:] = [d for d in dirs if d not in ("target", ".git", ".overhaul", "node_modules")]
        if "Cargo.toml" in files:
            try:
                with open(os.path.join(root, "Cargo.toml"), "rb") as f:
                    pkg = tomllib.load(f).get("package", {})
            except (OSError, tomllib.TOMLDecodeError):
                continue
            if str(pkg.get("name", "")).replace("-", "_") == want:
                rel = os.path.relpath(root, wt).replace("\\", "/")
                return "" if rel == "." else rel
    return ""


def unit_test_path(wt, name, output):
    """Best guess at the source file of a failed unit test: a pytest node id's file, or for libtest the module file
    of `name` inside the crate whose `Running` line precedes its failure."""
    if "::" in name and name.split("::")[0].endswith(".py"):
        return name.split("::")[0]
    at = output.find(f"---- {name} stdout ----")
    runs = [m for m in RUNNING.finditer(output) if at < 0 or m.start() < at]
    if not runs:
        return ""
    src, crate = runs[-1].group(1).replace("\\", "/"), runs[-1].group(2)
    pkg = crate_dir(wt, crate)
    base = f"{pkg}/" if pkg else ""
    parts = [p for p in name.split("::")[:-1] if p != "tests"]
    for cand in ([f"src/{'/'.join(parts)}.rs", f"src/{'/'.join(parts)}/mod.rs"] if parts else []):
        if os.path.isfile(os.path.join(wt, base + cand)):
            return base + cand
    return base + src


def unit_flake(ctx, cmd, output, timeout):
    """The whole-tree test command failed: route its first failed test through flake_on_main."""
    names = sorted(failed_names(output))
    if not names:
        return ""
    name = names[0]
    path = unit_test_path(ctx["wt"], name, output)
    return flake_on_main(ctx, name, path, "", unit_test_cmd(cmd, name), timeout)


def commit_land(repo, ctx, main, timeout):
    """Step 6 under the lock. When main has not moved and nothing was stacked, the gated tree lands as is. When
    main moved, main is merged in: if every stacked function landed and nothing else did, the tree equals the gated
    one (FUNCTIONS.md aside) and no gate reruns; otherwise the branch is merged with main afresh and gated again.
    main only fast-forwards, and only to a tree the gate passed."""
    wt, fpath, fid, branch = ctx["wt"], ctx["fpath"], ctx["fid"], ctx["branch"]
    cur = git(repo, "rev-parse", "--verify", f"refs/heads/{main}^{{commit}}").stdout.strip()
    if cur != ctx["main_sha"] or ctx["stacked"]:
        now = parse_functions(git(repo, "show", f"{cur}:{fpath}").stdout)
        before = {r for r, row in ctx["rows"].items() if row["status"] == "proven"}
        after = {r for r, row in now.items() if row["status"] == "proven"}
        if fid in after:
            raise LandFail(f"function {fid} already proven")
        stack_ok = bool(ctx["stacked"]) and after - before == set(ctx["stacked"])
        git(wt, "checkout", "-q", "--detach", ctx["gated"] if stack_ok else ctx["fn_sha"])
        merged = git(wt, "merge", "-q", "--no-edit", "-m", f"land {fid}: merge {main} into {branch}", cur,
                     check=False)
        if merged.returncode != 0:
            git(wt, "merge", "--abort", check=False)
            raise LandFail(f"merge conflict merging {main} into {branch}")
        same = stack_ok and git(wt, "diff", "--quiet", ctx["gated"], "HEAD", "--", ".", f":(exclude){fpath}",
                                f":(exclude){index_api.INDEX_DIR}", f":(exclude){index_api.SUMMARY}",
                                check=False).returncode == 0
        ctx["regated"] = not same
        if not same:  # main moved by something the gate did not see: gate the real tree, under the lock
            ctx["main_now"] = cur
            regate(repo, ctx, cur, now, timeout)
        ctx["main_sha"] = cur

    # The land commit: status proven, then main and the branch fast-forward to it
    main_sha, fn_sha = ctx["main_sha"], ctx["fn_sha"]
    fp = os.path.join(wt, fpath)
    with open(fp, encoding="utf-8", newline="") as f:
        text = f.read()
    text = set_status(text, parse_functions(text)[fid])
    with open(fp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    git(wt, "add", "--", fpath)
    stage_index(wt, ctx.get("apath", "spec/ARCHITECTURE.md"))
    git(wt, "commit", "-q", "-m", f"land {fid}: proven")
    new = git(wt, "rev-parse", "HEAD").stdout.strip()
    advance(repo, main, new, main_sha)
    try:
        advance(repo, branch, new, fn_sha)
    except LandFail:
        pass  # main has landed; a busy builder worktree keeps its branch where it is
    return new, sum(1 for r in parse_functions(text).values() if r["status"] == "proven")


def regate(repo, ctx, cur, now, timeout):
    """The re-merged tree's gate after main moved from the gated base to `cur`: the whole-tree commands and this
    function's acceptance as always, then only the proven rows the newly landed footprints can affect
    (regate_rows); every proven row when that cannot be bounded. ctx["regate"] says which."""
    gated_base = ctx["main_sha"]
    conf_src = git(repo, "show", f"{cur}:overhaul.toml", check=False).stdout
    try:
        conf = tomllib.loads(conf_src)
    except tomllib.TOMLDecodeError:
        conf = {}
    if conf.get("commands", {}).get("acceptance"):
        ctx["commands"] = load_commands(conf)
    apath = conf.get("paths", {}).get("architecture", "spec/ARCHITECTURE.md")
    sel = regate_rows(repo, gated_base, cur, ctx["fpath"], apath, now, ctx["fid"])
    total = sum(1 for r, row in now.items() if row["status"] == "proven" and r != ctx["fid"])
    if sel is None:
        ctx["regate"] = f"full regate: {total} proven rows"
        gate_tree(ctx, now, (), timeout)
        return
    keep, newly, mods = sel
    ctx["regate"] = (f"partial regate: {len(keep)} of {total} proven rows (landed modules: "
                     f"{', '.join(sorted(mods)) or 'none'})")
    gate_tree(ctx, now, sorted(newly), timeout, only=keep, near_add=mods)


def gate_merged(wt, rows, commands, out_path, timeout):
    """Whole-tree commands, then every proven row's acceptance, on the merged tree in wt."""
    for name, cmd in gate_cmds(commands):
        cmd = fill(cmd)
        code, tail = run(cmd, wt, out_path, timeout)
        if not passes(cmd, code):
            raise LandFail(f"{name} failed (exit {code}) on merged tree: {tail}")
    hit = regression(rows, [r for r, row in rows.items() if row["status"] == "proven"], commands, wt, out_path,
                     timeout)
    if hit:
        raise LandFail(hit[2])


def land_polish(repo, fid, main, timeout):
    """Land a polish fix: no FUNCTIONS row, no red-before; merge, gate, keep every proven row green, fast-forward."""
    ovh = os.path.join(repo, ".overhaul")
    wt, branch = os.path.join(ovh, "wt", "land"), f"fn/{fid}"
    main_sha = git(repo, "rev-parse", "--verify", f"refs/heads/{main}^{{commit}}").stdout.strip()
    fn = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", check=False)
    if fn.returncode != 0:
        raise LandFail(f"no branch {branch}")
    conf = tomllib.loads(git(repo, "show", f"{main_sha}:overhaul.toml").stdout)
    commands = load_commands(conf)
    fpath = conf.get("paths", {}).get("functions", "spec/FUNCTIONS.md")
    if not commands.get("acceptance"):
        raise LandFail("overhaul.toml has no [commands] acceptance")
    touched = git(repo, "diff", "--name-only", f"{main_sha}...{fn.stdout.strip()}").stdout.splitlines()
    bad = [p for p in touched if p.startswith(PLAN_PATHS) or p == fpath]
    if bad:
        raise LandFail(f"{branch} edits plan files: {', '.join(bad[:5])}")
    check_anchors(repo, main_sha, fn.stdout.strip(), branch, fpath)
    if not touched:
        raise LandFail(f"{branch} changes nothing")
    prepare_worktree(repo, wt)
    git(wt, "checkout", "-q", "--detach", fn.stdout.strip())
    merged = git(wt, "merge", "-q", "--no-edit", "-m", f"polish {fid}: merge {main} into {branch}", main_sha,
                 check=False)
    if merged.returncode != 0:
        git(wt, "merge", "--abort", check=False)
        raise LandFail(f"merge conflict merging {main} into {branch}")
    gate_merged(wt, parse_functions(git(repo, "show", f"{main_sha}:{fpath}").stdout), commands,
                os.path.join(ovh, "land.out"), timeout)
    commit_index(wt, arch_path(conf), f"polish {fid}: regenerate INDEX")
    new = git(wt, "rev-parse", "HEAD").stdout.strip()
    advance(repo, main, new, main_sha)
    return new


def log_line(repo, fid, event, detail, start):
    with open(os.path.join(repo, ".overhaul", "log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": now_iso(), "fn": fid, "event": event, "detail": detail,
                            "seconds": round(time.monotonic() - start, 1), "tokens": None}) + "\n")


def main_polish(repo, fid, main, timeout):
    """--polish under the lock: one log line, no state.json entry."""
    start = time.monotonic()
    try:
        sha = land_polish(repo, fid, main, timeout)
        ok, detail = True, f"{main} -> {sha[:12]}"
    except LandFail as e:
        ok, detail = False, str(e)
    except Exception as e:  # noqa: BLE001 - any crash is one reason line, main untouched
        ok, detail = False, f"land error: {type(e).__name__}: {e}"
    log_line(repo, fid, "polish_land_ok" if ok else "polish_land_fail", detail, start)
    print(f"landed {fid}: {detail}" if ok else detail)
    return 0 if ok else 1


# ---- state and log --------------------------------------------------------------------------------------------------

def read_state(path):
    if not os.path.exists(path):
        return {"cycle": 0, "functions": {}, "proven_history": []}
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("cycle", 0)
    state.setdefault("functions", {})
    state.setdefault("proven_history", [])
    return state


def write_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def state_path(repo):
    src = git(repo, "show", "HEAD:overhaul.toml", check=False).stdout
    try:
        rel = tomllib.loads(src).get("paths", {}).get("state", ".overhaul/state.json") if src else None
    except tomllib.TOMLDecodeError:
        rel = None
    return os.path.join(repo, rel or ".overhaul/state.json")


def wait_stack(repo, main, fpath, stack, log_at, timeout):
    """Wait, without the lock, until every stacked function is proven on main or has a land_fail logged since
    byte `log_at` of log.jsonl. commit_land checks the outcome; this only avoids taking the lock too early."""
    end, log = time.monotonic() + timeout, os.path.join(repo, ".overhaul", "log.jsonl")
    while stack and time.monotonic() < end:
        rows = parse_functions(git(repo, "show", f"{main}:{fpath}", check=False).stdout)
        failed = set()
        try:
            with open(log, "rb") as f:
                f.seek(log_at)
                for line in f.read().decode("utf-8", errors="replace").splitlines():
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("event") in ("land_fail", "land_timeout"):
                        failed.add(rec.get("fn"))
        except OSError:
            pass
        if all(s in failed or rows.get(s, {}).get("status") == "proven" for s in stack):
            return
        time.sleep(0.5)


def target_dir(repo, given, stacked_fid=""):
    """The cargo target dir this land reuses across lands: never shared with a builder slot or another land that
    runs at the same time, so a land worktree does not cold-build the workspace."""
    path = given or os.path.join(repo, ".overhaul", "target", f"land-{stacked_fid}" if stacked_fid else "land")
    return os.path.abspath(path if os.path.isabs(path) else os.path.join(repo, path))


def proc_mem_gb(repo):
    """[limits] proc_mem_gb of the working tree's overhaul.toml (default 12; 0 = no cap)."""
    try:
        with open(os.path.join(repo, "overhaul.toml"), "rb") as f:
            lim = tomllib.load(f).get("limits") or {}
    except (OSError, tomllib.TOMLDecodeError):
        lim = {}
    try:
        return float(lim.get("proc_mem_gb", 12) or 0)
    except (TypeError, ValueError):
        return 12.0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--fn", required=True, dest="fid")
    ap.add_argument("--main", default="main")
    ap.add_argument("--wait", type=float, default=30.0, help="seconds to wait for .overhaul/lock")
    ap.add_argument("--timeout", type=float, default=1800.0, help="seconds per command")
    ap.add_argument("--polish", action="store_true", help="land a polish fix branch (no FUNCTIONS row)")
    ap.add_argument("--stack", default="", help="ids of in-flight lands (disjoint footprints) to gate on top of; "
                    "the gate runs without the lock, which is taken only to commit")
    ap.add_argument("--target-dir", default="", help="CARGO_TARGET_DIR for every command (default "
                    ".overhaul/target/land, or land-<ID> when stacked); drive.py passes one per concurrent land")
    ap.add_argument("--parallel", action="store_true", help="gate on main outside the lock in its own worktree, "
                    "take the lock only to commit; if main moved meanwhile, re-merge and rerun only the tests the "
                    "newly landed footprints affect (drive.py runs up to [harness] land_parallel of these)")
    a = ap.parse_args(argv)
    repo = os.path.abspath(a.repo)
    stack = [x.strip() for x in a.stack.split(",") if x.strip()]
    priority.set_self(priority.NORMAL)  # a land outranks the BELOW_NORMAL builders even if started from one
    own = (bool(stack) or a.parallel) and not a.polish
    os.environ["CARGO_TARGET_DIR"] = target_dir(repo, a.target_dir, a.fid if own else "")
    if not os.environ.get(memcap.ENV):  # run by hand, not under drive.py: take the same [limits] proc_mem_gb cap
        memcap.cap_self(proc_mem_gb(repo))
    start_budget(0 if a.polish else land_timeout_min(main_conf(repo, a.main)))
    if own:
        return main_stacked(repo, a, stack)

    if not acquire_lock(repo, a.wait):
        try:
            with open(lock_path(repo), encoding="utf-8") as f:
                holder = f.read().strip()
        except OSError:
            holder = "?"
        print(f"lock held: .overhaul/lock ({holder})")
        return 1
    if a.polish:
        try:
            return main_polish(repo, a.fid, a.main, a.timeout)
        finally:
            release_lock(repo)
    return finish(repo, a, time.monotonic(), lambda: land(repo, a.fid, a.main, a.timeout))


def main_conf(repo, main):
    """overhaul.toml on main, parsed ({} when missing or unreadable)."""
    src = git(repo, "show", f"{main}:overhaul.toml", check=False).stdout
    try:
        return tomllib.loads(src) if src else {}
    except tomllib.TOMLDecodeError:
        return {}


def functions_path(repo, main):
    return main_conf(repo, main).get("paths", {}).get("functions", "spec/FUNCTIONS.md")


def main_stacked(repo, a, stack):
    """--stack or --parallel: gate outside the lock (on main + the stacked branches, or on main alone), wait for
    the stacked lands, then commit under the lock; a base that moved meanwhile is re-merged and re-gated
    (commit_land, regate). If a stacked gate fails, a stacked branch may be the cause: once the stack is done it
    lands alone, gated under the lock on main as it is then (a plain land). A parallel gate's failure is the
    land's failure."""
    start, log = time.monotonic(), os.path.join(repo, ".overhaul", "log.jsonl")
    log_at = os.path.getsize(log) if os.path.exists(log) else 0
    failed = None
    try:
        ctx = gate_branch(repo, a.fid, a.main, a.timeout, stack, own=True)
    except LandTimeout as e:
        ctx, failed = None, e
    except LandFail as e:  # stacked: retried once as a plain land below
        ctx, failed = None, (None if stack else e)
    except Exception as e:  # noqa: BLE001 - a crash: retried once as a plain land when stacked
        ctx, failed = None, (None if stack else LandFail(f"land error: {type(e).__name__}: {e}"))
    t0 = time.monotonic()
    wait_stack(repo, a.main, functions_path(repo, a.main), stack if ctx is None else ctx["stacked"], log_at,
               a.timeout)
    if not acquire_lock(repo, a.timeout):
        print("lock held: .overhaul/lock")
        return 1
    pause_budget(time.monotonic() - t0)

    def step():
        if failed is not None:
            raise failed
        if ctx is None:
            sha, proven = land(repo, a.fid, a.main, a.timeout)
            return sha, proven, " (stacked gate failed; gated alone on the new main)"
        sha, proven = commit_land(repo, ctx, a.main, a.timeout)
        how = (f"regated on the new main, {ctx.get('regate', '')}" if ctx.get("regated") else "no second gate")
        if not stack:
            return sha, proven, f" (parallel, {how})"
        return sha, proven, f" (stacked on {', '.join(ctx['stacked']) or 'nothing'}, {how})"
    return finish(repo, a, start, step)


def finish(repo, a, start, step):
    """Run `step` (the land) under the held lock, record state.json and log.jsonl, then release the lock."""
    try:
        spath = state_path(repo)
        try:
            state = read_state(spath)
        except (OSError, ValueError) as e:
            print(f"state.json unreadable: {e}")
            return 1
        event = "land_fail"
        try:
            sha, proven, *note = step()
            ok, detail = True, f"{a.main} -> {sha[:12]}" + "".join(note)
        except LandTimeout as e:
            ok, detail, event = False, str(e), "land_timeout"
        except LandFail as e:
            ok, detail = False, str(e)
        except Exception as e:  # noqa: BLE001 - any crash is one reason line, main untouched
            ok, detail = False, f"land error: {type(e).__name__}: {e}"
        entry = state["functions"].setdefault(a.fid, {"status": "planned", "attempts": 0})
        entry.setdefault("attempts", 0)
        entry.update(branch=f"fn/{a.fid}", updated=now_iso(), last_reason="" if ok else detail)
        if ok:
            entry["status"] = "proven"
            state["proven_history"].append([now_iso(), proven])
        write_state(spath, state)
        line = {"t": now_iso(), "fn": a.fid, "event": "land_ok" if ok else event, "detail": detail,
                "seconds": round(time.monotonic() - start, 1), "tokens": None}
        if event == "land_timeout":
            line["slowest"], secs = slowest()
            line["slowest_s"] = round(secs, 1)
        with open(os.path.join(repo, ".overhaul", "log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        print(f"landed {a.fid}: {detail}" if ok else detail)
        return 0 if ok else 1
    finally:
        release_lock(repo)


if __name__ == "__main__":
    sys.exit(main())
