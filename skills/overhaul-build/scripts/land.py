"""Land one function's branch on main (overhaul v2). Deterministic, no LLM, Python stdlib only.

    py -3 land.py --repo R --fn ID [--main main] [--wait 30] [--timeout 1800] [--polish] [--target-dir D]

Sequential under the lock file R/.overhaul/lock (created with O_EXCL, removed on exit; drive.py takes the same lock to
write state.json and can import acquire_lock/release_lock from here). All work happens in the worktree
R/.overhaul/wt/land, never in R's own working tree.

  1. The branch fn/<ID> must not edit plan files (spec/, tests/acceptance/, overhaul.toml): those change only in a plan
     commit on main. Planned tests are anchors (anchors.py): a test file that was in the plan commit may not be
     modified, renamed or deleted; new test files are fine.
  2. RED BEFORE: the function's acceptance must FAIL on main. If it passes: `acceptance already green before change`.
  3. Merge main into fn/<ID> (no force, no rewrite).
  4. The whole-tree commands test, lint, dup, literals, shape, cycles and entry_lint from overhaul.toml [commands] must
     pass on the merged tree. A pytest exit of 5 (no tests collected) counts as pass.
  5. GREEN AFTER: the function's acceptance passes on the merged tree, and every row already `proven` still passes.
     Tests run batched: one command per test target (cargo `--test <stem>` with every exact name, one pytest call),
     failures mapped back to test ids; the first failing id then reruns alone so land.out holds its own output.
     A whole-tree `test` failure or a regression outside the branch's footprint reruns alone on clean main up to 3
     times; if it fails there too the reason starts `main flake: test=.. path=.. module=..` and drive.py sends it
     to a fix in the owning module instead of failing the function.
  6. One commit sets the row's status cell to `proven` in FUNCTIONS.md; main fast-forwards to it (and fn/<ID> too).

FUNCTIONS.md rows: | id | phase | module | deps | entry | acceptance | status |, acceptance =
`tests/acceptance/<f>.py::<test>` (or `<f>.rs`) items separated by `;`. The acceptance command template takes {test}
(the test name), {file} (the test file), {stem} (the file name without suffix: a Rust [[test]] target) or {node}
(file::test); `<skill>` in any command is this skill's directory.

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
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import PurePosixPath

import anchors  # beside this script
os.environ.setdefault("PROPTEST_DISABLE_FAILURE_PERSISTENCE", "1")  # no .proptest-regressions files (vc1)
os.environ.setdefault("PROPTEST_RNG_SEED", "0")  # fixed seed: a property green at land stays green on main (vc1)

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHOLE_TREE = ("test", "lint", "dup", "literals", "shape", "cycles", "entry_lint")
PLAN_PATHS = ("spec/", "tests/acceptance/", "overhaul.toml")
GREEN_BEFORE = "acceptance already green before change"


class LandFail(Exception):
    """One reason line; the land stops and main stays untouched."""


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

def lock_path(repo):
    return os.path.join(repo, ".overhaul", "lock")


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


def acquire_lock(repo, wait=30.0, owner="land.py"):
    """Create .overhaul/lock exclusively, waiting up to `wait` seconds. Returns False if another holder keeps it.
    A lock whose holder is dead is broken."""
    path = lock_path(repo)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.monotonic() + max(wait, 0)
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
            time.sleep(0.2)
            continue
        with os.fdopen(fd, "w") as f:
            f.write(f"{owner} pid {os.getpid()} since {now_iso()}\n")
        return True


def release_lock(repo):
    try:
        os.remove(lock_path(repo))
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


def run(cmd, cwd, out_path, timeout):
    """Run one shell command in cwd; output goes to out_path. Returns (exit code, last output line)."""
    with open(out_path, "wb") as out:
        out.write(f"$ {cmd}\n".encode())
        out.flush()
        proc = subprocess.Popen(cmd, cwd=cwd, shell=True, stdout=out, stderr=subprocess.STDOUT)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return -1, f"timeout after {timeout}s"
    with open(out_path, "rb") as f:
        lines = [x.strip() for x in f.read().decode("utf-8", "replace").splitlines() if x.strip()]
    tail = lines[-1] if len(lines) > 1 else ""
    return code, tail[:200]


def acceptance_cmds(template, nodes):
    cmds = []
    for node in nodes:
        file, _, name = node.partition("::")
        if not any(p in template for p in ("{test}", "{file}", "{node}", "{stem}")):
            cmds.append((node, f"{fill(template)} {node}"))
        else:
            cmds.append((node, fill(template, test=name or file, file=file, node=node, stem=PurePosixPath(file).stem)))
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
    one found in a batch reruns by itself so out_path holds its own output (the batch verdict stands)."""
    def runner(cmd):
        code, tail = run(cmd, wt, out_path, timeout)
        return code, tail, read_out(out_path)
    res = batch_results(template, nodes, runner)
    bad = [(n, res[n][0], res[n][1]) for n in dict.fromkeys(nodes) if res[n][0] != 0]
    if bad and alone and not res[bad[0][0]][2]:
        code, tail = run(acceptance_cmds(template, [bad[0][0]])[0][1], wt, out_path, timeout)
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


