"""fleet-train harness v2: claim / submit / release for builder agents, and a batch land train (merge all, gate
once, bisect on red). No LLM in here.

  py -3 harness.py frontier            # JSON summary of buildable rows
  py -3 harness.py claim --slot N [--wait S]
  py -3 harness.py submit --slot N --row R
  py -3 harness.py release --slot N --row R --reason "..."
  py -3 harness.py train [--loop]      # land queued branches
  py -3 harness.py status              # one-line rate
  py -3 harness.py note "<text>"       # append a line to <HARNESS_HOME>/notes/log/YYYY-MM.md
"""
from __future__ import annotations

import argparse, json, os, re, shutil, subprocess, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import SimpleQueue

import cargo_failures as cf, desk, quarantine, rules_gate, rules_langs

REPO = Path(os.environ.get("HARNESS_REPO", "."))
HOME = Path(os.environ.get("HARNESS_HOME", str(Path(__file__).parent)))
ST = HOME / "state"
WT = Path(os.environ.get("HARNESS_WT", str(REPO.parent / "vh")))  # per-slot worktrees, fixed paths -> warm incremental builds
TARGETS = REPO / ".overhaul" / "target"
SPEC = "spec/FUNCTIONS.md"
SPEC_REF = os.environ.get("HARNESS_SPEC_REF", "main")  # spec ref (check a branch before it lands)
CLAIM_TTL = 100 * 60
ST.mkdir(parents=True, exist_ok=True)
WT.mkdir(parents=True, exist_ok=True)


def now() -> float:
    return time.time()


def iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console windows for children


def kill_tree(pid: int, sig: int = 9) -> None:
    """Kill pid and its tree: taskkill /T on Windows, else its process group (sh() children lead one) or pid."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
        return
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):  # default: already gone
        pass


def sh(cmd, cwd=REPO, check=True, env=None, timeout=None, stdin=None, low=False, on_start=None) -> subprocess.CompletedProcess:
    """Run cmd; a timeout kills its tree. low: below-normal priority; on_start(pid) once it runs."""
    flags = NO_WINDOW | (getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) if low else 0)
    p = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", env=env, shell=isinstance(cmd, str),
                         stdin=subprocess.PIPE if stdin is not None else None, creationflags=flags,
                         start_new_session=os.name != "nt")
    if low and os.name != "nt":
        try:
            os.setpriority(os.PRIO_PGRP, p.pid, 10)
        except OSError:
            pass
    if on_start:
        on_start(p.pid)
    try:
        out, err = p.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        # kill the tree: a grandchild (app.exe under a test) holds the pipes open
        kill_tree(p.pid)
        try:
            out, err = p.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
    r = subprocess.CompletedProcess(cmd, p.returncode, out, err)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd} -> {r.returncode}\n{(r.stdout + r.stderr)[-3000:]}")
    return r


def git(*a, cwd=REPO, check=True):
    return sh(["git", *a], cwd=cwd, check=check)


def lock_holder(name: str) -> str:
    try:
        return (ST / f"{name}.lock").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def release_lock(name: str) -> None:
    """Drop name's lock only if this process holds it; retry WinError 5, then leave it to lock()'s stale rule."""
    p = ST / f"{name}.lock"
    for _ in range(40):
        try:
            if lock_holder(name) == str(os.getpid()):
                p.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.25)
    event("lock_release_failed", lock=name)


LOCK_PROBE_SECS = 5.0  # a waiter asks whether the holder still runs at most this often


def pid_state(pid: int) -> bool | None:
    """The one process probe: True runs, False gone (positive evidence only), None unknown."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
        return True
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True,
                           timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode or not r.stdout.strip():
        return None
    return f'"{pid}"' in r.stdout


def pid_alive(pid: int) -> bool:
    return pid_state(pid) is True


def _break_dead(p: Path, stale: float, probe: bool = True) -> bool:
    """Move a dead lock aside: its holder pid is gone, or it is older than `stale` (hung). It goes back if it changed
    since read, so two waiters never both break it. True when p is gone."""
    try:
        seen, age = p.read_text(encoding="utf-8").strip(), now() - p.stat().st_mtime
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if age <= stale and not (probe and seen.isdigit() and pid_state(int(seen)) is False):
        return False
    aside = p.with_name(f"{p.name}.dead-{os.getpid()}-{threading.get_ident()}")
    try:
        os.replace(p, aside)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        if aside.read_text(encoding="utf-8").strip() != seen:  # replaced since read: a live holder's lock
            os.rename(aside, p)
            return False
    except OSError:
        pass
    aside.unlink(missing_ok=True)
    return True


@contextmanager
def lock(name: str, wait: float = 600):
    p = ST / f"{name}.lock"
    t0, probed = now(), None
    while True:
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except (FileExistsError, PermissionError):  # Windows: the holder's unlink is pending
            probe = probed is None or now() - probed >= LOCK_PROBE_SECS
            probed = now() if probe else probed
            if _break_dead(p, 120 if name == "state" else 3600, probe):  # stale (state sections are short)
                continue
            if now() - t0 > wait:
                raise TimeoutError(f"{name} lock held by pid {lock_holder(name) or '?'} for over {wait:.0f}s")
            time.sleep(0.5)
    try:
        yield
    finally:
        release_lock(name)


def load(name, default):
    p = ST / name
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return default


def save(name, obj):
    p = ST / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    os.replace(tmp, p)


EVENT_LOCK = threading.Lock()  # pick threads log too


def event(kind, **kw):
    with EVENT_LOCK, open(ST / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": iso(), "ts": now(), "event": kind, **kw}) + "\n")


# ---------------------------------------------------------------- spec
ROW_RE = re.compile(r"^[A-Z]\d")


def parse_spec(text: str) -> dict:
    rows = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(c) < 8 or not ROW_RE.match(c[0]):
            continue
        rows[c[0]] = {"id": c[0], "phase": c[1], "module": c[2], "deps_raw": c[3], "entry": c[4],
                      "acceptance": c[5], "status": c[6], "files": c[7]}
    for r in rows.values():
        r["deps"] = [d for d in re.findall(r"[A-Z]\d{2,4}", r["deps_raw"]) if d in rows and d != r["id"]]
        r["tests"] = [t.strip() for t in r["acceptance"].split(";") if "::" in t]
        r["file_list"] = [f.strip() for f in re.split(r"[;,]", r["files"]) if f.strip()]
    return rows


def owner_tests(rows: dict, changed) -> list:
    """Proven `rows`' tests on `changed` files (a planned row's are red by design)."""
    changed = set(changed)
    return sorted(t for r in rows.values() if r["status"] == "proven" for t in r["tests"]
                  if changed & set(r["file_list"]) or t.split("::")[0] in changed)


def main_spec() -> dict:
    return parse_spec(git("show", f"{SPEC_REF}:{SPEC}").stdout)


def old_state() -> dict:
    p = REPO / ".overhaul" / "state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("functions", {})
    except Exception:
        return {}


def blockers(rows) -> dict:
    """How many open rows each open row (transitively) unblocks."""
    rev = {i: [] for i in rows}
    for r in rows.values():
        for d in r["deps"]:
            rev[d].append(r["id"])
    out = {}
    for i in rows:
        seen, stack = set(), [i]
        while stack:
            for n in rev[stack.pop()]:
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
        out[i] = len(seen)
    return out


MODULE_CAP = {"kernel": 3}
DEFAULT_CAP = 6


def held_rows() -> set:
    """Rows a ruling holds back from builders (state/held-rows.txt)."""
    p = ST / "held-rows.txt"
    return set(p.read_text(encoding="utf-8").split()) if p.exists() else set()


def frontier(rows, claims, queue, attempts):
    busy = {c["row"] for c in claims.values()} | landing_ids(queue) | held_rows()
    # Only claims count against the module cap and file overlap (counting queued rows starved builders); the batch
    # pick handles queued-vs-new overlap.
    building = {c["row"] for c in claims.values()}
    inflight = {}
    busy_files = set()
    for i in building:
        if i in rows:
            inflight[rows[i]["module"]] = inflight.get(rows[i]["module"], 0) + 1
            busy_files |= set(rows[i]["file_list"])
    out = []
    b = blockers(rows)
    pri = set(load("priority.json", []))
    for r in rows.values():
        if r["status"] in ("proven", "deferred") or r["id"] in busy:
            continue
        if r["id"] in pri:
            pass
        elif b[r["id"]] < 8 and inflight.get(r["module"], 0) >= MODULE_CAP.get(r["module"], DEFAULT_CAP):
            continue  # too many of this module in flight: shared files conflict
        if r["id"] not in pri and set(r["file_list"]) & busy_files:
            continue
        queued = {q["row"] for q in queue}
        if all(rows[d]["status"] == "proven" or d in queued for d in r["deps"]):
            out.append(r["id"])
    b = blockers(rows)
    queued = {q["row"] for q in queue}
    stacked = lambda i: sum(1 for d in rows[i]["deps"] if d in queued)
    pri = load("priority.json", [])
    out.sort(key=lambda i: (i not in pri, attempts.get(i, 0) >= 3, stacked(i) > 0, -b[i], attempts.get(i, 0), i))
    return out


# ---------------------------------------------------------------- worktrees
def slot_wt(slot: int) -> Path:
    return WT / f"s{slot}"


def copy_assets(dst: Path, src: Path = None, globs=None) -> int:
    """Copy the profile's `copy_ignored` globs from the main checkout into a worktree; the count."""
    src = Path(src or REPO).resolve()
    n = 0
    for g in (P["copy_ignored"] if globs is None else globs):
        for f in src.glob(g):
            rel = f.relative_to(src)
            if not f.is_file() or ".git" in rel.parts or "node_modules" in rel.parts:
                continue
            t = Path(dst) / rel
            if t.exists() and t.stat().st_size == f.stat().st_size and t.stat().st_mtime >= f.stat().st_mtime:
                continue
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, t)
            n += 1
    return n


def ensure_wt(slot: int, branch: str, base: str):
    p = slot_wt(slot)
    if not (p / ".git").exists():
        git("worktree", "add", "-f", "--detach", str(p), "main")
    git("reset", "-q", "--hard", cwd=p, check=False)
    git("clean", "-qfd", "-e", "target", cwd=p, check=False)
    git("checkout", "-q", "-B", branch, base, cwd=p)
    copy_assets(p)
    return p


def stack_env(tmpl: dict, **kw) -> dict:
    """Resolve a profile env dict: "{targets}/x" -> TARGETS / x, "{repo}/x" -> REPO / x ({slot} etc. filled in)."""
    out = {}
    for k, v in tmpl.items():
        for tok, root in (("{targets}/", TARGETS), ("{repo}/", REPO.resolve())):
            if v.startswith(tok):
                v = str(root / v[len(tok):].format(**kw))
        out[k] = v
    return out


def env_for(slot) -> dict:
    return stack_env(P["slot_env"], slot=slot)


# ---------------------------------------------------------------- commands
def reexec_argv() -> list:
    """The argv a restarted train runs: the project's run_train.py wrapper, else this file."""
    wrap = Path(__file__).with_name("run_train.py")
    if wrap.exists():
        pyw = Path(sys.executable).with_name("pythonw.exe")
        return [str(pyw if pyw.exists() else sys.executable), "-u", str(wrap), "train", "--loop"]
    return [sys.executable, "-u", __file__, "train", "--loop"]


def cmd_frontier(a):
    rows = main_spec()
    claims, queue, attempts = load("claims.json", {}), load("queue.json", []), load("attempts.json", {})
    f = frontier(rows, claims, queue, attempts)
    c = {}
    for r in rows.values():
        c[r["status"]] = c.get(r["status"], 0) + 1
    print(json.dumps({"frontier": len(f), "top": f[:20], "claimed": len(claims), "queued": len(queue), "status": c}))


CLOUD_SLOT0 = int(os.environ.get("HARNESS_CLOUD_SLOT0", "1000"))  # cloud.py slots: expired by `cloud.py collect`, never by CLAIM_TTL


def is_cloud(slot) -> bool:
    return str(slot).isdigit() and int(slot) >= CLOUD_SLOT0


def previous_branch(rid):
    """An earlier attempt's branch still ahead of main (h/<row> or fn/<row>), or None."""
    for cand in (f"h/{rid}", f"fn/{rid}"):
        if git("rev-parse", "-q", "--verify", cand, check=False).returncode == 0:
            ahead = git("rev-list", "--count", f"main..{cand}", check=False).stdout.strip()
            if ahead and ahead != "0":
                return {"branch": cand, "commits_ahead_of_main": int(ahead)}
    return None


def take_row(slot, rid, rows, claims, attempts) -> dict:
    """Record slot's claim of rid (caller holds the state lock); return the claim fields every builder gets."""
    r = rows[rid]
    branch = f"h/{rid}-s{slot}"
    claims[str(slot)] = {"row": rid, "ts": now(), "branch": branch}
    save("claims.json", claims)
    attempts[rid] = attempts.get(rid, 0) + 1
    save("attempts.json", attempts)
    event("claim", slot=slot, row=rid, attempt=attempts[rid])
    return {"row": rid, "branch": branch, "module": r["module"], "entry": r["entry"], "deps": r["deps"],
            "tests": r["tests"], "files": r["file_list"], "attempt": attempts[rid],
            "previous_branch": previous_branch(rid),
            "old_harness_reason": old_state().get(rid, {}).get("last_reason", "")[:600],
            "recent_rejects": load("rejects.json", {}).get(rid, [])[-3:],
            "must_also_before_commit": QUALITY_BRIEF,
            "stack": {"label": P["stack_label"], "env_use": P["env_use"], "done": P["done_brief"], "tail": P["tail_brief"]}}


def cmd_claim(a):
    t0 = now()
    while True:
        with lock("state"):
            rows = main_spec()
            claims, queue, attempts = load("claims.json", {}), load("queue.json", []), load("attempts.json", {})
            for k in [k for k, c in claims.items() if not is_cloud(k) and now() - c["ts"] > CLAIM_TTL]:
                event("claim_expired", **claims.pop(k))
            claims.pop(str(a.slot), None)
            f = [] if (ST / "pause").exists() else frontier(rows, claims, queue, attempts)
            if f:
                info = take_row(a.slot, f[0], rows, claims, attempts)
                break
        if now() - t0 > a.wait:
            print(json.dumps({"row": None, "reason": "frontier empty", "queued": len(load("queue.json", []))}))
            return
        time.sleep(20)
    p = ensure_wt(a.slot, info["branch"], "main")  # outside the lock: worktree checkout is slow
    for q in [q for q in queue if q["row"] in info["deps"]]:  # stacked start: deps still waiting for the train
        if git("merge", "-q", "--no-edit", "-m", f"stack: {q['row']}", q["sha"], cwd=p, check=False).returncode != 0:
            git("merge", "--abort", cwd=p, check=False)
    e = env_for(a.slot)
    print(json.dumps({**info, "worktree": str(p).replace("\\", "/"), "env": e,
                      "env_prefix": " ".join(f"{k}={v}" for k, v in e.items()).replace("\\", "/")}, indent=1))


class QueueRefused(SystemExit):
    """queue_sha / queue_clean refused a sha (replace_refusal); cloud.py catches it."""


def replace_refusal(row, sha, slot, replace=False) -> str:
    """Why `slot` may not queue `sha` over row's queued entry, else "" (caller holds the state lock). Allowed when sha
    contains the queued sha, the same slot queued it, or the lane passes --replace."""
    q = next((q for q in load("queue.json", []) if q["row"] == row), None)
    if replace or not q or q.get("slot") == slot or git("merge-base", "--is-ancestor", q["sha"], sha, check=False).returncode == 0:
        return ""
    return (f"submit: {row} is already queued at {q['sha'][:12]} by slot {q.get('slot')} and {sha[:12]} does not contain it; "
            f"merge h/{row} into your branch and submit again, or pass --replace to drop that packet on purpose")


def queue_sha(slot, row, sha, since, replace=False) -> int:
    """Queue sha as slot's submission of row and drop slot's claim (caller holds the state lock); the queue length.
    QueueRefused when another slot's packet holds the row."""
    if why := replace_refusal(row, sha, slot, replace):
        raise QueueRefused(why)
    git("branch", "-f", f"h/{row}", sha)
    queue = [q for q in load("queue.json", []) if q["row"] != row] + [{"row": row, "sha": sha, "ts": now(), "slot": slot}]
    save("queue.json", queue)
    claims = load("claims.json", {})
    claims.pop(str(slot), None)
    save("claims.json", claims)
    event("submit", slot=slot, row=row, sha=sha, secs=round(now() - since))
    return len(queue)


