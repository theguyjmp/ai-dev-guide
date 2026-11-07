"""Land one function's branch on main (overhaul v2). Deterministic, no LLM, Python stdlib only.

    py -3 land.py --repo R --fn ID [--main main] [--wait 30] [--timeout 1800]

Sequential under the lock file R/.overhaul/lock (created with O_EXCL, removed on exit; drive.py takes the same lock to
write state.json and can import acquire_lock/release_lock from here). All work happens in the worktree
R/.overhaul/wt/land, never in R's own working tree.

  1. The branch fn/<ID> must not edit plan files (spec/, tests/acceptance/, overhaul.toml): those change only in a plan
     commit on main.
  2. RED BEFORE: the function's acceptance must FAIL on main. If it passes: `acceptance already green before change`.
  3. Merge main into fn/<ID> (no force, no rewrite).
  4. The whole-tree commands test, lint, dup, cycles and entry_lint from overhaul.toml [commands] must pass on the
     merged tree. A pytest exit of 5 (no tests collected) counts as pass.
  5. GREEN AFTER: the function's acceptance passes on the merged tree, and every row already `proven` still passes.
  6. One commit sets the row's status cell to `proven` in FUNCTIONS.md; main fast-forwards to it (and fn/<ID> too).

FUNCTIONS.md rows: | id | phase | module | deps | entry | acceptance | status |, acceptance =
`tests/acceptance/<f>.py::<test>` items separated by `;`. The acceptance command template takes {test} (the test name),
{file} (the test file) or {node} (file::test); `<skill>` in any command is this skill's directory.

Writes the untracked R/.overhaul/state.json (functions[ID]: status proven on success, last_reason on failure; attempts
are drive.py's) and appends one line to R/.overhaul/log.jsonl. Any failure prints one reason line, exits 1 and leaves
main untouched. The failing command's full output stays in R/.overhaul/land.out.
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

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHOLE_TREE = ("test", "lint", "dup", "cycles", "entry_lint")
PLAN_PATHS = ("spec/", "tests/acceptance/", "overhaul.toml")
GREEN_BEFORE = "acceptance already green before change"


class LandFail(Exception):
    """One reason line; the land stops and main stays untouched."""


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def git(cwd, *args, check=True):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        msg = (p.stderr.strip() or p.stdout.strip()).splitlines()
        raise LandFail(f"git {args[0]} failed: {msg[-1] if msg else 'exit %d' % p.returncode}")
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
    cols = {"id": 0, "acceptance": 5, "status": 6}
    rows = {}
    for n, line in enumerate(text.splitlines(keepends=True)):
        parts = _split(line)
        if not parts:
            continue
        cells = [_clean(c) for c in parts[2]]
        low = [c.lower() for c in cells]
        if {"id", "acceptance", "status"} <= set(low):
            cols = {name: low.index(name) for name in cols}
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c) or len(cells) <= max(cols.values()):
            continue
        fid = cells[cols["id"]]
        if not fid:
            continue
        nodes = [x.strip().strip("`").strip() for x in cells[cols["acceptance"]].split(";")]
        rows[fid] = {"acceptance": [x for x in nodes if x and x != "-"], "status": cells[cols["status"]].lower(),
                     "line": n, "status_col": cols["status"]}
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
        if not any(p in template for p in ("{test}", "{file}", "{node}")):
            cmds.append((node, f"{fill(template)} {node}"))
        else:
            cmds.append((node, fill(template, test=name or file, file=file, node=node)))
    return cmds


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
        git(wt, "merge", "--ff-only", "-q", new)
    else:
        git(repo, "update-ref", f"refs/heads/{branch}", new, old)


def land(repo, fid, main, timeout):
    ovh = os.path.join(repo, ".overhaul")
    out_path = os.path.join(ovh, "land.out")
    wt = os.path.join(ovh, "wt", "land")
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

    prepare_worktree(repo, wt)

    # RED BEFORE, on main's tree
    git(wt, "checkout", "-q", "--detach", main_sha)
    if all(run(cmd, wt, out_path, timeout)[0] == 0 for _, cmd in mine):
        raise LandFail(GREEN_BEFORE)

    # Merge main into the branch
    git(wt, "checkout", "-q", "--detach", fn_sha)
    merged = git(wt, "merge", "-q", "--no-edit", "-m", f"land {fid}: merge {main} into {branch}", main_sha, check=False)
    if merged.returncode != 0:
        git(wt, "merge", "--abort", check=False)
        raise LandFail(f"merge conflict merging {main} into {branch}")

    for name in WHOLE_TREE:
        cmd = commands.get(name)
        if not cmd:
            continue
        cmd = fill(cmd)
        code, tail = run(cmd, wt, out_path, timeout)
        if not passes(cmd, code):
            raise LandFail(f"{name} failed (exit {code}) on merged tree: {tail}")

    # GREEN AFTER, then no regression of proven rows
    for node, cmd in mine:
        code, tail = run(cmd, wt, out_path, timeout)
        if code != 0:
            raise LandFail(f"acceptance still red after merge: {node} (exit {code}): {tail}")
    for rid, row in rows.items():
        if row["status"] != "proven" or rid == fid:
            continue
        for node, cmd in acceptance_cmds(commands["acceptance"], row["acceptance"]):
            code, tail = run(cmd, wt, out_path, timeout)
            if code != 0:
                raise LandFail(f"regression: proven {rid} {node} fails (exit {code}): {tail}")

    # The land commit: status proven, then main and the branch fast-forward to it
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--fn", required=True, dest="fid")
    ap.add_argument("--main", default="main")
    ap.add_argument("--wait", type=float, default=30.0, help="seconds to wait for .overhaul/lock")
    ap.add_argument("--timeout", type=float, default=1800.0, help="seconds per command")
    a = ap.parse_args(argv)
    repo = os.path.abspath(a.repo)

    if not acquire_lock(repo, a.wait):
        try:
            with open(lock_path(repo), encoding="utf-8") as f:
                holder = f.read().strip()
        except OSError:
            holder = "?"
        print(f"lock held: .overhaul/lock ({holder})")
        return 1
    start = time.monotonic()
    try:
        spath = state_path(repo)
        try:
            state = read_state(spath)
        except (OSError, ValueError) as e:
            print(f"state.json unreadable: {e}")
            return 1
        try:
            sha, proven = land(repo, a.fid, a.main, a.timeout)
            ok, detail = True, f"{a.main} -> {sha[:12]}"
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