def gate_branch(repo, fid, main, timeout, stack=()):
    """Steps 1 to 5 on main as it is now; returns the context commit_land needs. With `stack` (functions whose lands
    are in flight, footprints disjoint from this one) the gate runs on main + their branches + this branch, so this
    land can commit right after theirs without a second gate. Runs without the lock when stacked."""
    ovh = os.path.join(repo, ".overhaul")
    out_path = os.path.join(ovh, f"land-{fid}.out" if stack else "land.out")
    wt = os.path.join(ovh, "wt", f"land-{fid}" if stack else "land")
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
    commands = conf.get("commands", {})
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
    mine = acceptance_cmds(commands["acceptance"], rows[fid]["acceptance"])

    touched = git(repo, "diff", "--name-only", f"{main_sha}...{fn_sha}").stdout.splitlines()
    bad = [p for p in touched if p.startswith(PLAN_PATHS) or p == fpath]
    if bad:
        raise LandFail(f"{branch} edits plan files: {', '.join(bad[:5])}")
    check_anchors(repo, main_sha, fn_sha, branch, fpath)

    prepare_worktree(repo, wt)

    # RED BEFORE, on main's tree
    git(wt, "checkout", "-q", "--detach", main_sha)
    if not red_nodes(commands["acceptance"], rows[fid]["acceptance"], wt, out_path, timeout, alone=False):
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
               commands=commands, fpath=fpath, rows=rows, mine=mine, stacked=stacked, touched=touched)
    gate_tree(ctx, rows, stacked, timeout)
    ctx["gated"] = git(wt, "rev-parse", "HEAD").stdout.strip()
    return ctx


def gate_tree(ctx, rows, extra, timeout):
    """Steps 4 and 5 on the tree checked out in ctx's worktree: whole-tree commands, this function's acceptance,
    then every proven row (and every stacked row in `extra`) still passes."""
    wt, out_path, commands, fid = ctx["wt"], ctx["out_path"], ctx["commands"], ctx["fid"]
    for name in WHOLE_TREE:
        cmd = commands.get(name)
        if not cmd:
            continue
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
    bad = red_nodes(commands["acceptance"], [n for n, _ in ctx["mine"]], wt, out_path, timeout)
    if bad:
        node, code, tail = bad[0]
        raise LandFail(f"acceptance still red after merge: {node} (exit {code}): {tail}")
    keep = [rid for rid, row in rows.items() if (row["status"] == "proven" or rid in extra) and rid != fid]
    hit = regression(rows, keep, commands, wt, out_path, timeout)
    if hit:
        rid, node, reason = hit
        row = rows[rid]
        if row.get("module") != rows[fid].get("module"):
            flake = flake_on_main(ctx, node, node.partition("::")[0], row.get("module", ""),
                                  acceptance_cmds(commands["acceptance"], [node])[0][1], timeout)
            if flake:
                raise LandFail(f"{flake}; on the merged tree: {reason}")
        raise LandFail(reason)


def regression(rows, ids, commands, wt, out_path, timeout):
    """(row id, node, reason) of the first of `ids` whose acceptance fails in wt, or None. All their tests run
    batched by test target, not one command per test."""
    owner = {}
    for rid in ids:
        for node in rows[rid]["acceptance"]:
            owner.setdefault(node, rid)
    bad = red_nodes(commands["acceptance"], list(owner), wt, out_path, timeout)
    if not bad:
        return None
    node, code, tail = bad[0]
    return owner[node], node, f"regression: proven {owner[node]} {node} fails (exit {code}): {tail}"