def cmd_submit(a):
    why = submit_dup(slot_wt(a.slot))  # takes minutes: run before the state lock
    if why:
        sys.exit(why)
    with lock("state"):
        c = load("claims.json", {}).get(str(a.slot))
        if not c or c["row"] != a.row:
            sys.exit(f"slot {a.slot} does not hold {a.row}")
        p = slot_wt(a.slot)
        if git("status", "--porcelain", cwd=p).stdout.strip():
            sys.exit("worktree has uncommitted changes; commit first")
        sha = git("rev-parse", "HEAD", cwd=p).stdout.strip()
        if git("rev-list", "--count", f"main..{sha}").stdout.strip() == "0":
            sys.exit("no commits on top of main")
        print(json.dumps({"ok": True, "queued": queue_sha(a.slot, a.row, sha, c["ts"], replace=a.replace)}))


def cmd_release(a):
    with lock("state"):
        claims = load("claims.json", {})
        c = claims.pop(str(a.slot), None)
        save("claims.json", claims)
        rj = load("rejects.json", {})
        rj.setdefault(a.row, []).append({"t": iso(), "by": "builder", "reason": a.reason[:1500]})
        save("rejects.json", rj)
        event("release", slot=a.slot, row=a.row, reason=a.reason[:300])
        print(json.dumps({"ok": True}))


# ---------------------------------------------------------------- stack profile
# Every stack-specific command and pattern lives here; HARNESS_STACK (else harness.toml `stack`, else rust) picks the
# profile and a harness.toml [profile] table overrides any key. Placeholders: see expand(); "" turns a step off.
QS = os.environ.get("HARNESS_QS", str(Path.home() / ".claude/skills/overhaul-build/scripts"))
JS_EXTS = [".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"]
STACKS = {
    "rust": {
        "src_exts": [".rs"],
        "src_dirs": ["crates", "src"],  # product code: review diff, quality scripts, grep hint
        "size_dirs": ["crates", "src", "tests"],  # S8 size gate
        "test_file_re": r"(^|/)tests/|(^|/)tests\.rs$|_tests\.rs$",  # gets the test-file size cap
        "slot_env": {"CARGO_TARGET_DIR": "{targets}/slot-{slot}", "CARGO_INCREMENTAL": "1"},
        "train_env": {"CARGO_TARGET_DIR": "{targets}/land-0", "CARGO_PROFILE_RELEASE_INCREMENTAL": "true",
                      "PROPTEST_DISABLE_FAILURE_PERSISTENCE": "1"},
        "fmt": "rustfmt",  # batched rustfmt over every workspace target root (fmt_tree)
        "unit": "cargo test -q --workspace --lib --bins --no-fail-fast",
        "unit_one": "cargo test -q --workspace --lib --bins -- --exact {name} --test-threads=1",
        "name_escape": False,  # unit_one gets the raw test name
        "unit_parser": "cargo",  # cargo_failures.parse; a binary that crashed naming no test is kept by selector
        "unit_target": "cargo test -q {target} -- --test-threads=1",
        "lint": "cargo clippy -q --workspace --all-targets -- -D warnings",
        "lint_strict": ("cargo clippy -q --workspace --all-targets -- -D warnings -D clippy::too_many_lines "
                        "-D clippy::unwrap_used -D clippy::cognitive_complexity"),
        "lint_strict_brief": ("cargo clippy -q -p <each crate you touched> --all-targets -- -D warnings "
                              "-D clippy::too_many_lines -D clippy::unwrap_used -D clippy::cognitive_complexity"),
        "copy_ignored": [],  # gitignored globs copied into slot and train trees
        "stack_label": "Rust workspace",
        "env_use": "<env_prefix> cargo ...",
        "done_brief": ("`cargo test -q --test <stem> -- --exact <test_name>` for each test in \"tests\" (tests under "
                       "tests/acceptance/s*.rs may be slow in debug: add --release for those if a debug run takes over "
                       "3 min), then `cargo clippy -q -p <crate> --all-targets -- -D warnings` for each crate you touched"),
        "tail_brief": "pipe cargo output through tail -30 or grep error",
        "qa": ["dup_blocks.py", "dup_literals.py", "shape_check.py", "check_cycles.py"],  # in QS, run over src_dirs
        "accept": "cargo",  # build the test binaries once, run each (run_tests_cargo)
        "check": "cargo check -q --workspace --tests",
        "check_exts": [".rs"],
        "check_hint": "cargo check -q --workspace --tests 2>&1 | grep -E '^error' -A5 | head -40",
        # the pick check's compile of main + one row
        "pick_build": "cargo check -q --profile test --lib --bins --workspace",
        "pick_env": {"CARGO_TARGET_DIR": "{targets}/pick"},
        "pick_err_re": r"^error(\[E\d+\])?: (?!could not compile)",
        "fn_added_re": r"^\+\s*(pub(\([a-z]+\))?\s+)?fn\s+\w+",
        "fn_def_re": r"\bfn\s+{name}\b",
        "review_skip_re": r"/tests/",
        "path_re": r"((?:crates|src|tests)[\\/][\w\\/.-]+\.rs)",  # repo files a failure log names
        "quoted_path_re": r"[\"'`]((?:crates|src|tests)/[\w/.-]+\.rs)[\"'`]",  # repo files a test reads
        "strip_root": False,
        "assert_sides_re": r"^\s*left: (.*)\n\s*right: (.*)$",
        "shrink_cmd": "",  # e.g. "cargo test -q --workspace --lib --bins --no-fail-fast -- only_shrink --nocapture"
        "scope_flag": "--workspace",  # finish.py's crates scope swaps in -p flags
        "pin_dirs": ["tests"],
        "test_file": "cargo test -q --test {stem}",
        "include_re": r"include_(?:str|bytes)!\(\s*\"([^\"]+)\"\s*\)",
        "include_const_re": r"(?:const|static)\s+(\w+)\s*:[^=;]*=\s*include_(?:str|bytes)!\(\s*\"([^\"]+)\"\s*\)",
        # MSVC and cc builds need ProgramFiles(x86), which Git Bash drops
        "require_env": ["ProgramFiles(x86)"] if os.name == "nt" else [],
        # a gate log line meaning a wrong toolchain env, not a bad row (fleet_beat TRAIN-ENV)
        "env_fail_re": r"linker `link\.exe` not found|cannot open input file 'kernel32\.lib'",
        "crate_blame": True,  # unit failures name `-p <CRATE>-x` -> crates/x/
        "owner_fns": True,  # flag local copies of fns core owns (owner_copies)
        "lang": "Rust",
        "owners": "app_core::math, app_core::param, app_core::refusal (+ core::invalid), and the owners in",
        "owner_note": ("(core owns refusals and error builders, geom owns maths; vectors, param parsing, not_found/malformed/"
                       "invalid builders must come from their owner, never a local copy)"),
    },
    "node": {
        "src_exts": JS_EXTS,
        "src_dirs": ["."],
        "size_dirs": [],
        "test_file_re": r"(^|/)(tests?|__tests__)/|[._-](test|spec)\.[mc]?[jt]sx?$",
        "slot_env": {"NODE_PATH": "{repo}/node_modules"},  # worktrees have no node_modules (CommonJS only; ESM ignores it)
        "train_env": {"NODE_PATH": "{repo}/node_modules"},
        "fmt": "",  # prettier on changed files, when the project has it
        "unit": "node --test --test-reporter=tap",
        "unit_one": "node --test --test-reporter=tap --test-name-pattern=\"^{name}$\"",
        "name_escape": True,  # unit_one gets re.escape(name)
        "unit_parser": "regex",
        "unit_target": "",
        "unit_fail_re": [r"^\s*not ok \d+ - (.+?)(?: # .*)?$", r"^\s*[✖✕×] (.+?)(?: \([\d.]+ ?m?s\))?$"],
        "lint": "",
        "lint_strict": "",  # eslint on {files} when the project has eslint (load_profile)
        "lint_strict_brief": "",
        "copy_ignored": [],
        "stack_label": "Node project",
        "env_use": "<env_prefix> node ...",
        "done_brief": ("`node --test --test-name-pattern=\"^<test_name>$\" <test file>` for each test in \"tests\" "
                       "(`<file>::*` is the whole file), then `node --check` on each .js/.mjs file you changed"),
        "tail_brief": "pipe test output through tail -30 or grep \"not ok\"",
        "qa": [],  # {changed} = files the batch changed vs main
        "accept": "node --test --test-reporter=tap {names} {file}",  # one run per test file
        "accept_name": "--test-name-pattern=^{name}$",  # `file::*` runs the whole file
        "accept_fail_re": r"^\s*not ok \d+ - (.+?)(?: # .*)?$",
        "check": "node --check {file}",
        "check_exts": [".js", ".mjs", ".cjs"],
        "check_hint": "node --check <each conflicted .js/.mjs file>",
        "pick_build": "",  # no compile step
        "pick_env": {},
        "pick_err_re": "",
        "fn_added_re": (r"^\+\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?\s+\w+|"
                        r"(?:const|let)\s+\w+\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>)"),
        "fn_def_re": r"(?:\bfunction\*?\s+{name}\b|\b(?:const|let|var)\s+{name}\s*=)",
        "review_skip_re": r"(^|/)(tests?|__tests__)/|[._-](test|spec)\.[mc]?[jt]sx?$",
        "path_re": r"((?:[\w.-]+[\\/])+[\w.-]+\.(?:[mc]?js|jsx|[mc]?ts|tsx))",
        "quoted_path_re": r"[\"'`]((?:\.{1,2}/)*[\w./-]+\.(?:[mc]?js|jsx|[mc]?ts|tsx|json))[\"'`]",
        "strip_root": True,  # make logs' absolute paths repo-relative
        "assert_sides_re": "",
        "shrink_cmd": "",
        "include_re": "",
        "include_const_re": "",
        "scope_flag": "",
        "pin_dirs": ["test", "tests"],
        "test_file": "node --test {file}",
        "require_env": [],
        "env_fail_re": "",
        "crate_blame": False,
        "owner_fns": False,
        "lang": "JavaScript/TypeScript",
        "owners": "the owners in",
        "owner_note": "(the owners are listed in spec/ARCHITECTURE.md; never a local copy)",
    },
}


def derive_unit_one(unit: str, template: str) -> str:
    """The one-test rerun for an overridden `unit`: a `node --test` unit gets the name filter, any other ""."""
    if unit.startswith("node --test") and "--test-name-pattern=" in template:
        return f"{unit} --test-name-pattern=" + template.split("--test-name-pattern=", 1)[1]
    return ""


def load_profile() -> tuple:
    cfg = {}
    if (HOME / "harness.toml").exists():
        import tomllib
        cfg = tomllib.loads((HOME / "harness.toml").read_text(encoding="utf-8"))
    name = os.environ.get("HARNESS_STACK") or cfg.get("stack") or "rust"
    if name not in STACKS:
        sys.exit(f"HARNESS_STACK={name}: known stacks are {', '.join(STACKS)}")
    p = dict(STACKS[name])
    if name == "node":  # the project's own tools, when it has them
        pkg = json.loads((REPO / "package.json").read_text(encoding="utf-8")) if (REPO / "package.json").exists() else {}
        dev = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        if "test" in pkg.get("scripts", {}):
            p.update(unit="npm test --silent", unit_one="")
        if "prettier" in dev:
            p["fmt"] = "npx --no-install prettier --write {files}"
        if "eslint" in dev:
            p["lint_strict"] = "npx --no-install eslint --max-warnings 0 {files}"
            p["lint_strict_brief"] = "npx eslint --max-warnings 0 <the files you changed>"
    over = cfg.get("profile", {})
    p.update(over)
    if name == "node" and "unit" in over and "unit_one" not in over:
        p["unit_one"] = derive_unit_one(over["unit"], STACKS[name]["unit_one"])
    return name, p


STACK, P = load_profile()


def changed_files(cwd, base: str, dirs=None, exts=None) -> list:
    """Repo paths under `dirs` with one of `exts` added or modified at `cwd` against `base`."""
    r = git("diff", "--name-only", "--no-renames", "--diff-filter=AM", base, "--", *(dirs or []), cwd=cwd, check=False)
    exts = tuple(exts or P["src_exts"])
    return [f for f in r.stdout.split() if f.endswith(exts) and (Path(cwd) / f).exists()]


def expand(cmd: str, files: list) -> list:
    """The command(s) to run for a profile string: {QS} filled, {files} once over all, {file} once per file; [] = skip."""
    cmd = cmd.replace("{QS}", QS).replace("{changed}", "{files}")
    if "{file}" in cmd:
        return [cmd.replace("{file}", f'"{f}"') for f in files]
    if "{files}" in cmd:
        return [cmd.replace("{files}", " ".join(f'"{f}"' for f in files[i:i + 60])) for i in range(0, len(files), 60)]
    return [cmd] if cmd else []


# ---------------------------------------------------------------- train
TRAIN = WT / "train"
FIXTURES = WT / "fixture-cache-train"
CRATE = os.environ.get("HARNESS_CRATE", "app")  # workspace crate prefix: <CRATE>-<name> lives in crates/<name>/
FIXTURE_EXT = os.environ.get("HARNESS_FIXTURE_EXT", ".fixture")
TRAIN_ENV = {os.environ.get("HARNESS_FIXTURE_VAR", "APP_FIXTURE_CACHE"): str(FIXTURES), **stack_env(P["train_env"])}


def run_env(extra):
    e = dict(os.environ)
    e.update(extra)
    return e


def mark_proven(text: str, ids) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        if line.startswith("|"):
            c = line.split("|")
            if len(c) > 8 and c[1].strip() in ids:
                c[7] = " proven "
                line = "|".join(c)
        out.append(line)
    return "".join(out)


def tests_cmd(tests):
    stems = sorted({Path(t.split("::")[0]).stem for t in tests})
    names = sorted({t.split("::")[1] for t in tests})
    return stems, names


def run_tests(tests, timeout=1800, solo=None):
    """Acceptance tests 'file::name' failing on the train tree (or the desk); empty = pass."""
    if desk.offload(sys.modules[__name__]):
        res = desk.job(sys.modules[__name__], "tests", timeout, tests=sorted(set(tests)), solo=sorted(solo or []))
        lg = ST / "last-gate.txt"
        lg.unlink(missing_ok=True)
        if res["fails"] and res.get("log"):
            lg.write_text(res["log"], encoding="utf-8")
        return res["fails"]
    return run_tests_cargo(tests, timeout, solo) if P["accept"] == "cargo" else run_tests_cmd(tests, timeout)


def unit_runs(cmds, timeout=1800, first_fail=False, cwd=None) -> list:
    """[CompletedProcess] of each unit command, in order, in the train tree (or the desk), or in `cwd` here."""
    if cwd is None and desk.offload(sys.modules[__name__]):
        res = desk.job(sys.modules[__name__], "cmd", timeout * len(cmds), cmds=list(cmds), timeout=timeout,
                       first_fail=first_fail)
        return [subprocess.CompletedProcess(c, r["rc"], r["out"], "") for c, r in zip(cmds, res["runs"])]
    out = []
    for c in cmds:
        out.append(sh(c, cwd=cwd or TRAIN, check=False, env=run_env(TRAIN_ENV), timeout=timeout))
        if first_fail and out[-1].returncode != 0:
            break
    return out


def acceptance_tests(tests):
    with train_stage("acceptance"):
        return desk.run(sys.modules[__name__], tests) if desk.on(ST) else run_tests(tests)


BUILD_FAILED = "test build failed: "  # run_alone's tail when a test exe did not build (quarantine never caches it)


def run_alone(tests, timeout=1800) -> dict:
    """{test: failure tail} of the tests failing alone in the train tree (or the desk)."""
    if desk.offload(sys.modules[__name__]):
        return desk.job(sys.modules[__name__], "alone", timeout + 900 * len(tests), tests=sorted(tests))["red"]
    env = run_env(TRAIN_ENV)
    if P["accept"] == "cargo":
        exes, r = build_test_exes(sorted({Path(t.split("::")[0]).stem for t in tests}), env, timeout)
        build = BUILD_FAILED + r.stdout[-300:] + r.stderr[-1200:]
        tails = {t: run_one_alone(exes, t, env) if Path(t.split("::")[0]).stem in exes else build for t in tests}
    else:
        tails = {}
        for t in tests:
            bad, log = run_cmd_test(t.split("::")[0], [t.split("::", 1)[1]], env)
            tails[t] = (log or "failed") if bad and not perf_debt(t, log) else ""
    return {t: tail for t, tail in tails.items() if tail and not tail.startswith(TIME_CAP)}  # a time cap: no verdict


def perf_debt(t, out) -> bool:
    """A perf budget failed: meaningless under load, so record it for a quiet machine."""
    if not re.search(r"over budget|under floor|over the budget|over limit|^BENCH ", out, re.M):
        return False
    with open(ST / "perf-debt.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": iso(), "test": t, "out": out[-400:]}) + chr(10))
    event("perf_debt", test=t)
    return True


def run_cmd_test(f, names, env) -> tuple:
    argv = []
    for tok in P["accept"].split():
        if tok == "{names}":
            argv += [P["accept_name"].replace("{name}", re.escape(n)) for n in names if n != "*"]
        else:
            argv.append(tok.replace("{file}", f))
    argv[0] = shutil.which(argv[0]) or argv[0]
    try:
        rr = sh(argv, cwd=TRAIN, check=False, env=env, timeout=900)
    except subprocess.TimeoutExpired:
        return names, "timeout 900s"
    if rr.returncode == 0:
        return [], ""
    out = rr.stdout + rr.stderr
    bad = set(re.findall(P["accept_fail_re"], out, re.M))
    return [n for n in names if n in bad] or names, out[-1500:]


def run_tests_cmd(tests, timeout=1800):
    """One profile `accept` run per test file, in parallel, failures parsed by `accept_fail_re`; each re-runs alone,
    serially, before it counts (perf tests fail under load)."""
    if not tests:
        return []
    from concurrent.futures import ThreadPoolExecutor
    by_file, env = {}, run_env(TRAIN_ENV)
    for t in tests:
        by_file.setdefault(t.split("::")[0], []).append(t.split("::", 1)[1])
    retry, fails, logs = [], [], []
    with ThreadPoolExecutor(10) as ex:
        for f, (bad, _) in zip(sorted(by_file), ex.map(lambda f: run_cmd_test(f, by_file[f], env), sorted(by_file))):
            retry += [f"{f}::{n}" for n in bad]
    for t in retry:
        bad, out = run_cmd_test(t.split("::")[0], [t.split("::", 1)[1]], env)
        if bad and not perf_debt(t, out):
            fails.append(t)
            logs.append(f"== {t} (alone): {out}")
    if logs:
        (ST / "last-gate.txt").write_text(chr(10).join(logs)[-20000:], encoding="utf-8")
    return fails


def build_test_exes(stems, env, timeout=1800) -> tuple:
    cmd = ["cargo", "test", "--release", "--no-run", "--message-format=json", "-q"] + sum((["--test", s] for s in stems), [])
    r = sh(cmd, cwd=TRAIN, check=False, env=env, timeout=timeout)
    exes = {}
    for line in r.stdout.splitlines():
        if line.startswith("{") and '"executable"' in line:
            m = json.loads(line)
            if m.get("executable") and m.get("target", {}).get("kind") == ["test"]:
                exes[m["target"]["name"]] = m["executable"]
    return exes, r


TIME_CAP = "time cap: "  # run_one_alone's tail when the alone run was killed: no verdict, never red


def run_one_alone(exes, t, env) -> str:
    """Failure tail of one test run alone; "" on a pass or perf debt; TIME_CAP... when killed by its cap."""
    stem, name = Path(t.split("::")[0]).stem, t.split("::")[1]
    if stem not in exes:
        return "no executable"
    try:
        rr = sh([exes[stem], "--exact", "--test-threads=1", name], cwd=TRAIN, check=False, env=env, timeout=900)
        out = rr.stdout + rr.stderr
        m = re.search(r"sample.generate ([\w-]+): .*locked by another writer", out)
        if rr.returncode != 0 and m:  # stale fixture lock: drop the fixture, regenerate once
            for f in FIXTURES.glob(m.group(1) + FIXTURE_EXT + "*"):
                f.unlink(missing_ok=True)
            rr = sh([exes[stem], "--exact", "--test-threads=1", name], cwd=TRAIN, check=False, env=env, timeout=900)
            out = rr.stdout + rr.stderr
    except subprocess.TimeoutExpired as e:
        return f"{TIME_CAP}{e.timeout}s"
    except Exception as e:  # noqa: BLE001
        return str(e) or type(e).__name__
    if rr.returncode == 0 or perf_debt(t, out):
        return ""
    return out[-1500:] or f"exit {rr.returncode}"


ACCEPT_TIMES = "accept-times.json"  # {stem: seconds its binary last took in the parallel phase}: start order
ALONE_TIMES = "alone-times.json"  # {test: last alone seconds}
SOLO_DAYS = 2  # a test with a perf-budget miss this recent runs alone only (see solo_tests)


def solo_tests(tests) -> set:
    """Tests with a perf-budget miss (state/perf-debt.jsonl) within SOLO_DAYS: they fail under load, so run alone
    only."""
    p = ST / "perf-debt.jsonl"
    if not p.exists():
        return set()
    cut = datetime.fromtimestamp(now() - SOLO_DAYS * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = set()
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("t", "") >= cut:
            recent.add(e.get("test"))
    return recent & set(tests)


def longest_first(stems, known: dict) -> list:
    """`stems` in start order: unknown (new) binaries, then longest recorded time first, so no slow tail."""
    return sorted(stems, key=lambda s: (-known.get(s, float("inf")), s))


def run_tests_cargo(tests, timeout=1800, solo=None):
    """Build the test binaries once (release, incremental) and run them in parallel, longest first; `solo` tests
    (default solo_tests) alone. Failed 'file::name' (empty = pass)."""
    if not tests:
        return []
    from concurrent.futures import ThreadPoolExecutor
    t0 = now()
    solo = solo_tests(tests) if solo is None else set(solo) & set(tests)
    by_stem = {}
    for t in tests:
        if t not in solo:
            by_stem.setdefault(Path(t.split("::")[0]).stem, []).append(t.split("::")[1])
    env = run_env(TRAIN_ENV)
    stems = sorted(set(by_stem) | {Path(t.split("::")[0]).stem for t in solo})
    exes, r = build_test_exes(stems, env, timeout)
    if r.returncode != 0:
        (ST / "last-gate.txt").write_text((r.stdout[-5000:] + r.stderr[-15000:]), encoding="utf-8")
        missing = [s for s in stems if s not in exes]
        return [t for t in sorted(set(tests)) if Path(t.split("::")[0]).stem in (missing or stems)] or ["build"]
    t_build = now()

    def one(stem):
        exe, s0 = exes.get(stem), now()
        if not exe:
            return stem, by_stem[stem], "no executable", 0.0
        try:
            rr = sh([exe, "--exact", "--test-threads=4", *by_stem[stem]], cwd=TRAIN, check=False, env=env, timeout=900)
        except subprocess.TimeoutExpired:
            return stem, by_stem[stem], "timeout 900s", round(now() - s0, 1)
        secs = round(now() - s0, 1)
        if rr.returncode == 0:
            return stem, [], "", secs
        out = rr.stdout + rr.stderr
        bad = set(re.findall(r"^test (test_\w+) \.\.\. FAILED", out, re.M)) | set(re.findall(r"^\s{4}(test_\w+)$", out, re.M))
        return stem, [n for n in by_stem[stem] if n in bad] or by_stem[stem], out[-1500:], secs

    fails, logs, took = [], [], {}
    with ThreadPoolExecutor(10) as ex:
        for stem, bad, log, secs in ex.map(one, longest_first(by_stem, load(ACCEPT_TIMES, {}))):
            took[stem] = secs
            fails += [f"tests/acceptance/{stem}.rs::{n}" for n in bad]
            if bad:
                logs.append(f"== {stem}: {log}")
    t_par = now()
    # perf tests fail under load: re-run each failure alone, serially, with the solo tests
    retry, fails, logs, alone, capped = fails + sorted(solo), [], [], {}, {}
    for t in retry:
        s0 = now()
        tail = run_one_alone(exes, t, env)
        alone[t] = round(now() - s0)
        if tail.startswith(TIME_CAP):
            capped[t] = tail
        elif tail:
            fails.append(t)
            logs.append(f"== {Path(t.split('::')[0]).stem}::{t.split('::')[1]} (serial): {tail}")
    if capped:
        quarantine.slow(sys.modules[__name__], capped, git("rev-parse", "HEAD", cwd=TRAIN, check=False).stdout.strip())
    if logs:
        (ST / "last-gate.txt").write_text(chr(10).join(logs)[-20000:], encoding="utf-8")
    save(ACCEPT_TIMES, {**load(ACCEPT_TIMES, {}), **took})
    save(ALONE_TIMES, {**load(ALONE_TIMES, {}), **alone})
    event("accept_split", build=round(t_build - t0), parallel=round(t_par - t_build), serial=round(now() - t_par),
          tests=len(set(tests)), solo=len(solo), retried=len(retry) - len(solo),
          slowest=sorted(took.items(), key=lambda kv: -kv[1])[:10],
          alone=sorted(((t.split("/")[-1], v) for t, v in alone.items()), key=lambda kv: -kv[1])[:10])
    want = set(tests)
    return [f for f in fails if f in want] or fails


def fmt_tree(cwd):
    """rustfmt each workspace target root in batches and commit (`cargo fmt --all` overflows Windows' command line);
    other stacks: the profile's `fmt` on files changed vs main."""
    if P["fmt"] != "rustfmt":
        for c in expand(P["fmt"], changed_files(cwd, "main")):
            sh(c, cwd=cwd, check=False, timeout=600)
        if git("status", "--porcelain", cwd=cwd, check=False).stdout.strip():
            git("commit", "-qam", "train: format", cwd=cwd, check=False)
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("fmt_check", f"{QS}/fmt_check.py")
    fc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fc)
    for ed, files in fc.roots(Path(cwd)).items():
        for i in range(0, len(files), 60):
            subprocess.run(["rustfmt", "--edition", ed, *files[i:i + 60]], cwd=cwd, capture_output=True, creationflags=NO_WINDOW)
    if git("status", "--porcelain", cwd=cwd, check=False).stdout.strip():
        git("commit", "-qam", "train: rustfmt", cwd=cwd, check=False)


def regen_tree(cwd) -> str:
    """Run the profile's `regen` commands in the train tree and commit them ("train: regen"): generated files come
    from the merged batch. "" or the failure text."""
    for c in P.get("regen") or []:
        try:
            r = sh(c, cwd=cwd, check=False, timeout=600)
        except subprocess.TimeoutExpired:
            event("regen_error", cmd=c, rc="timeout")
            return f"regen: `{c}` timed out after 600s"
        if r.returncode != 0:
            event("regen_error", cmd=c, rc=r.returncode)
            return f"regen: `{c}` exited {r.returncode}{chr(10)}{(r.stdout + r.stderr)[-3000:]}"
    if git("status", "--porcelain", cwd=cwd, check=False).stdout.strip():
        git("add", "-A", cwd=cwd, check=False)
        git("commit", "-qm", "train: regen", cwd=cwd, check=False)
    return ""


def unit_failures(out: str) -> cf.CargoFailures:
    """The one reading of a unit run: names, and (cargo) each failed target no name accounts for."""
    if P["unit_parser"] == "cargo":
        return cf.parse(out)
    return cf.CargoFailures(tuple(sorted(set().union(*(re.findall(p, out, re.M) for p in P["unit_fail_re"])))))


UNIT_BLOCK_CAP = 12000  # chars of a failing test's captured output in a red unit log (whole lines)