# ---- main flakes: a test outside the function's footprint that fails on clean main too ------------------------------

MAIN_FLAKE = "main flake:"
FLAKE_RUNS = 3
RUNNING = re.compile(r"^\s*Running (?:unittests )?(\S+) \((?:.*[\\/])?([A-Za-z0-9_]+)-[0-9a-f]+(?:\.exe)?\)", re.M)


def flake_on_main(ctx, test, path, module, cmd, timeout):
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
        code, _ = run(cmd, wt, out, timeout)
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
        same = stack_ok and git(wt, "diff", "--quiet", ctx["gated"], "HEAD", "--", ".",
                                f":(exclude){fpath}", check=False).returncode == 0
        ctx["regated"] = not same
        if not same:  # main moved by something the gate did not see: gate the real tree, under the lock
            ctx["main_now"] = cur
            gate_tree(ctx, now, (), timeout)
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
    git(wt, "commit", "-q", "-m", f"land {fid}: proven")
    new = git(wt, "rev-parse", "HEAD").stdout.strip()
    advance(repo, main, new, main_sha)
    try:
        advance(repo, branch, new, fn_sha)
    except LandFail:
        pass  # main has landed; a busy builder worktree keeps its branch where it is
    return new, sum(1 for r in parse_functions(text).values() if r["status"] == "proven")


def gate_merged(wt, rows, commands, out_path, timeout):
    """Whole-tree commands, then every proven row's acceptance, on the merged tree in wt."""
    for name in WHOLE_TREE:
        cmd = commands.get(name)
        if not cmd:
            continue
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
    commands = conf.get("commands", {})
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
                    if isinstance(rec, dict) and rec.get("event") == "land_fail":
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
    a = ap.parse_args(argv)
    repo = os.path.abspath(a.repo)
    stack = [x.strip() for x in a.stack.split(",") if x.strip()]
    os.environ["CARGO_TARGET_DIR"] = target_dir(repo, a.target_dir, a.fid if stack and not a.polish else "")
    if stack and not a.polish:
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


def functions_path(repo, main):
    src = git(repo, "show", f"{main}:overhaul.toml", check=False).stdout
    try:
        return tomllib.loads(src).get("paths", {}).get("functions", "spec/FUNCTIONS.md")
    except tomllib.TOMLDecodeError:
        return "spec/FUNCTIONS.md"


def main_stacked(repo, a, stack):
    """--stack: gate outside the lock on main + the stacked branches, wait for their lands, then commit under it.
    If that gate fails, a stacked branch may be the cause: once the stack is done it lands alone, gated under the
    lock on main as it is then (a plain land)."""
    start, log = time.monotonic(), os.path.join(repo, ".overhaul", "log.jsonl")
    log_at = os.path.getsize(log) if os.path.exists(log) else 0
    try:
        ctx = gate_branch(repo, a.fid, a.main, a.timeout, stack)
    except Exception:  # noqa: BLE001 - LandFail or a crash: retried once as a plain land below
        ctx = None
    wait_stack(repo, a.main, functions_path(repo, a.main), stack if ctx is None else ctx["stacked"], log_at,
               a.timeout)
    if not acquire_lock(repo, a.timeout):
        print("lock held: .overhaul/lock")
        return 1

    def step():
        if ctx is None:
            sha, proven = land(repo, a.fid, a.main, a.timeout)
            return sha, proven, " (stacked gate failed; gated alone on the new main)"
        sha, proven = commit_land(repo, ctx, a.main, a.timeout)
        how = "regated on the new main" if ctx.get("regated") else "no second gate"
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
        try:
            sha, proven, *note = step()
            ok, detail = True, f"{a.main} -> {sha[:12]}" + "".join(note)
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
        with open(os.path.join(repo, ".overhaul", "log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": now_iso(), "fn": a.fid, "event": "land_ok" if ok else "land_fail",
                                "detail": detail, "seconds": round(time.monotonic() - start, 1), "tokens": None})
                    + "\n")
        print(f"landed {a.fid}: {detail}" if ok else detail)
        return 0 if ok else 1
    finally:
        release_lock(repo)


if __name__ == "__main__":
    sys.exit(main())