def unit_red_log(out: str, new: "cf.CargoFailures | None" = None) -> str:
    """Output tail, each new failing test's own output (cargo: its captured block; main's failures never feed blame),
    names last (reject reasons)."""
    failing = unit_failures(out)
    if not failing.found:
        return out[-6000:]
    blocks, shown = cf.captured(out) if P["unit_parser"] == "cargo" else {}, []
    for t in [t for t in (new or failing).tests if t in blocks][:5]:
        lines, size = [], 0
        for line in blocks[t].splitlines():  # whole lines: a cut assert side names nothing
            if size + len(line) + 1 > UNIT_BLOCK_CAP:
                break
            lines.append(line)
            size += len(line) + 1
        shown.append(f"---- {t} stdout ----" + chr(10) + chr(10).join(lines))
    if not shown:
        hits = sorted((i, n) for n in (new or failing).tests if (i := out.find(n)) >= 0)
        per = max(200, 1400 // max(1, len(hits[:4])))
        shown = [out[i:i + per].rstrip() for i, _ in hits[:4]]
    keys = failing.keys()
    return chr(10).join([out[-6000:], *shown, f"unit failures ({len(keys)}): " + "; ".join(keys[:8])])


def unit_flaky(failing: cf.CargoFailures, cwd=None) -> bool:
    """True when each failing test, and each target crashed naming none, passes alone on one thread (load flakiness).
    A cargo rerun must pass a test (`--exact` matching nothing exits 0). cwd as unit_runs."""
    keys = failing.keys()
    if not keys or len(keys) > 5 or not P["unit_one"] or (failing.unnamed and not P["unit_target"]):
        return False
    cmds = [P["unit_target"].replace("{target}", k) if k in failing.unnamed else
            P["unit_one"].replace("{name}", re.escape(k).replace('"', ".") if P["name_escape"] else k) for k in keys]
    for key, r in zip(keys, unit_runs(cmds, timeout=900, first_fail=True, cwd=cwd)):
        if r.returncode != 0 or (P["unit_parser"] == "cargo" and not cf.passed(r.stdout)):
            event("gate_stage", stage="unit-red-alone", failing=key, rc=r.returncode)
            return False
    event("gate_stage", stage="unit-flaky-passed-alone", failing=keys)
    return True


def unit_verdict(r, base, cwd=None) -> tuple:
    """(red, failing) of unit run r, the gate's one rule: green when it passed, when every failure is main's own
    (base(), called only after a failure), or when each other failure passes alone (unit_flaky). A failed run naming no
    failure is red."""
    if r.returncode == 0:
        return False, cf.from_keys([])
    failing = unit_failures(r.stdout + r.stderr)
    left = failing.without(base())
    if failing.found and not left.found:  # main is red on exactly these
        event("gate_stage", stage="unit-baseline-red", failing=failing.keys()[:5])
        return False, failing
    if left.found and unit_flaky(left, cwd):
        return False, failing
    return True, failing


@contextmanager
def at_main():
    """The train tree at main for a baseline run, restored after."""
    sha = git("rev-parse", "main").stdout.strip()
    cur = git("rev-parse", "HEAD", cwd=TRAIN).stdout.strip()
    git("checkout", "-q", "--detach", "-f", sha, cwd=TRAIN)
    try:
        yield sha
    finally:
        git("checkout", "-q", "--detach", "-f", cur, cwd=TRAIN)


def main_unit_baseline() -> cf.CargoFailures:
    """Unit failures on main itself (cached per main sha)."""
    sha = git("rev-parse", "main").stdout.strip()
    cache = load("unit-baseline.json", {})
    if cache.get("sha") != sha:
        with at_main():
            rr = unit_runs([P["unit"]])[0]
        fails = unit_failures(rr.stdout + rr.stderr).keys()
        cache = {"sha": sha, "fails": fails}
        save("unit-baseline.json", cache)
        event("unit_baseline", sha=sha, fails=fails[:10])
    return cf.from_keys(cache["fails"])


PY = "py -3" if os.name == "nt" else "python3"
QUALITY_CMDS = [c.replace("{QS}", QS) if " " in c else f"{PY} {QS}/{c} {' '.join(P['src_dirs'])}" for c in P["qa"]]
CLIPPY_STRICT = P["lint_strict"]  # the strict linter (name kept from the rust-only harness)
FILE_LINES = int(os.environ.get("HARNESS_FILE_LINES", "500"))  # spec/STANDARDS.md S8 module-size cap
TEST_FILE_LINES = int(os.environ.get("HARNESS_TEST_FILE_LINES", "600"))  # the same cap for test files
RULES_CHECK = f"{PY} {Path(__file__).with_name('rules_gate.py').as_posix()} check --dir . --lang {STACK}"
FIND_OWNER = f'{PY} {QS}/index_api.py --find "<purpose words>"'
RULES_BRIEF = (f"{RULES_CHECK} (must print RULES OK: no swallowed error or silent default, no expect/panic or silenced "
               "lint in product code, no type named like another owner's Forbidden types, no spec or CLAUDE.md text "
               "over its [text] caps, no new plan_validate problem)")
CHECKS_BRIEF = " ; and ".join(x for x in (" ; ".join(QUALITY_CMDS + [RULES_BRIEF]), P["lint_strict_brief"]) if x)
QUALITY_BRIEF = ("The plan forbids second copies (spec/STANDARDS.md S1, S5, S6, S44): before writing ANY function, "
                 f"private helpers included, find the owner that already has it with {FIND_OWNER} "
                 + P["owner_note"] + " and extend the owner instead. "
                 f"Keep every file you add or grow at most {FILE_LINES} lines (tests {TEST_FILE_LINES}; STANDARDS S8): split into modules. "
                 "Clean code is part of done. "
                 + ("Before committing, from your worktree run each of: " + CHECKS_BRIEF
                    + ". Fix every problem they report in files you changed (extract repeated blocks into one named fn, "
                    "name repeated literals as consts, split functions over 30 lines into named steps). " if CHECKS_BRIEF else "")
                 + "Before writing a helper, find its owner with index_api.py --find and reuse it. "
                 "The land train rejects branches that add any of these.")


def quality_on() -> bool:
    return True  # never optional: removing it takes a code change


OWNED_FN = re.compile(r"^\s*(?:pub(?:\([a-z]+\))?\s+)?fn\s+(unit|unit_above|unit_scaled|cross|cross2|dot|norm|point2|point3|"
                      r"not_found|malformed|conflict|positive|finite)\s*[<(]", re.M)


def owner_copies(cwd) -> set:
    """`file::fn` for every fn outside crates/core named like a capability core owns (math, params, refusals)."""
    found = set()
    if not P["owner_fns"]:
        return found
    for f in Path(cwd).glob("crates/*/src/**/*.rs"):
        rel = f.relative_to(cwd).as_posix()
        if rel.startswith("crates/core/"):
            continue
        for m in OWNED_FN.finditer(f.read_text(encoding="utf-8", errors="replace")):
            found.add(f"{rel}::{m.group(1)}")
    return found




def file_line_cap(rel: str) -> int:
    """The S8 cap for repo path `rel`: test files (profile test_file_re) get TEST_FILE_LINES."""
    return TEST_FILE_LINES if re.search(P["test_file_re"], rel) else FILE_LINES


def oversized_files(cwd, base: str) -> list:
    """Size gate: source files (src_exts under size_dirs) the tree at `cwd` adds or grows past the cap against
    `base`. A file over at `base` is grandfathered until split; renames count as adds."""
    r = git("diff", "--name-only", "--no-renames", "--diff-filter=AM", base, "--", *P["size_dirs"],
            cwd=cwd, check=False)
    if r.returncode != 0:
        return [f"size gate: git diff against {base} failed: {(r.stderr or '').strip()[-300:]}"]
    out = []
    for rel in r.stdout.split():
        f = Path(cwd) / rel
        if not rel.endswith(tuple(P["src_exts"])) or not f.exists():
            continue
        cap, now_n = file_line_cap(rel), len(f.read_text(encoding="utf-8", errors="replace").splitlines())
        if now_n <= cap:
            continue
        old = git("show", f"{base}:{rel}", cwd=cwd, check=False)
        before = len(old.stdout.splitlines()) if old.returncode == 0 else 0
        if before <= cap:
            out.append(f"{rel}:1: {now_n} lines (was {before}); STANDARDS S8 caps a file at {cap}: split it into modules")
    return out


def quality_problems(cwd, base: str = "main", landing=(), plan: str = "spec") -> str:
    """Output of every failing quality check (empty = clean); the size gate measures growth from `base`."""
    static = static_problems(cwd, base, plan, landing)
    out = [static] if static else []
    env = run_env(qc_env(cwd))
    with lock("qc-other", wait=4000) if Path(env.get("CARGO_TARGET_DIR", "")).name == "qc-other" else nullcontext():
        for c in expand(CLIPPY_STRICT, changed_files(cwd, base) if "{file" in CLIPPY_STRICT else []):
            r = sh(c, cwd=cwd, check=False, env=env, timeout=1800)
            if r.returncode != 0:
                out.append((r.stdout + r.stderr)[-4000:])
    return chr(10).join(out)


def qc_env(cwd) -> dict:
    """TRAIN_ENV for linting cwd: train target, caller's CARGO_TARGET_DIR, slot target, else qc-other (hold its lock)."""
    env = dict(TRAIN_ENV)
    if "CARGO_TARGET_DIR" not in env or Path(cwd).resolve() == TRAIN.resolve():
        return env
    m = re.match(r"s(\d+)$", Path(cwd).name)
    env["CARGO_TARGET_DIR"] = (os.environ.get("CARGO_TARGET_DIR")
                               or (env_for(int(m.group(1))).get("CARGO_TARGET_DIR") if m else "")
                               or str(TARGETS / "qc-other"))
    return env


def static_problems(cwd, base: str = "main", plan: str = "spec", landing=()) -> str:
    """quality_problems without the lint (no build); plan="off" skips the plan check (minutes)."""
    out = []
    allow = set((ST / "owner-allowlist.txt").read_text(encoding="utf-8").split()) if (ST / "owner-allowlist.txt").exists() else set()
    new_copies = sorted(owner_copies(cwd) - allow)
    if new_copies:
        out.append("second copy of a capability core owns (STANDARDS S1/S6; use app_core::math/param/refusal): "
                   + " ".join(c.split("::")[0] + ":1: " + c.split("::")[1] for c in new_copies))
    big = oversized_files(cwd, base)
    if big:
        out.append("file over the S8 size cap (new or newly over; existing big files are grandfathered until split): "
                   + chr(10) + chr(10).join(big))
    rules = rules_gate.problems(cwd, base, ST, WT, STACK, plan=plan, landing=landing)
    if rules:
        out.append(f"rule ratchet (rules_gate.py; a file may not grow past its state/{rules_gate.DEBT} count): "
                   + chr(10) + chr(10).join(rules))
    env = run_env(TRAIN_ENV)
    changed = changed_files(cwd, base) if any("{changed}" in c for c in QUALITY_CMDS) else []
    for c in [x for c in QUALITY_CMDS for x in (expand(c, changed) if "{changed}" in c else [c])]:
        r = sh(c, cwd=cwd, check=False, env=env, timeout=1800)
        if r.returncode != 0:
            out.append((r.stdout + r.stderr)[-4000:])
    return chr(10).join(out)


def cmd_quality_check(a):
    """Used by <repo>/.git/hooks/pre-push: refuse to publish a tree with quality problems."""
    d = Path(a.dir) if a.dir else REPO
    # --regen: regenerate first, as the train does, so its fixable drift does not fail
    if getattr(a, "regen", False):
        bad = regen_tree(d)
        if bad:
            print(bad[-3000:])
            sys.exit(1)
    # local main: origin/main lags when a push is refused
    q = quality_problems(d, a.base or "main")
    if q:
        print(q[-3000:])
        sys.exit(1)
    print("QUALITY OK")


def cmd_clean_start(a):
    p = ensure_wt(a.slot, f"clean/{a.name}-s{a.slot}", "main")
    e = env_for(a.slot)
    print(json.dumps({"worktree": str(p).replace(chr(92), "/"), "env_prefix": " ".join(f"{k}={v}" for k, v in e.items()).replace(chr(92), "/")}))


def cmd_review_prompt(a):
    d = Path(a.dir) if a.dir else REPO
    print(review_prompt(added_functions(d, a.base or "main")), end="")


def cmd_unit_baseline(a):
    """Main's unit failures for the tree --dir (at main) to --out as {"sha", "fails"}: `unit-check`'s baseline. A
    builder outside the train caches it per main sha."""
    d = Path(a.dir)
    r = unit_runs([P["unit"]], cwd=d)[0]
    data = {"sha": git("rev-parse", "HEAD", cwd=d).stdout.strip(), "fails": unit_failures(r.stdout + r.stderr).keys()}
    tmp = Path(f"{a.out}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, a.out)
    print(f"UNIT BASELINE {data['sha'][:12]}: {len(data['fails'])} failing on main")


def cmd_unit_check(a):
    """unit_verdict on the tree --dir against --baseline. Prints MAIN-RED <names> for main's own failures, then RED
    <names> and exit 1 when the tree fails tests main passes that also fail alone, else UNIT OK."""
    base = cf.from_keys(json.loads(Path(a.baseline).read_text(encoding="utf-8"))["fails"])
    if base.found:
        print("MAIN-RED " + "; ".join(base.keys()[:20]))
    r = unit_runs([P["unit"]], cwd=Path(a.dir))[0]
    red, failing = unit_verdict(r, lambda: base, cwd=Path(a.dir))
    if red:
        print(unit_red_log(r.stdout + r.stderr, failing.without(base))[-4000:])
        print("RED " + ("; ".join(failing.without(base).keys()[:20]) or "(the unit run failed and named no test)"))
        sys.exit(1)
    print("UNIT OK")


def cmd_self_review(a):
    """Run the land train's duplicate-capability reviewer on a slot's branch (functions it adds vs main)."""
    wt = slot_wt(a.slot)
    base = git("merge-base", "main", "HEAD", cwd=wt).stdout.strip() or "main"
    try:
        dups = batch_review([], cwd=wt, base=base)
    except ReviewUnavailable as e:
        sys.exit(f"self-review: reviewer unavailable twice ({e}); run it again before submitting")
    print(json.dumps({"duplicates": dups}, indent=1))


def cmd_clean_submit(a):
    p = slot_wt(a.slot)
    if git("status", "--porcelain", cwd=p).stdout.strip():
        sys.exit("commit first")
    sha = git("rev-parse", "HEAD", cwd=p).stdout.strip()
    with lock("state"):
        queue_clean(a.name, sha, a.slot, front=a.front, absorbs=a.absorbs.split(",") if a.absorbs else None, replace=a.replace)
    print(json.dumps({"ok": True}))


def queue_clean(name, sha, slot, front=False, absorbs=None, replace=False) -> None:
    """Queue sha as row CLEAN-<name> (caller holds the state lock). front: ahead of every row (ts = oldest - 1).
    absorbs: rows it lands too (lands_rows), each verified against sha; None keeps the queued entry's."""
    row = f"CLEAN-{name}"
    if why := replace_refusal(row, sha, slot, replace):  # QueueRefused: another slot's packet holds the row
        raise QueueRefused(why)
    queue = load("queue.json", [])
    if absorbs is None:
        absorbs = next((q.get("absorbs", []) for q in queue if q["row"] == row), [])
    absorbs = [r for r in dict.fromkeys(r.strip() for r in absorbs) if r and r != row]
    if bad := absorbs_unverified(sha, absorbs):
        sys.exit(f"clean-submit: {sha[:12]} does not contain the commits of absorbed rows {bad} (each needs commits under "
                 "its own subject in main..sha and, when h/<row> exists, that tip as an ancestor)")
    rest = [q for q in queue if q["row"] != row]
    ts = min([now()] + [q["ts"] for q in rest]) - 1 if front else now()
    entry = {"row": row, "sha": sha, "ts": ts, "slot": slot, **({"absorbs": absorbs} if absorbs else {})}
    absorb_own_rows(entry, rest)  # its commits' spec rows, so a lane never names them
    extra = {"absorbs": entry["absorbs"]} if entry.get("absorbs") else {}
    save("queue.json", rest + [entry])
    event("submit", slot=slot, row=row, sha=sha, **extra)


def known_red() -> dict:
    """Known-red entries (quarantine.py); the rule is known_red_tests()."""
    return load(quarantine.KNOWN, {})


def known_red_tests() -> set:
    """The one known-red rule: never gated, never a failure, blame input, probe target or pick red."""
    return set(known_red())


TRAIN_STAGE = "train-stage.json"


@contextmanager
def train_stage(stage: str):
    """Mark `stage` (alarm_dispatch waits out acceptance; finish.py and priority_keeper.ps1 the compile stages)."""
    save(TRAIN_STAGE, {"stage": stage, "pid": os.getpid(), "t": iso()})
    try:
        yield
    finally:
        save(TRAIN_STAGE, {})


def gate_tests(new_rows, rows) -> list:
    """Both gate paths' acceptance tests: landing and proven rows', less known_red_tests()."""
    tests = {t for i in new_rows for t in rows[i]["tests"]}
    tests |= {t for r_ in rows.values() if r_["status"] == "proven" for t in r_["tests"]}
    if skipped := sorted(tests & known_red_tests()):
        event("gate_stage", stage="known-red-skipped", tests=skipped[:5], n=len(skipped))
    return sorted(tests - set(skipped))


def gate(new_rows, rows, quick=False):
    """Return (ok, failed_tests, log)."""
    env = run_env(TRAIN_ENV)
    fmt_tree(TRAIN)  # auto-fix formatting, never fail on it
    bad = regen_tree(TRAIN)  # a failing regen fails the batch
    if bad:
        return False, ["quality"], bad
    if quick:  # retry after ejecting rows whose tests failed: lint passed; unit re-runs (removing a branch can
        # break another that used it)
        with train_stage("unit"):
            r = unit_runs([P["unit"]])[0]
        red, failing = unit_verdict(r, main_unit_baseline)
        if red:
            return False, ["unit"], unit_red_log(r.stdout + r.stderr, failing.without(main_unit_baseline()))
        with train_stage("quality"):
            q = quality_problems(TRAIN, landing=new_rows, plan="always")
        if q:
            return False, ["quality"], q
        t0 = now()
        fails = acceptance_tests(gate_tests(new_rows, rows))
        event("gate_stage", stage="acceptance-quick", secs=round(now() - t0), fails=len(fails), failed=sorted(fails)[:20])
        log = (ST / "last-gate.txt").read_text(encoding="utf-8", errors="replace")[-20000:] if fails else ""
        return (not fails), fails, log
    if not quality_on():  # the quality stage's strict clippy is a superset
        t0 = now()
        with train_stage("clippy"):
            r = sh(P["lint"] or "exit 0", cwd=TRAIN, check=False, env=env, timeout=1800)
        event("gate_stage", stage="clippy", secs=round(now() - t0), rc=r.returncode)
        if r.returncode != 0:
            return False, ["clippy"], (r.stdout + r.stderr)[-6000:]
    t0 = now()
    with train_stage("unit"):
        r = unit_runs([P["unit"]])[0]
    event("gate_stage", stage="unit", secs=round(now() - t0), rc=r.returncode)
    red, failing = unit_verdict(r, main_unit_baseline)
    out = r.stdout + r.stderr
    if red and not P["crate_blame"]:
        return False, ["unit"], unit_red_log(out, failing.without(main_unit_baseline()))
    if red:
        pkgs = sorted({t.split()[1] for t in failing.targets if t.split()[1].startswith(CRATE)})
        dirs = " ".join("src/" if p == CRATE else "crates/" + p[len(CRATE) + 1:] + "/" for p in pkgs)
        # the description sits above `failing crates:`, which try_batch splits into blame paths
        return False, ["unit"], (failing.describe() + chr(10) + f"failing crates: {dirs}" + chr(10)
                                 + unit_red_log(out, failing.without(main_unit_baseline())))
    if quality_on():
        t0 = now()
        with train_stage("quality"):
            q = quality_problems(TRAIN, landing=new_rows, plan="always")
        event("gate_stage", stage="quality", secs=round(now() - t0), rc=1 if q else 0)
        if q:
            return False, ["quality"], q
    tests = gate_tests(new_rows, rows)
    t0 = now()
    fails = acceptance_tests(tests)
    event("gate_stage", stage="acceptance", secs=round(now() - t0), n=len(tests), fails=len(fails), failed=sorted(fails)[:20])
    # an empty log would send every unowned acceptance failure to bisect
    log = (ST / "last-gate.txt").read_text(encoding="utf-8", errors="replace")[-20000:] if fails else ""
    return (not fails), fails, log


def build_train(batch, rows):
    """Merge every queued sha onto main in the train worktree; (merged, conflicted). Superseded entries are left out
    (neither merged nor rejected) for a later train."""
    if stale := superseded(batch):
        event("train_superseded", rows=[q["row"] for q in stale], shas=[q["sha"][:12] for q in stale], at="build")
        batch = [q for q in batch if q not in stale]
    if not (TRAIN / ".git").exists():
        git("worktree", "add", "-f", "--detach", str(TRAIN), "main")
    git("reset", "-q", "--hard", cwd=TRAIN, check=False)
    git("checkout", "-q", "--detach", "main", cwd=TRAIN)
    git("reset", "-q", "--hard", "main", cwd=TRAIN)
    copy_assets(TRAIN)
    merged, conflicted = [], []
    for q in batch:
        r = git("merge", "-q", "--no-ff", "--no-edit", "-m", f"train: land {q['row']}", q["sha"], cwd=TRAIN, check=False)
        if r.returncode != 0:
            git("merge", "--abort", cwd=TRAIN, check=False)
            conflicted.append(q)
        else:
            merged.append(q)
    # one hot file can reject most of a train: resolve a few here; the gate still tests the result
    for q in list(conflicted[:RESOLVE_MAX]):
        if resolve_conflict(q):
            conflicted.remove(q)
            merged.append(q)
    return merged, conflicted


RESOLVE_MAX = int(os.environ.get("HARNESS_RESOLVE_MAX", "12"))  # conflicted rows per train given to the resolver
RESOLVE_PROMPT = """You are resolving a git merge conflict in the current folder (the land train's tree; main is already proven).
Row {row}'s branch is being merged onto main. Conflicted files: {files}
1. Open only the conflicted hunks (grep -n '<<<<<<<\\|>>>>>>>' then read those line ranges). Keep BOTH sides: main's code must
   keep all its behaviour; apply the row's additions on top of it. Where both sides changed the same function, combine them.
2. Remove every conflict marker. Touch other files only if needed to compile.
3. Run `@CHECK@` and fix errors until it is clean.
4. Do NOT commit, abort the merge, or run tests. Reply DONE, or FAIL with one line why. Keep tool output small.""".replace(
    "@CHECK@", P["check_hint"])


def resolve_conflict(q):
    """Merge q again with a short Sonnet pass resolving the conflict; accept only with no markers left and cargo
    check passing."""
    t0 = now()
    git("merge", "-q", "--no-ff", "--no-edit", "-m", f"train: land {q['row']} (conflict resolved)", q["sha"], cwd=TRAIN, check=False)
    files = git("diff", "--name-only", "--diff-filter=U", cwd=TRAIN, check=False).stdout.split()
    ok = bool(files) and len(files) <= 4 and all(f.endswith(tuple(P["src_exts"])) for f in files)
    if ok:
        sh([CLAUDE_BIN, "-p", "--model", "sonnet", "--max-turns", "30", "--dangerously-skip-permissions"], cwd=TRAIN,
           check=False, env=run_env(TRAIN_ENV), timeout=600, stdin=RESOLVE_PROMPT.format(row=q["row"], files=" ".join(files)))
        marked = git("grep", "-l", "-E", "^(<<<<<<<|>>>>>>>) ", "--", *files, cwd=TRAIN, check=False).stdout.strip()
        ok = not marked and all(sh(c, cwd=TRAIN, check=False, env=run_env(TRAIN_ENV), timeout=900).returncode == 0
                                for c in expand(P["check"], [f for f in files if f.endswith(tuple(P["check_exts"]))]))
    if ok:
        git("add", "-A", cwd=TRAIN, check=False)
        ok = git("commit", "-q", "--no-edit", cwd=TRAIN, check=False).returncode == 0
    if not ok:
        git("merge", "--abort", cwd=TRAIN, check=False)
        git("reset", "-q", "--hard", "HEAD", cwd=TRAIN, check=False)
    event("resolve", row=q["row"], ok=ok, files=files[:4], secs=round(now() - t0))
    return ok


# A proven row's prose is dead text (its test is the contract): the proven commit deletes it.
PACKETS = "spec/packets"
BULLET_RE = re.compile(r"^- ([A-Za-z0-9][\w.-]*): ")
PACKET_RE = re.compile(r"^- `([A-Za-z0-9][\w.-]*)` ")


def drop_items(text: str, ids, head: re.Pattern) -> str:
    """text without the list items `head` matches with group 1 in ids, continuation lines included."""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if skip and line[:1] in (" ", "\t") and line.strip():
            continue
        m = head.match(line)
        skip = bool(m) and m.group(1) in ids
        if not skip:
            out.append(line)
    return "".join(out)


def finish_spec(ids):
    ids = set(ids)
    p = TRAIN / SPEC
    p.write_text(drop_items(mark_proven(p.read_text(encoding="utf-8"), ids), ids, BULLET_RE), encoding="utf-8")
    for f in sorted((TRAIN / PACKETS).glob("*.md")):
        text = f.read_text(encoding="utf-8")
        if (pruned := drop_items(text, ids, PACKET_RE)) != text:
            f.write_text(pruned, encoding="utf-8")
    # keep spec/INDEX.md + spec/index/* current: builders search them before writing any function (STANDARDS S6)
    sh([*PY.split(), f"{QS}/index_api.py"], cwd=TRAIN, check=False, timeout=600)
    tighten_shrink_lists(TRAIN, "main")  # tracked product lists: `commit -a` takes them
    git("add", "-A", "spec", cwd=TRAIN, check=False)
    git("commit", "-qam", f"train: proven {' '.join(ids)}", cwd=TRAIN, check=False)


# The product's shrink-only list tests print each list tightened between these marks (SKILL.md Train rules)
SHRINK_BEGIN = re.compile(r"--- tightened (\S+) ---")  # unanchored: progress dots share the line
SHRINK_END = "--- end tightened ---"


def printed_lists(out: str) -> dict:
    """{list path: tightened text} of every complete marked block in out."""
    lists = {}
    for m in SHRINK_BEGIN.finditer(out):
        body, sep, _ = out[m.end():].partition(SHRINK_END)
        if sep:
            lists[m.group(1)] = body
    return lists


def lowered_list(old: str, printed: str) -> tuple:
    """(text, changes): each `path count` line of old lowered to printed's count (absent 0, dropped at 0), never raised."""
    def entry(line):
        file, _, n = line.rpartition(" ")
        return (file, int(n)) if file and not line.startswith("#") and n.isdigit() else None
    now_ = dict(e for e in map(entry, printed.splitlines()) if e)
    out, changes = [], []
    for line in old.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if (e := entry(body)) is None:
            out.append(line)
            continue
        file, n = e
        if (m := min(n, now_.get(file, 0))) < n:
            changes.append(f"{file} {n}->{m}")
        if m:
            out.append(f"{file} {m}{line[len(body):]}")
    return "".join(out), changes


def in_src(rel: str) -> bool:
    return any(d in (".", "") or rel.startswith(d.rstrip("/") + "/") for d in P["src_dirs"])


def tighten_shrink_lists(tree, base) -> list:
    """When base..HEAD changed product files, lower each tracked .txt list shrink_cmd prints; anything else keeps them."""
    if not P["shrink_cmd"]:
        return []
    changed = git("diff", "--name-only", "--no-renames", base, "HEAD", cwd=tree, check=False).stdout.split()
    if not any(in_src(f) and f.endswith((*P["src_exts"], ".txt")) for f in changed):
        return []
    try:
        r = sh(P["shrink_cmd"], cwd=tree, check=False, env=run_env(TRAIN_ENV), timeout=900)
    except subprocess.TimeoutExpired:
        event("shrink_lists_error", error="timeout")
        return []
    printed = printed_lists(r.stdout) if not r.returncode else {}
    if not printed:
        event("shrink_lists_error", rc=r.returncode, error=(r.stdout + r.stderr)[-300:])
        return []
    tracked = set(git("ls-files", "--", "*.txt", cwd=tree, check=False).stdout.split())
    cut = []
    for rel, text in printed.items():
        if rel not in tracked:
            continue
        p = Path(tree) / rel
        new, changes = lowered_list(p.read_bytes().decode("utf-8"), text)
        if changes:
            p.write_bytes(new.encode("utf-8"))
            cut += [f"{rel}: {c}" for c in changes]
    if cut:
        event("shrink_lists_tightened", changes=cut[:20], n=len(cut))
    return cut


def reject(q, reason, locked=False):
    """Record the reject AND dequeue the row (every reject path), so it is never picked again. locked=True: the
    caller holds the state lock."""
    with nullcontext() if locked else lock("state"):
        rj = load("rejects.json", {})
        rj.setdefault(q["row"], []).append({"t": iso(), "by": "train", "sha": q["sha"], "reason": reason[:1500]})
        save("rejects.json", rj)
        save("queue.json", [x for x in load("queue.json", []) if x["sha"] != q["sha"]])
    event("reject", row=q["row"], reason=reason[:300])


def dup_text(dups) -> str:
    """The one refusal text for duplicates (pick time, submit time, cloud collect)."""
    why = "; ".join(f"{d['file']}::{d['fn']} duplicates {d['existing']} ({d.get('why', '')[:120]})" for d in dups)
    return ("duplicate capability (STANDARDS S1/S6): reuse or extend the owner, delete your copy: " + why) if why else ""


def dup_reason(q, dups) -> str:
    return dup_text([d for d in dups if adds_fn(q, d)])


def submit_dup(cwd) -> str:
    """Refusal text when the branch in cwd adds a duplicate capability (batch_review on merge-base..HEAD, so every
    duplicate is the row's own), else "". An unavailable reviewer is no verdict: warn on stderr and let the row queue;
    the pick-time review still guards it."""
    base = git("merge-base", "main", "HEAD", cwd=cwd).stdout.strip() or "main"
    try:
        return dup_text(batch_review([], cwd=cwd, base=base))
    except ReviewUnavailable as e:
        print(f"submit: duplicate reviewer unavailable twice ({e}); queued, the pick-time review decides", file=sys.stderr)
        return ""


def adds_fn(q, d) -> bool:
    """True when branch q itself adds fn d["fn"] in d["file"], not merely touches it."""
    diff = git("diff", "-U0", f"main...{q['sha']}", "--", d["file"], check=False).stdout
    return re.search(r"^\+.*" + P["fn_def_re"].replace("{name}", re.escape(d["fn"])), diff, re.M) is not None


def branch_files(q) -> set:
    return set(git("diff", "--name-only", f"main...{q['sha']}", check=False).stdout.split())


REVIEW_PROMPT = """You are the duplicate-capability reviewer for the rebuild (@LANG@). Your cwd is the merged train tree.
These functions were ADDED by the batch about to land (file: signature):
{added}
For each one, decide whether the codebase ALREADY has a function doing the same job (same input meaning, same output,
possibly different name/wording): search spec/index/*.md and grep @GREP@, and read both bodies before deciding.
Owners to prefer: @OWNERS@
spec/ARCHITECTURE.md. Report ONLY verified duplicates. Thin wrappers that call the owner are fine. Be fast: at most 40 tool calls.
Final answer: ONLY a JSON object {{"duplicates": [{{"file": "<path of the NEW copy>", "fn": "<name>", "existing": "<file:line>", "why": "<short>"}}]}}"""
REVIEW_PROMPT = (REVIEW_PROMPT.replace("@LANG@", P["lang"]).replace("@OWNERS@", P["owners"])
                 .replace("@GREP@", " ".join(d.rstrip("/") + "/" for d in P["src_dirs"])))


CLAUDE_BIN = (shutil.which("claude.exe") or shutil.which("claude")
              or str(Path.home() / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"))
REVIEW_MAX_FNS = 300


class ReviewUnavailable(Exception):
    """The reviewer gave no usable answer twice: the batch waits, never landing unreviewed."""


def added_functions(cwd, base: str) -> list:
    """`file: signature` of each function HEAD at `cwd` adds against `base` (review_skip_re files left out)."""
    diff = git("diff", "-U0", base, "HEAD", "--", *P["src_dirs"], cwd=cwd, check=False).stdout
    added, cur = [], ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
        elif line.startswith("+") and re.match(P["fn_added_re"], line) and not re.search(P["review_skip_re"], cur):
            added.append(f"{cur}: {line[1:].strip()[:160]}")
    return added


def review_prompt(added: list) -> str:
    """The one duplicate-review prompt for `added`; "" when nothing is added."""
    return REVIEW_PROMPT.format(added=chr(10).join(added[:REVIEW_MAX_FNS])) if added else ""


def batch_review(merged, cwd=None, base="main") -> list:
    """Headless reviewer over the functions this batch adds, fail-CLOSED: an empty or unparseable answer is retried
    once, then ReviewUnavailable defers the batch. Builders run it as `self-review --slot N`."""
    cwd = cwd or TRAIN
    added = added_functions(cwd, base)
    if not added:
        return []
    t0, last = now(), ""
    for attempt in (1, 2):
        try:
            r = sh([CLAUDE_BIN, "-p", "--output-format", "json", "--model", "sonnet", "--max-turns", "60",
                    "--dangerously-skip-permissions"], cwd=cwd, check=False, timeout=1500,
                   stdin=review_prompt(added))
            res = json.loads(r.stdout).get("result", "")
            m = re.search(r"\{.*\}", res, re.S)
            if not m:
                raise ValueError("no JSON object in the reviewer's answer: " + res[-200:])
            dups = json.loads(m.group(0)).get("duplicates", [])
            dups = [d for d in dups if isinstance(d, dict) and d.get("file") and d.get("fn")]
            event("review", added=len(added), duplicates=len(dups), secs=round(now() - t0), attempt=attempt)
            return dups
        except Exception as e:  # noqa: BLE001 - logged, retried once, then the batch is deferred
            last = str(e)[-400:]
            event("review_error", error=last, secs=round(now() - t0), attempt=attempt)
    raise ReviewUnavailable(last)


GREEN = {"head": ""}  # last gate-verified train head; never HEAD, which keeps a bisect's rejected right half


def train_spec(rows: dict) -> dict:
    """`rows` (main's spec) overlaid with the train tree's spec rows, so a batch rewriting a row's acceptance is gated
    on what it lands."""
    p = TRAIN / SPEC
    return {**rows, **parse_spec(p.read_text(encoding="utf-8", errors="replace"))} if p.is_file() else rows


def try_batch(batch, rows, depth=0, quick=False, reviewed=False):
    """Land as much of batch as possible; the landed queue entries."""
    merged, conflicted = build_train(batch, rows)
    for q in conflicted:
        reject(q, "merge conflict with main or another branch in the same train; merge main into your branch and resolve")
    if not merged:
        return []
    rows = train_spec(rows)
    # review first (a minute): a duplicate found after the gate costs a second gate
    if not quick and not reviewed:
        dups = batch_review(merged)
        why = {q["sha"]: dup_reason(q, dups) for q in merged}
        guilty = [q for q in merged if why[q["sha"]]]
        if guilty:
            for q in guilty:
                reject(q, why[q["sha"]])
            event("review_reject", rows=[q["row"] for q in guilty])
            rest = [q for q in merged if q not in guilty]
            return try_batch(rest, rows, depth + 1, quick=False, reviewed=True) if rest else []
    ids = sorted(landing_ids(merged))
    ok, fails, log = gate(ids, rows, quick)
    if not ok and fails:  # the one known-red rule
        kept = [t for t in fails if t not in known_red_tests()]
        ok, fails = (True, []) if not kept else (False, kept)
    if ok:
        return land_verdict(merged, ids, rows, depth)
    own = {i for i in ids for t in rows[i]["tests"] if t in fails}
    if fails not in (["clippy"], ["unit"], ["quality"]) and own and all(any(t in rows[i]["tests"] for i in own) for t in fails):
        # only the batch's own tests failed: eject those rows, retry the rest
        for q in merged:  # an absorbed row's red test is its integration entry's
            if mine := [t for i in lands_rows(q) for t in rows[i]["tests"] if t in fails]:
                reject(q, "own acceptance failed on merged tree: " + ", ".join(mine))
        rest = [q for q in merged if not own & set(lands_rows(q))]
        return try_batch(rest, rows, depth + 1, quick=True) if rest else []
    # before blame and probe, which pin a main-red test on an innocent row
    head = git("rev-parse", "HEAD", cwd=TRAIN, check=False).stdout.strip()  # gated_tree_intact compares it
    if healed := quarantine.heal(sys.modules[__name__], merged, rows, fails, ids):
        if healed.covers(fails) and gated_tree_intact(head):  # the verdict stands
            event("accept_main_red_only", tests=sorted(fails)[:10], rows=[q["row"] for q in merged])
            return land_verdict(merged, ids, rows, depth)
        return try_batch(merged, rows, depth + 1, reviewed=True)
    # blame by file
    if P["strip_root"]:  # absolute paths in the log -> repo-relative
        for root in {str(TRAIN), TRAIN.as_posix()}:
            log = (log or "").replace(root + chr(92), "").replace(root + "/", "")
    if len(merged) > 1:
        group, entries, lists = blame_entries(merged, fails, log)
        if group and len(group) < len(merged):  # one shared cause's rows in one step, the rest gated as one run
            why = (f"gate red ({fails[:3]}): your branch changes an entry the failing assert diff names "
                   f"({sorted(entries)[:4]}), as does every row ejected with it: {[q['row'] for q in group]}; "
                   f"lists read: {sorted(lists)[:3]} :: {log[-1200:]}")
            for q in group:
                reject(q, why)
            event("blame_entries", ejected=[q["row"] for q in group], entries=sorted(entries)[:10], lists=sorted(lists)[:5])
            return try_batch([q for q in merged if q not in group], rows, depth + 1, reviewed=True)
        guilty, named, own = blame_by_file(merged, rows, fails, log)
        if len(guilty) == 1:
            q = guilty[0]
            reject(q, f"gate red ({fails[:3]}) in a file your branch changed: {sorted(touching(own[q['row']], named))[:5]} :: {log[-1200:]}")
            event("blame_by_file", ejected=[q["row"]], named=sorted(named)[:10])
            return try_batch([x for x in merged if x is not q], rows, depth + 1)
        if guilty:
            event("blame_suspects", rows=[q["row"] for q in guilty], named=sorted(named)[:10])
    if len(merged) > 1 and fails and not set(fails) & {"clippy", "unit", "quality"}:
        bad = probe_culprit(merged, rows, fails)
        if bad is not None:
            reject(bad, f"gate red: {fails[:3]} fail with only your branch merged on main (probe bisect) :: {log[-1200:]}")
            return try_batch([q for q in merged if q is not bad], rows, depth + 1, reviewed=True)
    if len(merged) == 1:
        reject(merged[0], f"gate red on merged tree: {fails[:8]} {log[-1500:]}")
        return []
    mid = len(merged) // 2
    left = try_batch(merged[:mid], rows, depth + 1)
    if left:
        promote(left)
    return left + try_batch(merged[mid:], rows, depth + 1)


def land_verdict(merged, ids, rows, depth) -> list:
    """The one landing of a verdict that holds for the train tree."""
    if stale := superseded(merged):  # requeued in place while gated: the verdict is for code nobody queues now
        event("train_superseded", rows=[q["row"] for q in stale], shas=[q["sha"][:12] for q in stale], at="green")
        rest = [q for q in merged if q not in stale]
        return try_batch(rest, rows, depth + 1, reviewed=True) if rest else []
    finish_spec(ids)
    GREEN["head"] = git("rev-parse", "HEAD", cwd=TRAIN).stdout.strip()
    return merged


def gated_tree_intact(head) -> bool:
    """The train tree still sits at the gated `head` and main is in it."""
    return (git("rev-parse", "HEAD", cwd=TRAIN).stdout.strip() == head
            and git("merge-base", "--is-ancestor", "main", head, check=False).returncode == 0)


LAND_SUBJECT = re.compile(r"^train: land (\S+)")  # build_train's merge subject


def assert_diff_files(log: str) -> set:
    """Paths an assertion's two sides (profile assert_sides_re) name on one side only or with another value."""
    out = set()
    for left, right in (re.findall(P["assert_sides_re"], log or "", re.M) if P["assert_sides_re"] else []):
        sides = []
        for side in (left, right):
            if side.count("{") + side.count("[") + side.count("(") != side.count("}") + side.count("]") + side.count(")"):
                break
            sides.append(Counter((p.replace("\\\\", "/").replace(chr(92), "/"), (v or "").strip()) for p, v in
                                 re.findall("\"" + P["path_re"] + r"\"(?:\s*[:,]\s*([^,\"}\])\s][^,}\])]*))?", side)))
        if len(sides) == 2:
            out |= {p for p, _ in (sides[0] - sides[1]) + (sides[1] - sides[0])}
    return out


def blame_named(rows, fails, log, tree=TRAIN) -> set:
    """Files a red gate names: an assertion diff's alone, else the log's paths, failing crates, failing tests' row
    files and an unowned failing test's reads."""
    named = assert_diff_files(log)
    if named:
        return named
    named = set(m.replace(chr(92), "/") for m in re.findall(P["path_re"], log or ""))
    m = re.search(r"failing crates: ([^\n]*)", log or "")
    if m:
        named |= set(m.group(1).split())
    for r_ in rows.values():  # a failing test points at its row's declared files
        if any(t in fails for t in r_["tests"]):
            named |= set(r_["file_list"])
    for t in fails:  # an unowned test (quality, regression) points at the repo files it reads
        src = tree / t.split("::")[0]
        if "::" in t and src.is_file():
            import posixpath  # a relative import ("../x.js") resolves against the test file's folder
            named |= set(posixpath.normpath(posixpath.join(posixpath.dirname(t.split("::")[0]), p)) if p.startswith(".") else p
                         for p in re.findall(P["quoted_path_re"], src.read_text(encoding="utf-8", errors="replace")))
    return named


def own_ranges(merged, tree=TRAIN) -> dict:
    """{row: (range, cwd)}: its land merge over its first parent (a stacked row never owns its parent's change)."""
    land = {}
    for line in git("log", "--first-parent", "--format=%H%x09%s", "main..HEAD", cwd=tree, check=False).stdout.splitlines():
        c, _, subj = line.partition(chr(9))
        if (m := LAND_SUBJECT.match(subj)) and m.group(1) not in land:
            land[m.group(1)] = c
    return {q["row"]: (([f"{c}^1", c], tree) if (c := land.get(q["row"])) else ([f"main...{q['sha']}"], REPO))
            for q in merged}


def own_files(merged, tree=TRAIN) -> dict:
    """{row: files} of own_ranges."""
    return {row: set(git("diff", "--name-only", *rng, cwd=cwd, check=False).stdout.split())
            for row, (rng, cwd) in own_ranges(merged, tree).items()}


def touching(files, named) -> set:
    """files in named, a named dir/ matching all under it."""
    return {f for f in files if f in named or any(d.endswith("/") and f.startswith(d) for d in named)}


def test_lists(fails, log, tree=TRAIN) -> set:
    """Files the failing tests include (include_re, include_const_re), e.g. a shrink-only list."""
    import posixpath
    if not P["include_re"]:
        return set()
    m = re.search(r"failing crates: ([^\n]*)", log or "")
    crate_srcs = [p for d in (m.group(1).split() if m else []) for p in (tree / d).rglob(f"*{P['src_exts'][0]}")]
    out = set()
    for t in [*cf.captured(log), *(t for t in fails if "::" in t)]:
        own, name = tree / t.split("::")[0], t.rsplit("::", 1)[-1]
        for src in [own] if own.is_file() else crate_srcs:
            text = src.read_text(encoding="utf-8", errors="replace")
            fn = re.search(r"^([ \t]*)(?:\S.*?)?" + P["fn_def_re"].replace("{name}", re.escape(name)), text, re.M)
            if not fn:
                continue
            end = re.compile(rf"^{fn.group(1)}\}}", re.M).search(text, fn.end())
            body = text[fn.start():end.end() if end else len(text)]
            paths = re.findall(P["include_re"], body) + [p for c, p in re.findall(P["include_const_re"], text)
                                                         if re.search(rf"\b{c}\b", body)]
            base = src.parent.relative_to(tree).as_posix()
            out |= {posixpath.normpath(f"{base}/{p}") for p in paths}
    return out


def names_entry(diff: str, entries) -> bool:
    """A changed line of diff names an entry as a whole path."""
    return any(re.search(rf"(?<![\w/.-]){re.escape(e)}(?![\w/.-])", line[1:]) for line in diff.splitlines()
               if line[:1] in "+-" and line[:3] not in ("+++", "---") for e in entries)


def blame_entries(merged, fails, log, tree=TRAIN):
    """(rows, entries, lists): every row whose own change includes an assert-diff entry: its file or its list line."""
    entries = assert_diff_files(log)
    if not entries:
        return [], entries, set()
    lists, ranges, out = test_lists(fails, log, tree), own_ranges(merged, tree), []
    for q in merged:
        rng, cwd = ranges[q["row"]]
        files = set(git("diff", "--name-only", *rng, cwd=cwd, check=False).stdout.split())
        if touching(files, entries) or any(names_entry(git("diff", "-U0", *rng, "--", f, cwd=cwd, check=False).stdout,
                                                       entries) for f in sorted(files & lists)):
            out.append(q)
    return out, entries, lists


def blame_by_file(merged, rows, fails, log, tree=TRAIN):
    """(entries whose own files touch a named file, named, own), after blame_entries. One is ejected, more bisect."""
    named = blame_named(rows, fails, log, tree)
    if not named:
        return [], named, {}
    own = own_files(merged, tree)
    return [q for q in merged if touching(own[q["row"]], named)], named, own


def probe_culprit(merged, rows, fails):
    """The one row that alone reproduces the failing tests (bisected with only those); None for an interaction."""
    cand, t0 = list(merged), now()
    while len(cand) > 1:
        mid = len(cand) // 2
        for half in (cand[:mid], cand[mid:]):
            m, _ = build_train(half, rows)
            if m and run_tests(fails):
                cand = m
                break
        else:
            event("probe_none", rows=[q["row"] for q in cand], secs=round(now() - t0))
            return None
    event("probe_found", row=cand[0]["row"], tests=fails[:5], secs=round(now() - t0))
    return cand[0]


class UnprovenLand(RuntimeError):
    """promote refused: an entry it would land is no longer queued with its sha."""


def superseded(entries) -> list:
    """Entries no longer queued with their own sha (queue.json now): the one rule that a row lands only with its
    queued sha (build_train, try_batch, promote)."""
    queued = {(q["row"], q["sha"]) for q in load("queue.json", [])}
    return [q for q in entries if (q["row"], q["sha"]) not in queued]


def promote(landed):
    """Fast-forward main to GREEN (the last gate-verified head); no-op when there. UnprovenLand when an entry not on
    main is no longer queued with its sha."""
    head = GREEN["head"]
    before = git("rev-parse", "main").stdout.strip()
    if not head or head == before:
        return
    pending = [q for q in landed if git("merge-base", "--is-ancestor", q["sha"], before, check=False).returncode != 0]
    if stale := [q["row"] for q in superseded(pending)]:
        raise UnprovenLand(f"promote {head[:12]}: superseded {stale}")
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        git("update-ref", "refs/heads/main", head)  # main checkout dirty: move the ref only
    else:
        git("merge", "-q", "--ff-only", head)
    git("push", "-q", "origin", "main", check=False)
    event("land", rows=sorted(landing_ids(landed)), sha=head)
    tighten_rules_debt(before, head)


def tighten_rules_debt(before, head):
    """Lower the rules debt to the landed files' counts; an error is an event, never a failed land."""
    files = git("diff", "--name-only", "--no-renames", before, head, check=False).stdout.split()
    try:
        with lock("rules-debt"):
            cut = rules_gate.tighten(rules_langs.LANGS[STACK], ST, TRAIN, sorted(f for f in files if (TRAIN / f).exists()))
    except (OSError, ValueError) as e:
        event("rules_debt_error", error=str(e)[:300])
        return
    if cut:
        event("rules_debt_tightened", changes=cut[:20], n=len(cut))


TRAIN_CODE = ("harness.py", "desk.py", "quarantine.py", "rules_gate.py", "rules_langs.py")  # what the train imports


def train_code() -> dict:
    """{path: mtime} of the train's own modules: any change restarts it between trains."""
    here = Path(__file__).resolve().parent
    return {p: p.stat().st_mtime for p in (here / n for n in TRAIN_CODE) if p.exists()}


def cmd_train(a):
    missing = [v for v in P["require_env"] if not os.environ.get(v)]
    if missing:  # without it every native test fails and innocent rows are rejected
        sys.exit(f"train: {', '.join(missing)} not set (started from Git Bash?): start the train from PowerShell or cmd")
    if a.loop:  # restart and kill scripts use this pid, never a command-line pattern
        (ST / "train.pid").write_text(str(os.getpid()), encoding="utf-8")
    born = train_code()
    while True:
        if a.loop and (now_code := train_code()) != born and all(
                sh([sys.executable, "-c", f"compile(open(r'{p}',encoding='utf-8').read(),'m','exec')"], check=False).returncode == 0
                for p in now_code):  # code changed and parses: restart between trains
            print(f"{iso()} train: {', '.join(p.name for p in now_code if now_code[p] != born.get(p))} changed, re-exec", flush=True)
            os.execv(sys.executable, reexec_argv())
        try:
            did = train_once()
        except Exception as e:  # noqa: BLE001 - never let one bad train stop the loop
            deferred = isinstance(e, desk.Unavailable)
            event("desk_deferred" if deferred else "train_error", error=str(e)[-800:])
            print(f"{iso()} train {'deferred, desk' if deferred else 'error'}: {str(e)[-400:]}", flush=True)
            for n in ("train", "state"):
                release_lock(n)
            if deferred and a.loop:
                time.sleep(DESK_RETRY_SECS)
            did = False
        if not a.loop:
            return
        if not did:
            time.sleep(30)


DESK_RETRY_SECS = 300
MIN_BATCH = int(os.environ.get("HARNESS_MIN_BATCH", "8"))
BATCH_WAIT_BUSY = int(os.environ.get("HARNESS_BATCH_WAIT_BUSY", str(25 * 60)))  # max wait for MIN_BATCH while claims are in flight
BATCH_WAIT = 600
# bigger batches land less: one bad row or clash per ~10 rows
BATCH_CAP = int(os.environ.get("HARNESS_BATCH_CAP", "20"))


FILL_MIN = int(os.environ.get("HARNESS_FILL_MIN", "5"))  # 5 / 10 min fit a ~25-min gate cycle; ~45 min: 8 / 20
FILL_WAIT = int(os.environ.get("HARNESS_FILL_WAIT", str(10 * 60)))  # 0 = off
FILL_GAP_MIN = 300  # a short FILL_WAIT must not restart a hold
FILLING = "filling: "
CLOUD_STATE = "cloud.json"  # owned here, written by cloud.py


def fill_hold(clean: int, out: int, fix_row: bool, since: float | None, t: float) -> bool:
    """Hold the train: few clean picks, rows out for rebase, no CLEAN-* pick, hold under FILL_WAIT old."""
    if FILL_WAIT <= 0 or fix_row or not out or clean >= FILL_MIN:
        return False
    return since is None or t - since < FILL_WAIT


def fill_since(events: list, t: float) -> float | None:
    """Start of the fill hold running at t, else None; a train_start, other idle or a long gap ends it."""
    since = last = None
    gap = max(FILL_WAIT, FILL_GAP_MIN)
    for e in events:
        kind = e.get("event")
        if kind == "train_idle" and str(e.get("reason", "")).startswith(FILLING):
            if since is None or e["ts"] - last > gap:
                since = e["ts"]
            last = e["ts"]
        elif kind in ("train_start", "train_idle"):
            since = last = None
    return since if last is not None and t - last <= gap else None


def rebase_out(queue: list, picks: list, sent: dict) -> int:
    """Queued rows, not picked, whose current sha cloud.py sent to the rebasers."""
    picked = {q["sha"] for q in picks}
    return sum(1 for q in queue if q["sha"] not in picked and sent.get(q["row"]) == q["sha"])


def fill_wait(queue: list, picks: list) -> bool:
    """True, logging a train_idle, while fill_hold() holds the train."""
    t, events = now(), read_events()
    sent = {r: v.get("rebase_sent") for r, v in load(CLOUD_STATE, {"rows": {}}).get("rows", {}).items()}
    out = rebase_out(queue, picks, sent)
    fix_row = any(q["row"].startswith(quarantine.FIX_PREFIX) for q in picks)
    if not fill_hold(len(picks), out, fix_row, fill_since(events, t), t):
        return False
    event("train_idle", queued=len(queue), reason=f"{FILLING}{len(picks)} clean, {out} out for rebase")
    return True


SKIP_PARENT = "pick-skip-parent.json"  # {row: sha}: queued rows whose pick_skip_parent is already logged


def stacked_on(picks: list, blockers: dict, base: str = "main") -> list:
    """[(q, parent sha)] for picks whose sha carries a blocker's unlanded commit (base..q): landing q would land the
    parent's rejected or unjudged code. Exempt: a blocker that is a pick, or whose row a pick lands (lands_rows): the
    child rides with its parent. A skipped pick blocks its own descendants and re-lands none of its row's shas
    (fixpoint). Rejected parent first."""
    left, out = list(picks), []
    carried = {q["sha"]: set(git("rev-list", f"{base}..{q['sha']}").stdout.split()) for q in picks}
    while True:
        picked = {q["sha"] for q in left}
        relands = {s for p in left for s in carried[p["sha"]] & blockers.keys() if blockers[s][0] in lands_rows(p)}
        hits = []
        for q in left:
            par = sorted((not blockers[s][1], blockers[s][0], s) for s in carried[q["sha"]] & blockers.keys()
                         if s not in picked and s not in relands)
            if par:
                hits.append((q, par[0][2]))
        if not hits:
            return out
        out += hits
        left = [q for q in left if all(q is not x for x, _ in hits)]


def pick_blockers(picks: list) -> dict:
    """{sha: (row, rejected)} picks must not carry: every rejected sha (rejects.json) and every queued row
    (queue.json, not the filtered pick copy) outside the picks."""
    picked = {q["sha"] for q in picks}
    out = {r["sha"]: (row, True) for row, rs in load("rejects.json", {}).items() for r in rs if r.get("sha")}
    out.update({q["sha"]: (q["row"], False) for q in load("queue.json", []) if q["sha"] not in picked})
    return out


def skip_stacked(picks: list) -> list:
    """The picks stacked_on a blocker: they stay queued until the parent lands or they are restacked. Each (row, sha)
    logs pick_skip_parent once."""
    blockers = pick_blockers(picks)
    hits = stacked_on(picks, blockers)
    with lock("state"):
        seen, live = load(SKIP_PARENT, {}), {q["row"]: q["sha"] for q in load("queue.json", [])}
        for q, p in hits:
            if seen.get(q["row"]) != q["sha"]:
                seen[q["row"]] = q["sha"]
                event("pick_skip_parent", row=q["row"], sha=q["sha"][:12], parent=blockers[p][0], parent_sha=p[:12],
                      rejected=blockers[p][1])
        save(SKIP_PARENT, {r: sha for r, sha in seen.items() if sha == live.get(r)})
    return [q for q, _ in hits]


MERGE_DRIVERS = {  # name -> (description, command) of each .gitattributes driver
    "lineset": ("three-way merge of a line-set data file (fleet-train harness)",
                f"{PY} {(HOME / 'lineset_merge.py').as_posix()} %O %A %B %P"),
    "keyed": ("three-way merge of a keyed spec file by row, bullet or [[test]] entry (fleet-train harness)",
              f"{PY} {(HOME / 'keyed_merge.py').as_posix()} %O %A %B %L %P"),
}


def ensure_merge_drivers() -> None:
    """Register every MERGE_DRIVERS driver in the repo config, shared by every local merge and merge-tree. Another
    clone runs the same `git config` lines once (SKILL.md)."""
    for name, (description, command) in MERGE_DRIVERS.items():
        if git("config", "--get", f"merge.{name}.driver", check=False).stdout.strip() != command:
            git("config", f"merge.{name}.name", description)
            git("config", f"merge.{name}.driver", command)


def disjoint(queue: list) -> list:
    """Oldest first, the rows that merge cleanly onto main plus every earlier pick (git merge-tree). Fixed point: a row
    stays a candidate while candidates land each unproven row it carries (lands_rows), so a stack goes whole or waits
    (state/pick-held.json)."""
    ensure_merge_drivers()
    main = git("rev-parse", "main").stdout.strip()
    spec = main_spec()
    proven = {i for i, r in spec.items() if r["status"] == "proven"}
    dep = [carried_rows(q, spec) - proven for q in queue]
    dropped = set()

    def stable(cand: set) -> set:
        while bad := {i for i in cand if dep[i] - landing_ids(queue[j] for j in cand)}:
            cand = cand - bad
        return cand

    while True:
        cand = stable(set(range(len(queue))) - dropped)
        acc, out, clashed = main, [], set()
        for i in sorted(cand):
            r = git("merge-tree", "--write-tree", acc, queue[i]["sha"], check=False)
            if r.returncode == 1:  # conflicts: next batch
                clashed.add(i)
                continue
            if r.returncode != 0 or not r.stdout.split():
                return _disjoint_files(queue)
            c = git("commit-tree", r.stdout.split()[0], "-p", acc, "-p", queue[i]["sha"], "-m", "train pick",
                    check=False).stdout.strip()
            if not c:
                return _disjoint_files(queue)
            acc = c
            out.append(queue[i])
        if stable(cand - clashed) == cand - clashed:
            break
        dropped |= clashed
    provided = landing_ids(out)
    save("pick-held.json", {queue[i]["row"]: sorted(dep[i] - provided) for i in range(len(queue)) if dep[i] - provided})
    return out


def lands_rows(q) -> list:
    """The rows queue entry q lands: its own, then its `absorbs`; the one answer for the hold rule, frontier, gate's
    new rows, finish_spec and land event. In a train gate_rows adds `proves` (memory only): rows the branch's spec marks
    proven and main's does not."""
    return [q["row"], *q.get("absorbs", []), *q.get("proves", [])]


def landing_ids(entries) -> set:
    """Every row the queue entries land (lands_rows)."""
    return {i for q in entries for i in lands_rows(q)}


def absorbs_unverified(sha: str, absorbs) -> list:
    """`absorbs` rows sha does not verify: it must carry commits under the row's subject (main..sha) and, if h/<row>
    exists, descend from its tip."""
    if not absorbs:
        return []
    spec = {**main_spec(), **parse_spec(git("show", f"{sha}:{SPEC}", check=False).stdout)}
    carried = carried_rows({"row": "", "sha": sha}, spec)
    bad = []
    for r in absorbs:
        tip = git("rev-parse", "--verify", "-q", f"h/{r}", check=False).stdout.strip()
        if r not in carried or (tip and git("merge-base", "--is-ancestor", tip, sha, check=False).returncode != 0):
            bad.append(r)
    return bad


def gate_rows(entries, rows) -> list:
    """Fill rows (main's spec) with each row the entries land that main's spec lacks: a CLEAN-* row as a test-less
    stub, an absorbed spec row from the entry's spec as "absorbed" (gated, then marked proven by finish_spec). Rows the
    branch marks proven and main does not (q["proves"]) are gated too. Returns [(entry, why)] for entries landing a spec
    row with no acceptance tests."""
    bad = []
    for q in entries:
        q.pop("proves", None)
        branch = parse_spec(git("show", f"{q['sha']}:{SPEC}", check=False).stdout)
        own = set(lands_rows(q))
        q["proves"] = sorted(i for i, r in branch.items() if r["status"] == "proven" and i not in own
                             and rows.get(i, {}).get("status") != "proven")
        for i in lands_rows(q):
            if i.startswith("CLEAN-"):
                rows.setdefault(i, {"id": i, "tests": [], "status": "cleanup", "file_list": [], "deps": [], "module": "cleanup"})
                continue
            if i == q["row"]:
                continue
            if i not in rows and i in branch:
                rows[i] = dict(branch[i], status="absorbed")
            if not rows.get(i, {}).get("tests"):
                bad.append((q, f"landed spec row {i} has no acceptance tests in {SPEC} (main or the branch): it cannot be proven"))
                break
    return bad


def carried_rows(q, spec: dict) -> set:
    """Rows other than q's own (lands_rows) whose unlanded commits q's branch carries, by subject (`<row id>:` or
    `stack: <row id>`); landing without them fails the plan check."""
    subj = git("log", "--no-merges", "--format=%s", f"main..{q['sha']}", check=False).stdout
    return {i for i in subject_ids(subj) if i in spec or i.startswith("CLEAN-")} - set(lands_rows(q))


def subject_ids(subj: str) -> list:
    """The row ids commit subjects name (`<row id>: ...` or `stack: <row id>`), first seen first."""
    return list(dict.fromkeys(m.group(1).rstrip(":") for m in re.finditer(r"^(?:stack: )?(\S+)", subj, re.M)))


def own_spec_rows(q: dict, queue: list) -> list:
    """The spec rows q's own commits (main..sha minus those of queued entries it stacks on) carry that no other entry
    lands: q should absorb them. None when they still carry another CLEAN-* row (an unqueued parent's rows are its own
    to absorb). Kept: rows main does not mark proven, no other entry lands (landing_ids) and absorbs_unverified passes."""
    sha, others = q["sha"], [o for o in queue if o["row"] != q["row"]]
    mine = set(git("rev-list", f"main..{sha}", check=False).stdout.split())
    own = lambda under: subject_ids(git("log", "--reverse", "--no-merges", "--format=%s", sha, "--not", "main", *under,
                                        check=False).stdout)
    under = [o["sha"] for o in others if o["sha"] in mine]
    found = own(under)
    parents = [o["sha"] for o in others if o["row"] in found and o["sha"] not in under]
    if parents:
        found = own(under + parents)
    taken = landing_ids(others) | set(lands_rows(q))
    ids = [i for i in found if ROW_RE.match(i) and i not in taken]  # a spec row id (parse_spec's shape)
    if not ids or any(i.startswith("CLEAN-") and i != q["row"] for i in found):
        return []
    main = main_spec()
    spec = {**main, **parse_spec(git("show", f"{sha}:{SPEC}", check=False).stdout)}
    ids = [i for i in ids if i in spec and main.get(i, {}).get("status") != "proven"]
    bad = absorbs_unverified(sha, ids)
    return [i for i in ids if i not in bad]


def absorb_own_rows(q: dict, queue: list) -> list:
    """Add own_spec_rows to q's absorbs and log absorbs_auto (caller holds the state lock, saves the queue): the one
    writer of automatic absorbs; the ids added."""
    add = own_spec_rows(q, queue)
    if add:
        q["absorbs"] = [*q.get("absorbs", []), *add]
        event("absorbs_auto", row=q["row"], ids=add)
    return add


def _disjoint_files(queue: list) -> list:
    """Oldest first, the rows whose changed files overlap no earlier pick."""
    taken, out = set(), []
    for q in queue:
        files = set(git("diff", "--name-only", f"main...{q['sha']}", check=False).stdout.split())
        if files & taken:
            continue
        out.append(q)
        taken |= files
    return out


# ---------------------------------------------------------------- pick check: no-build checks before a row enters a batch
PICK_WT = WT / "pick"  # the one compile worktree (warm target)
PICK_CACHE = "pick-verdicts.json"  # {sha: {row, base, checks, built, reviewed}}: static pass per sha, compile per main
PICK_CHECKS = 2  # bump when the static pick checks change: older passes run again
PICK_IDLE_SECS = 120
PICK_BUILD_TIMEOUT = int(os.environ.get("HARNESS_PICK_BUILD_TIMEOUT", "600"))
PICK_BUILD_RED = "pick check: gate red (['unit']): the unit stage's compile fails on main + this row: "
PICK_PARALLEL = int(os.environ.get("HARNESS_PICK_PARALLEL", "3"))  # rows pick_screen checks at once
PICK_BUILD_BASE = "pick-build-base.json"  # {base sha: compiles}
PICK_BACKGROUND_PID = "pick-background.pid"  # priority_keeper.ps1 reads it
PICK_BUILD_LOCK = threading.Lock()


class NoVerdict(Exception):
    pass


@dataclass(frozen=True)
class PickSlot:
    """One row's worktree for regen, static checks and review; the compile stays on PICK_WT (one warm target)."""
    wt: Path


def pick_slot(i: int) -> PickSlot:
    return PickSlot(WT / f"pick-static-{i}")


def pick_checkout(commit: str, wt: Path = PICK_WT) -> None:
    if not (wt / ".git").exists():
        git("worktree", "add", "-q", "--detach", str(wt), commit)
    else:
        git("checkout", "-q", "-f", "--detach", commit, cwd=wt)
        git("clean", "-q", "-f", "-d", cwd=wt)


def merged_commit(base: str, sha: str) -> str | None:
    r = git("merge-tree", "--write-tree", base, sha, check=False)
    if r.returncode != 0 or not r.stdout.split():
        return None
    return git("commit-tree", r.stdout.split()[0], "-p", base, "-p", sha, "-m", "pick check").stdout.strip()


def main_static_clean(base: str, cache: dict) -> bool:
    """A main failing the static checks itself rejects no row."""
    if cache.get("_main", {}).get("sha") != base:
        wt = pick_slot(0).wt
        pick_checkout(base, wt)
        cache["_main"] = {"sha": base, "clean": not static_problems(wt, base, plan="off")}
    return cache["_main"]["clean"]


def proc_start(pid: int) -> str:
    """pid's start time ('' if gone), from overhaul-build's drivelock."""
    if QS not in sys.path:
        sys.path.insert(0, QS)
    import drivelock
    return drivelock.proc_start(pid) or ""


def in_acceptance() -> bool:
    mark = load(TRAIN_STAGE, {})
    return mark.get("stage") == "acceptance" and bool(mark.get("pid")) and pid_alive(mark["pid"])


def pick_compile(commit: str, background: bool = False) -> tuple:
    """(ok, error lines) of P["pick_build"] at `commit` in PICK_WT; NoVerdict on a timeout, and in acceptance unless
    background (PICK_BACKGROUND_PID)."""
    if not background and in_acceptance():
        raise NoVerdict("the train is in its acceptance stage")
    pick_checkout(commit)
    mark = ST / PICK_BACKGROUND_PID
    try:
        r = sh(P["pick_build"], cwd=PICK_WT, check=False, env=run_env(stack_env(P["pick_env"])), timeout=PICK_BUILD_TIMEOUT,
               low=background, on_start=(lambda pid: mark.write_text(str(pid), encoding="utf-8")) if background else None)
    except subprocess.TimeoutExpired:
        raise NoVerdict(f"pick build over {PICK_BUILD_TIMEOUT} s") from None
    finally:
        if background:
            mark.unlink(missing_ok=True)
    out, blocks, cur = r.stdout + r.stderr, [], None
    for line in out.splitlines():
        if P["pick_err_re"] and re.match(P["pick_err_re"], line):
            cur = [line]
            blocks.append(cur)
        elif cur is not None and line.strip():
            cur.append(line)
        else:
            cur = None
    return r.returncode == 0, chr(10).join(chr(10).join(b) for b in blocks) or out[-1500:]


def pick_build(base: str, merged: str, background: bool = False) -> str:
    """'' or the compile errors of `merged` that `base` lacks; one compile at a time."""
    if not P["pick_build"]:
        return ""
    with PICK_BUILD_LOCK:
        t0 = now()
        ok, errs = pick_compile(merged, background)
        event("pick_compile", ok=ok, secs=round(now() - t0, 1), **({"background": True} if background else {}))
        if ok:
            return ""
        built = load(PICK_BUILD_BASE, {})
        if base not in built:
            built = {**dict(list(built.items())[-3:]), base: pick_compile(base, background)[0]}
            save(PICK_BUILD_BASE, built)
    return PICK_BUILD_RED + errs[-3000:] if built[base] else ""


def compiled_for(entry: dict, base: str) -> bool:
    """The cached compile holds on `base` (code_same)."""
    built = entry.get("built")
    return bool(built) and (built == base or code_same(built, base))


def prescreened(entry: dict) -> bool:
    """The static pass and review are cached."""
    return entry.get("checks") == PICK_CHECKS and bool(entry.get("reviewed"))


def passed(entry: dict, base: str) -> bool:
    """The whole pick check is cached for `base`."""
    return prescreened(entry) and compiled_for(entry, base)


def pick_verdict(q: dict, base: str, cache: dict, slot: PickSlot | None = None, build: bool = True,
                 background: bool = False) -> str:
    """'' or the reject reason of queued row q on commit `base` (a conflict is ''); raises ReviewUnavailable.
    Checks run in `slot`; build=False: no compile."""
    merged = merged_commit(base, q["sha"])
    if merged is None:
        return ""
    entry, wt = cache.setdefault(q["sha"], {"row": q["row"]}), (slot or pick_slot(0)).wt
    pick_checkout(merged, wt)
    if entry.get("checks") != PICK_CHECKS:
        # regenerate first, as the train does: its fixable drift never rejects
        log = chr(10).join(x for x in (regen_tree(wt), static_problems(wt, base, plan="off"),
                                       *rules_gate.row_plan_problems(wt, base, WT, (q["row"],))) if x)
        if log:
            return "pick check: gate red (['quality']): " + log[-1500:]
        entry["base"], entry["checks"] = base, PICK_CHECKS
    if build and not compiled_for(entry, base):
        why = pick_build(base, merged, background)
        if why:
            return why
        entry["built"] = base
    if not entry.get("reviewed"):
        why = dup_reason(q, batch_review([q], cwd=wt, base=base))
        if why:
            return "pick check: " + why
        entry["reviewed"] = True
    return ""


# Pick acceptance (`harness.py pick-accept`, on while <state>/pick-accept-on exists): each queued row's owner_tests on
# the desk, merged onto what the next pick sees. Never during acceptance (desk.pick_stop). pick_screen rejects a red
# verdict while main's code equals its base; no verdict leaves the row to the gate.
PICK_ACCEPT_ON = "pick-accept-on"
PICK_ACCEPT = "pick-accept.json"  # {sha: {row, base, ok, fails, tests, log, secs, send_secs}}
PICK_ACCEPT_PID = "pick-accept.pid"


def code_same(a: str, b: str) -> bool:
    r = git("diff", "--name-only", a, b, check=False)
    return r.returncode == 0 and all(f == SPEC for f in r.stdout.split())


def pick_accept_red(sha: str, base: str) -> str:
    v = load(PICK_ACCEPT, {}).get(sha) if (ST / PICK_ACCEPT_ON).exists() else None
    if not v or v["ok"] or not code_same(v["base"], base):
        return ""
    return (f"pick check: gate red ({v['fails'][:5]}) in acceptance on main + this row (desk, main {v['base'][:12]}, "
            f"{len(v['tests'])} tests of rows sharing your files): " + v["log"][-1200:])


def pick_accept_base() -> str:
    for e in reversed(read_events()[-3000:]):
        if e.get("event") == "train_end":
            break
        if e.get("event") == "train_start" and (TRAIN / ".git").exists():
            return git("rev-parse", "HEAD", cwd=TRAIN).stdout.strip()
    return git("rev-parse", "main").stdout.strip()


def pick_accept_check(q: dict, base: str, run=None) -> dict | None:
    """Row q's verdict on `base`; None on a conflict, a row already in base, or no verdict."""
    merged = merged_commit(base, q["sha"])
    if merged is None or git("merge-base", "--is-ancestor", q["sha"], base, check=False).returncode == 0:
        return None
    files = git("diff", "--name-only", "--no-renames", base, merged).stdout.split()
    kr = known_red_tests()
    tests = [t for t in owner_tests(parse_spec(git("show", f"{base}:{SPEC}").stdout), files) if t not in kr]
    v, t0 = {"row": q["row"], "base": base, "ok": True, "fails": [], "tests": tests, "log": "", "secs": 0,
             "send_secs": 0}, now()
    if tests:
        try:
            res = (run or (lambda c, ts: desk.pick_run(sys.modules[__name__], c, ts)))(merged, tests)
        except NoVerdict as e:
            event("pick_accept", row=q["row"], ok=None, why=str(e)[-200:], tests=len(tests))
            return None
        fails = sorted(set(res.get("fails") or []) - kr)
        v.update(ok=not fails, fails=fails, log=res.get("log", "") if fails else "", send_secs=res["send_secs"],
                 secs=round(now() - t0))
    event("pick_accept", row=q["row"], ok=v["ok"], tests=len(tests), fails=v["fails"][:5], secs=v["secs"],
          send_secs=v["send_secs"])
    return v


def pick_accept_queue(max_rows: int = 6, check=pick_accept_check) -> int:
    """Queued rows with no verdict on pick_accept_base(), oldest first, one desk run at a time; rows checked."""
    lock_f = ST / PICK_ACCEPT_PID
    held = lock_f.read_text().strip() if lock_f.exists() else ""
    if not (ST / PICK_ACCEPT_ON).exists() or (held.isdigit() and pid_alive(int(held))):
        return 0
    lock_f.write_text(str(os.getpid()))
    try:
        n = 0
        for q in sorted(load("queue.json", []), key=lambda x: x["ts"]):
            if n >= max_rows or in_acceptance():
                break
            base, have = pick_accept_base(), load(PICK_ACCEPT, {}).get(q["sha"])
            if q.get("proof") or (have and code_same(have["base"], base)):
                continue
            got = check(q, base)
            live = {x["sha"] for x in load("queue.json", [])}
            v = {k: x for k, x in load(PICK_ACCEPT, {}).items() if k in live}
            if got is not None:
                v[q["sha"]], n = got, n + 1
            save(PICK_ACCEPT, v)
        return n
    finally:
        lock_f.unlink(missing_ok=True)


def gate_reject(picks: list, rows: dict) -> set:
    """Reject and dequeue the `picks` gate_rows refuses (a landed spec row with no acceptance tests); their shas."""
    bad = gate_rows(picks, rows)
    for q, why in bad:
        reject(q, why)
    return {q["sha"] for q, _ in bad}


def pick_screen(picks: list, budget: float = float("inf"), workers: int = PICK_PARALLEL) -> set:
    """Reject and dequeue, in pick order, the `picks` failing the pick check; their shas."""
    cache = load(PICK_CACHE, {})
    base = git("rev-parse", "main").stdout.strip()
    bad = {q["sha"]: why for q in picks if (why := pick_accept_red(q["sha"], base))}  # before the cached-pass skip
    todo = [q for q in picks if q["sha"] not in bad and not passed(cache.get(q["sha"], {}), base)]
    if todo and main_static_clean(base, cache):
        bad.update(pick_rows(todo, base, cache, now() + budget, workers))
    with lock("state"):
        for q in picks:
            if q["sha"] in bad:
                reject(q, bad[q["sha"]], locked=True)
        queue = load("queue.json", [])
    live = {q["sha"] for q in queue} | {"_main"}
    save(PICK_CACHE, {k: v for k, v in cache.items() if k in live})
    return set(bad)


def pick_rows(todo: list, base: str, cache: dict, deadline: float, workers: int) -> dict:
    """{sha: reason} of the failing `todo` rows, `workers` at once, each in a PickSlot on a copy of its cache entry;
    only this thread writes `cache`, saved per verdict."""
    slots = [pick_slot(i) for i in range(max(1, min(workers, len(todo))))]
    for s in slots:  # serially: parallel worktree adds race
        if not (s.wt / ".git").exists():
            pick_checkout(base, s.wt)
    free = SimpleQueue()
    for s in slots:
        free.put(s)
    own = {q["sha"]: ({q["sha"]: dict(cache[q["sha"]])} if q["sha"] in cache else {}) for q in todo}

    def check(q: dict) -> tuple:
        """(started, reason) of row q in a free slot; an error is no verdict."""
        if now() > deadline:
            return False, ""
        slot = free.get()
        try:
            return True, pick_verdict(q, base, own[q["sha"]], slot)
        except Exception as e:  # noqa: BLE001 - no verdict
            event("pick_error", row=q["row"], error=f"{type(e).__name__}: {str(e)[-300:]}")
            return False, ""
        finally:
            free.put(slot)

    bad = {}
    with ThreadPoolExecutor(len(slots), thread_name_prefix="pick") as ex:
        futures = {ex.submit(check, q): q for q in todo}
        for f in as_completed(futures):
            q = futures[f]
            started, why = f.result()
            if why:
                bad[q["sha"]] = why
                cache.pop(q["sha"], None)
            if q["sha"] in own[q["sha"]] and not why:
                cache[q["sha"]] = own[q["sha"]][q["sha"]]
            if started:
                event("pick_check", row=q["row"], ok=not why)
            save(PICK_CACHE, cache)
    return bad


PRESCREEN_POLL = 60  # s between queue looks


def owes_prescreen(entry: dict, head: str) -> bool:
    """Static checks and review owed, or (head set) the compile on head."""
    return not prescreened(entry) or bool(head) and not compiled_for(entry, head)


def prescreen(skip, stop, poll: float = PRESCREEN_POLL, accepting=None, head=None) -> int:
    """Warm the pick cache while a batch gates: rows outside `skip` that owe it, oldest first, until `stop`: static
    checks and review on main, in acceptance the compile on the batch head. Never rejects or locks. Rows passed."""
    accepting, head = accepting or in_acceptance, head or pick_accept_base
    tried, red, n = set(), set(skip), 0  # tried: (sha, compile base)
    while not stop.is_set():
        try:
            build = accepting()
            base = head() if build else git("rev-parse", "main").stdout.strip()
            at = base if build else ""
            cache = load(PICK_CACHE, {})
            q = next((x for x in sorted(load("queue.json", []), key=lambda x: x["ts"]) if x["sha"] not in red
                      and (x["sha"], at) not in tried and owes_prescreen(cache.get(x["sha"], {}), at)), None)
            if q is None:
                stop.wait(poll)
                continue
            tried.add((q["sha"], at))
            t0 = now()
            why = pick_verdict(q, base, cache, pick_slot(0), build=build, background=True)
            if why:
                cache.pop(q["sha"], None)
                red.add(q["sha"])
            n += not why
            event("pick_prescreen", row=q["row"], ok=not why, secs=round(now() - t0, 1), **({"built": at[:12]} if at else {}))
            live = {x["sha"] for x in load("queue.json", [])} | {"_main"}
            save(PICK_CACHE, {k: v for k, v in cache.items() if k in live})
        except Exception as e:  # noqa: BLE001 - no verdict
            event("pick_prescreen", ok=None, error=f"{type(e).__name__}: {str(e)[-300:]}")
            stop.wait(poll)
    return n


@contextmanager
def prescreening(batch: list):
    """prescreen in a thread while `batch` gates; joined after the land."""
    stop = threading.Event()
    t = threading.Thread(target=prescreen, args=({q["sha"] for q in batch}, stop), name="prescreen", daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()


def train_once():
    with lock("train", wait=5):
        with lock("state"):
            queue = load("queue.json", [])
        if not queue:
            return False
        rows = main_spec()
        proven = {i for i, r in rows.items() if r["status"] == "proven"}
        if any(q["row"] in proven for q in queue):
            with lock("state"):
                queue = [q for q in load("queue.json", []) if q["row"] not in proven]
                save("queue.json", queue)
        if (ST / "clean-only").exists():  # owner: land the cleanup before anything else
            queue = [q for q in queue if q["row"].startswith("CLEAN-")]
            if not queue:
                return False
        # a gate costs ~15-20 min at any size: while builders hold claims wait up to BATCH_WAIT_BUSY for MIN_BATCH
        # rows; with no claims nothing more is coming: go now
        in_flight = len(load("claims.json", {}))
        wait = BATCH_WAIT_BUSY if in_flight else 0
        queue = sorted(queue, key=lambda q: q["ts"])
        if not queue or (len(queue) < MIN_BATCH and now() - queue[0]["ts"] < wait):
            pick_screen(queue, PICK_IDLE_SECS)
            return False
        queue = quarantine.train_queue(sys.modules[__name__], queue)  # a cap pause leaves only CLEAN-* rows
        if not queue:
            return False
        batch = disjoint(queue)[:BATCH_CAP]
        skipped = []
        while batch:  # skip a pick on a rejected or unpicked parent, screen, gate_rows; each drop re-picks
            if stacked := skip_stacked(batch):
                skipped += stacked
                bad = {q["sha"] for q in stacked}
            elif not (bad := pick_screen(batch) or gate_reject(batch, rows)):
                break
            queue = [q for q in queue if q["sha"] not in bad]
            batch = disjoint(queue)[:BATCH_CAP]
        if not batch:  # every queued row conflicts: normal conflict handling, then idle (never an empty train)
            for q in queue:  # skipped rows already left this list: they stay queued for their parents
                reject(q, "merge conflict with main; merge main into your branch and resolve")
            ride = f"; {len(skipped)} ride on a rejected or queued parent" if skipped else ""
            event("train_idle", queued=len(load("queue.json", [])), reason=f"no row merges cleanly with main{ride}")
            return False
        if fill_wait(queue, batch):
            return False
        t0 = now()
        event("train_start", rows=[q["row"] for q in batch])
        GREEN["head"] = ""
        with prescreening(batch):
            return land_batch(batch, rows, t0)


def land_batch(batch: list, rows: dict, t0: float) -> bool:
    """Gate, promote and dequeue the picked batch (the train lock is held)."""
    try:
        landed = try_batch(batch, rows)
    except ReviewUnavailable as e:  # never land unreviewed: keep every row queued, retry the batch later
        event("review_deferred", rows=[q["row"] for q in batch], error=str(e)[-300:])
        print(f"{iso()} train: reviewer unavailable, batch deferred", flush=True)
        time.sleep(300)  # the loop calls train_once again; nothing was removed from the queue
        return True
    except quarantine.MainBroken as e:  # rows stay queued
        print(f"{iso()} train: {e}", flush=True)
        return True
    if landed:
        promote(landed)
    with lock("state"):
        queue = load("queue.json", [])
        done = {q["sha"] for q in batch}
        save("queue.json", [q for q in queue if q["sha"] not in done])
    event("train_end", landed=sorted(landing_ids(landed)), tried=len(batch), secs=round(now() - t0))
    for gen in ("dash.py",):  # dashboards are regenerated by the train, never by memory
        if (HOME / gen).exists():
            # detached: a slow dashboard inside the train lock blocked every next batch
            subprocess.Popen([*PY.split(), str(HOME / gen)], cwd=HOME, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=NO_WINDOW)
    print(f"{iso()} train: landed {len(landed)}/{len(batch)} in {round(now() - t0)}s", flush=True)
    quarantine.release(sys.modules[__name__])
    return True


def read_events() -> list:
    """events.jsonl, skipping blank or half-written lines."""
    out = []
    if (ST / "events.jsonl").exists():
        for line in open(ST / "events.jsonl", encoding="utf-8"):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def cmd_status(a):
    ev = read_events()
    t = now()
    landed = lambda h: len({r for e in ev if e["event"] == "land" and t - e["ts"] < h * 3600 for r in e["rows"]})
    rows = main_spec()
    proven = sum(1 for r in rows.values() if r["status"] == "proven")
    print(json.dumps({"landed_1h": landed(1), "landed_3h": landed(3), "proven": proven, "total": len(rows),
                      "claimed": len(load("claims.json", {})), "queued": len(load("queue.json", [])),
                      "rejects_1h": sum(1 for e in ev if e["event"] == "reject" and t - e["ts"] < 3600)}))


LOG_DIR = Path(os.environ.get("HARNESS_LOG_DIR", str(HOME / "notes" / "log")))
NOTE_MAX_CHARS = 300


def append_note(text: str, at: datetime | None = None) -> Path:
    """Append one timestamped line to this month's log without reading it."""
    line = " ".join(text.split())
    if not line:
        raise ValueError("empty note")
    if len(line) > NOTE_MAX_CHARS:
        raise ValueError(f"note is {len(line)} chars, cap {NOTE_MAX_CHARS}: put details in a hand-back or commit")
    at = at or datetime.now(timezone.utc)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{at:%Y-%m}.md"
    head = "" if path.exists() else f"# Harness log, {at:%Y-%m}\nAppend-only: `harness.py note \"<text>\"`.\n\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{head}- {at:%Y-%m-%d %H:%M}Z {line}\n")
    return path


def cmd_note(a):
    try:
        print(append_note(a.text))
    except ValueError as e:
        sys.exit(f"note refused: {e}")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("frontier")
    p = sp.add_parser("claim"); p.add_argument("--slot", type=int, required=True); p.add_argument("--wait", type=float, default=0)
    p = sp.add_parser("submit"); p.add_argument("--slot", type=int, required=True); p.add_argument("--row", required=True)
    p.add_argument("--replace", action="store_true", help="replace another slot's queued sha this one does not contain")
    p = sp.add_parser("release"); p.add_argument("--slot", type=int, required=True); p.add_argument("--row", required=True); p.add_argument("--reason", required=True)
    p = sp.add_parser("train"); p.add_argument("--loop", action="store_true")
    sp.add_parser("status")
    p = sp.add_parser("quality-check"); p.add_argument("--dir", default=""); p.add_argument("--base", default="")
    p.add_argument("--regen", action="store_true", help="run the profile's regen commands first, as the train does")
    p = sp.add_parser("clean-start"); p.add_argument("--slot", type=int, required=True); p.add_argument("--name", required=True)
    p = sp.add_parser("self-review"); p.add_argument("--slot", type=int, required=True)
    p = sp.add_parser("review-prompt", help="print the duplicate reviewer's prompt for a tree's added functions")
    p.add_argument("--dir", default=""); p.add_argument("--base", default="")
    p = sp.add_parser("unit-baseline", help="write main's unit failures for a tree checked out at main")
    p.add_argument("--dir", required=True); p.add_argument("--out", required=True)
    p = sp.add_parser("unit-check", help="the train's unit verdict on a tree against a unit-baseline file")
    p.add_argument("--dir", required=True); p.add_argument("--baseline", required=True)
    p = sp.add_parser("clean-submit"); p.add_argument("--slot", type=int, required=True); p.add_argument("--name", required=True)
    p.add_argument("--absorbs", default="", help="row ids this integration row lands with it, comma separated; each is "
                   "verified in the branch")
    p.add_argument("--front", action="store_true", help="queue ahead of every row (a fix the owner wants first)")
    p.add_argument("--replace", action="store_true", help="replace another slot's queued sha this one does not contain")
    sp.add_parser("note", help="append one line to the dated log").add_argument("text")
    sp.add_parser("pick-accept", help="desk acceptance of queued rows (on: state/pick-accept-on)")
    a = ap.parse_args()
    {"frontier": cmd_frontier, "claim": cmd_claim, "submit": cmd_submit, "release": cmd_release,
     "train": cmd_train, "status": cmd_status, "note": cmd_note, "pick-accept": lambda a: print(pick_accept_queue()),
     "clean-start": cmd_clean_start, "self-review": cmd_self_review, "review-prompt": cmd_review_prompt,
     "unit-baseline": cmd_unit_baseline, "unit-check": cmd_unit_check,
     "quality-check": cmd_quality_check, "clean-submit": cmd_clean_submit}[a.cmd](a)


if __name__ == "__main__":
    main()
