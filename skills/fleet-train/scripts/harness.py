"""fleet-train harness v2: claim / submit / release for builder agents, and a batch land train.

No LLM in here. Builders are Claude Code Workflow agents; this file only hands out rows,
keeps per-slot worktrees warm, and lands finished branches in batches (merge all, gate once,
bisect on red).

  py -3 harness.py frontier            # JSON summary of buildable rows
  py -3 harness.py claim --slot N [--wait S]
  py -3 harness.py submit --slot N --row R
  py -3 harness.py release --slot N --row R --reason "..."
  py -3 harness.py train [--loop]      # land queued branches
  py -3 harness.py status              # one-line rate
  py -3 harness.py note "<text>"       # append one line to the dated log (<HARNESS_HOME>/notes/log/YYYY-MM.md)

One harness for every stack: HARNESS_STACK=rust (default) or node, or `stack = "node"` in <HARNESS_HOME>/harness.toml,
whose [profile] table overrides any command or pattern in STACKS below.
"""
from __future__ import annotations

import argparse, json, os, re, shutil, subprocess, sys, time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

import desk, quarantine, rules_gate, rules_langs

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


def sh(cmd, cwd=REPO, check=True, env=None, timeout=None, stdin=None) -> subprocess.CompletedProcess:
    p = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", env=env, shell=isinstance(cmd, str),
                         stdin=subprocess.PIPE if stdin is not None else None, creationflags=NO_WINDOW)
    try:
        out, err = p.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        # kill the whole tree: a grandchild (app.exe under a test) keeps the pipes open and would hang us forever
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, creationflags=NO_WINDOW)
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


@contextmanager
def lock(name: str, wait: float = 600):
    p = ST / f"{name}.lock"
    t0 = now()
    while True:
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except (FileExistsError, PermissionError):  # Windows: the holder's unlink is pending
            try:
                if now() - p.stat().st_mtime > (120 if name == "state" else 3600):  # stale (state sections are short)
                    p.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            except PermissionError:
                pass
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


def event(kind, **kw):
    with open(ST / "events.jsonl", "a", encoding="utf-8") as f:
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
    busy = {c["row"] for c in claims.values()} | {q["row"] for q in queue} | held_rows()
    # Only rows being BUILT (claims) count against the module cap and file overlap. Counting queued rows (built,
    # waiting to land) too left 0 of 93 ready rows buildable while 93 waited in the land queue: builders starved
    # exactly when landing was behind. The trial-merge batch pick handles queued-vs-new overlap at land time.
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
            continue  # too many rows of this module in flight: they conflict in its shared files
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
    """Copy the profile's `copy_ignored` globs from the main checkout into a worktree (real files); returns the count."""
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
    """The argv a restarted train runs: the project's run_train.py wrapper when there is one, else this file."""
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


CLOUD_SLOT0 = int(os.environ.get("HARNESS_CLOUD_SLOT0", "1000"))  # cloud.py slots: expired by `cloud.py collect` (2 h after a cloud session claims), never by CLAIM_TTL


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


def queue_sha(slot, row, sha, since) -> int:
    """Queue sha as slot's submission of row and drop slot's claim (caller holds the state lock); the queue length."""
    git("branch", "-f", f"h/{row}", sha)
    queue = [q for q in load("queue.json", []) if q["row"] != row] + [{"row": row, "sha": sha, "ts": now(), "slot": slot}]
    save("queue.json", queue)
    claims = load("claims.json", {})
    claims.pop(str(slot), None)
    save("claims.json", claims)
    event("submit", slot=slot, row=row, sha=sha, secs=round(now() - since))
    return len(queue)


def cmd_submit(a):
    why = submit_dup(slot_wt(a.slot))  # minutes, so before the state lock; the claim is still ours, fix and submit again
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
        print(json.dumps({"ok": True, "queued": queue_sha(a.slot, a.row, sha, c["ts"])}))


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
# Every stack-specific command and pattern is here, nowhere else. HARNESS_STACK (else `stack` in <HARNESS_HOME>/harness.toml,
# else rust) picks the profile; a [profile] table in harness.toml overrides any key. Command strings may use {QS} (quality
# scripts), {files} (the changed files, one run) or {file} (one run per changed file). "" turns a step off.
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
        "unit_fail_re": [r"^(\S+) --- FAILED$", r"^test (\S+) \.\.\. FAILED$"],
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
        # the unit stage's compile, run by the pick check on main + one row (a warm target rechecks only what changed)
        "pick_build": "cargo check -q --profile test --lib --bins --workspace",
        "pick_env": {"CARGO_TARGET_DIR": "{targets}/pick"},
        "pick_err_re": r"^error(\[E\d+\])?: (?!could not compile)",
        "fn_added_re": r"^\+\s*(pub(\([a-z]+\))?\s+)?fn\s+\w+",
        "fn_def_re": r"\bfn\s+{name}\b",
        "review_skip_re": r"/tests/",
        "path_re": r"((?:crates|src|tests)[\\/][\w\\/.-]+\.rs)",  # repo files a failure log names
        "quoted_path_re": r"[\"'`]((?:crates|src|tests)/[\w/.-]+\.rs)[\"'`]",  # repo files a test reads
        "strip_root": False,
        # env the toolchain needs: on Windows the MSVC linker and cc builds are found through ProgramFiles(x86),
        # which Git Bash drops. A profile can add its own (harness.toml [profile] require_env = [...]).
        "require_env": ["ProgramFiles(x86)"] if os.name == "nt" else [],
        # a gate log line that means the toolchain env was wrong, not a row (fleet_beat TRAIN-ENV)
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
        "fmt": "",  # prettier on the files the train changed, when the project has prettier (load_profile)
        "unit": "node --test --test-reporter=tap",
        "unit_one": "node --test --test-reporter=tap --test-name-pattern=\"^{name}$\"",
        "name_escape": True,  # unit_one gets re.escape(name)
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
        "qa": [],  # {changed} in a command = the files the batch changed vs main. dup_literals.py / check_cycles.py read JS: turn them on in harness.toml once main passes them
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
        "strip_root": True,  # logs carry absolute paths: drop the train root so the names are repo-relative
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
    """The one-test rerun for an overridden `unit`: a `node --test` unit gets the name filter appended, any other gets ""."""
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
    """Repo paths added or modified in the tree at `cwd` against `base`, under `dirs`, with one of `exts`."""
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
    """Run acceptance tests 'file::name' on the train tree. Returns the failed ones (empty = pass)."""
    return run_tests_cargo(tests, timeout, solo) if P["accept"] == "cargo" else run_tests_cmd(tests, timeout)


def acceptance_tests(tests):
    with train_stage("acceptance"):
        return desk.run(sys.modules[__name__], tests) if desk.on(ST) else run_tests(tests)


BUILD_FAILED = "test build failed: "  # run_alone's tail when a test exe did not build (quarantine never caches it)


def run_alone(tests, timeout=1800) -> dict:
    """{test: failure tail} of the tests failing alone in the train tree."""
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
    return {t: tail for t, tail in tails.items() if tail}


def perf_debt(t, out) -> bool:
    """A perf budget failed: meaningless on a loaded laptop, so record it to check later on a quiet machine."""
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
    """One profile `accept` run per test file (in parallel), its failures parsed by `accept_fail_re`; each failure is
    re-run alone, serially, before it counts (perf and timing tests fail under parallel load)."""
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


def run_one_alone(exes, t, env) -> str:
    """Failure tail of one test run alone; "" on a pass or perf debt."""
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
    except Exception as e:  # noqa: BLE001
        return str(e) or type(e).__name__
    if rr.returncode == 0 or perf_debt(t, out):
        return ""
    return out[-1500:] or f"exit {rr.returncode}"


ACCEPT_TIMES = "accept-times.json"  # {stem: seconds its binary last took in the parallel phase}: start order
ALONE_TIMES = "alone-times.json"  # {test: last alone seconds}
SOLO_DAYS = 2  # a test with a perf-budget miss this recent runs alone only (see solo_tests)


def solo_tests(tests) -> set:
    """Tests with a perf-budget miss in state/perf-debt.jsonl in the last SOLO_DAYS days: they fail under parallel
    load and are re-run alone anyway, so they run alone only."""
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
    """`stems` in start order: longest recorded time first, an unknown binary (a new row's) before all. Alphabetical
    order started the slow bench binaries last, so they were the parallel phase's tail."""
    return sorted(stems, key=lambda s: (-known.get(s, float("inf")), s))


def run_tests_cargo(tests, timeout=1800, solo=None):
    """Build the needed test binaries once (release, incremental), run them in parallel, longest first; `solo` tests
    (default solo_tests) run alone. Returns list of failed 'file::name' (empty = pass).
    Logs accept_split (build / parallel / serial seconds, slowest binaries and alone runs) and keeps per-binary times."""
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
    # perf/bench tests fail under parallel load: re-run each failure alone, serially, with the solo tests
    retry, fails, logs, alone = fails + sorted(solo), [], [], {}
    for t in retry:
        s0 = now()
        tail = run_one_alone(exes, t, env)
        alone[t] = round(now() - s0)
        if tail:
            fails.append(t)
            logs.append(f"== {Path(t.split('::')[0]).stem}::{t.split('::')[1]} (serial): {tail}")
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
    """rustfmt every workspace target root in batches and commit (`cargo fmt --all` overflows the Windows command
    line on large workspaces). Other stacks: the profile's `fmt` over the files changed against main."""
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
    """Run the profile's `regen` commands (generated files) in the train tree and commit them as "train: regen", so
    they come from the merged batch. "" or the failure text (the batch fails quality with it)."""
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


def unit_failures(out: str, first_only=False) -> set:
    """Failing unit test names in a unit run's output (the quick gate has always parsed only the first pattern)."""
    pats = P["unit_fail_re"][:1] if first_only else P["unit_fail_re"]
    return set().union(*(re.findall(p, out, re.M) for p in pats))


def unit_red_log(out: str) -> str:
    """Output tail plus each failing test's own lines, last: a reject reason (the last 1500 chars) names what failed."""
    hits = sorted((m.start(), m.group(1)) for p in P["unit_fail_re"] for m in re.finditer(p, out, re.M))
    if not hits:
        return out[-6000:]
    per = max(200, 1400 // len(hits[:4]))
    lines = "".join(chr(10) + out[i:i + per].rstrip() for i, _ in hits[:4])
    return out[-6000:] + chr(10) + f"unit failures ({len(hits)}): " + "; ".join(n for _, n in hits[:8]) + lines


def unit_flaky(failing: set, env) -> bool:
    """True when every failing unit test passes when rerun alone (single thread): load flakiness, not the batch.
    2026-11-12: a store snapshot timing test failed the whole gate under train load twice; it passes alone."""
    if not failing or len(failing) > 5 or not P["unit_one"]:
        return False
    for name in sorted(failing):
        n = re.escape(name).replace('"', ".") if P["name_escape"] else name
        rr = sh(P["unit_one"].replace("{name}", n), cwd=TRAIN, check=False, env=env, timeout=900)
        if rr.returncode != 0:
            return False
    event("gate_stage", stage="unit-flaky-passed-alone", failing=sorted(failing))
    return True


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


def main_unit_baseline() -> set:
    """Unit tests failing on main itself (cached per main sha)."""
    sha = git("rev-parse", "main").stdout.strip()
    cache = load("unit-baseline.json", {})
    if cache.get("sha") != sha:
        with at_main():
            rr = sh(P["unit"], cwd=TRAIN, check=False, env=run_env(TRAIN_ENV), timeout=1800)
        fails = unit_failures(rr.stdout + rr.stderr)
        cache = {"sha": sha, "fails": sorted(fails)}
        save("unit-baseline.json", cache)
        event("unit_baseline", sha=sha, fails=sorted(fails)[:10])
    return set(cache["fails"])


QUALITY_CMDS = [c.replace("{QS}", QS) if " " in c else f"py -3 {QS}/{c} {' '.join(P['src_dirs'])}" for c in P["qa"]]
CLIPPY_STRICT = P["lint_strict"]  # the strict linter (name kept from the rust-only harness)
FILE_LINES = int(os.environ.get("HARNESS_FILE_LINES", "500"))  # spec/STANDARDS.md S8 module-size cap
TEST_FILE_LINES = int(os.environ.get("HARNESS_TEST_FILE_LINES", "600"))  # the same cap for test files
RULES_CHECK = f"py -3 {Path(__file__).with_name('rules_gate.py').as_posix()} check --dir . --lang {STACK}"
FIND_OWNER = f'py -3 {QS}/index_api.py --find "<purpose words>"'
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
    return True  # owner 2026-11-11: never optional. No flag file; removing this needs a code change and a commit.


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
    """The S8 cap for repo path `rel`: test files (profile test_file_re; rust: under a tests/ folder, or tests.rs /
    *_tests.rs) get TEST_FILE_LINES."""
    return TEST_FILE_LINES if re.search(P["test_file_re"], rel) else FILE_LINES


def oversized_files(cwd, base: str) -> list:
    """Size gate: source files (profile src_exts under size_dirs) the tree at `cwd` adds or grows past the cap against
    `base`. A file already over at `base` is grandfathered until split; renames count as adds."""
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
    """Output of every quality check that fails (empty = clean). `base` is what the size gate measures growth from."""
    static = static_problems(cwd, base, plan, landing)
    out = [static] if static else []
    env = run_env(TRAIN_ENV)
    for c in expand(CLIPPY_STRICT, changed_files(cwd, base) if "{file" in CLIPPY_STRICT else []):
        r = sh(c, cwd=cwd, check=False, env=env, timeout=1800)
        if r.returncode != 0:
            out.append((r.stdout + r.stderr)[-4000:])
    return chr(10).join(out)


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
        queue_clean(a.name, sha, a.slot, front=a.front)
    print(json.dumps({"ok": True}))


def queue_clean(name, sha, slot, front=False) -> None:
    """Queue sha as row CLEAN-<name> (caller holds the state lock). front: ahead of every queued row (ts = oldest - 1;
    the train picks by ts), for a fix the owner wants landed first (cloud.py lane fix)."""
    row = f"CLEAN-{name}"
    rest = [q for q in load("queue.json", []) if q["row"] != row]
    ts = min([now()] + [q["ts"] for q in rest]) - 1 if front else now()
    save("queue.json", rest + [{"row": row, "sha": sha, "ts": ts, "slot": slot}])
    event("submit", slot=slot, row=row, sha=sha)


def known_red() -> dict:
    """Proven tests red on main itself (quarantine.py): the gate skips them."""
    return load(quarantine.KNOWN, {})


TRAIN_STAGE = "train-stage.json"


@contextmanager
def train_stage(stage: str):
    """Mark `stage` (alarm_dispatch holds cargo agents meanwhile)."""
    save(TRAIN_STAGE, {"stage": stage, "pid": os.getpid(), "t": iso()})
    try:
        yield
    finally:
        save(TRAIN_STAGE, {})


def gate_tests(new_rows, rows) -> list:
    kr = known_red()
    tests = [t for i in new_rows for t in rows[i]["tests"]]
    return sorted(set(tests + [t for r_ in rows.values() if r_["status"] == "proven" for t in r_["tests"] if t not in kr]))


def gate(new_rows, rows, quick=False):
    """Return (ok, failed_tests, log)."""
    env = run_env(TRAIN_ENV)
    fmt_tree(TRAIN)  # auto-fix formatting, never fail on it
    bad = regen_tree(TRAIN)  # generated files: regenerated from the merged tree; a failing command fails the batch
    if bad:
        return False, ["quality"], bad
    if quick:  # retry after ejecting rows whose own tests failed: lint passed with them in; unit is re-run (cheap, and
        # removing a branch can break another that used it)
        r = sh(P["unit"], cwd=TRAIN, check=False, env=run_env(TRAIN_ENV), timeout=1800)
        if r.returncode != 0:
            o = r.stdout + r.stderr
            failing = unit_failures(o, first_only=True)
            base = main_unit_baseline()
            if not (failing and (failing <= base or unit_flaky(failing - base, run_env(TRAIN_ENV)))):
                return False, ["unit"], unit_red_log(o)
        q = quality_problems(TRAIN, landing=new_rows, plan="always")
        if q:
            return False, ["quality"], q
        t0 = now()
        fails = acceptance_tests(gate_tests(new_rows, rows))
        event("gate_stage", stage="acceptance-quick", secs=round(now() - t0), fails=len(fails), failed=sorted(fails)[:20])
        log = (ST / "last-gate.txt").read_text(encoding="utf-8", errors="replace")[-20000:] if fails else ""
        return (not fails), fails, log
    if not quality_on():  # the quality stage's strict clippy is a superset; running both cost ~10 min a train (2026-11-12)
        t0 = now()
        r = sh(P["lint"] or "exit 0", cwd=TRAIN, check=False, env=env, timeout=1800)
        event("gate_stage", stage="clippy", secs=round(now() - t0), rc=r.returncode)
        if r.returncode != 0:
            return False, ["clippy"], (r.stdout + r.stderr)[-6000:]
    t0 = now()
    r = sh(P["unit"], cwd=TRAIN, check=False, env=env, timeout=1800)
    event("gate_stage", stage="unit", secs=round(now() - t0), rc=r.returncode)
    if r.returncode != 0:
        out = r.stdout + r.stderr
        failing = unit_failures(out)
        base = main_unit_baseline()
        if failing and failing <= base:  # main is already red on exactly these: not this batch's fault
            event("gate_stage", stage="unit-baseline-red", failing=sorted(failing)[:5])
            r = None
        elif failing and unit_flaky(failing - base, env):
            r = None
    if r is not None and r.returncode != 0 and not P["crate_blame"]:
        return False, ["unit"], unit_red_log(out)
    if r is not None and r.returncode != 0:
        crates = sorted(set(re.findall(r"-p (" + re.escape(CRATE) + r"-[\w-]+)", out)))
        dirs = " ".join("crates/" + c[len(CRATE) + 1:] + "/" for c in crates if c != CRATE)
        if f"-p {CRATE} " in out or f"`-p {CRATE}`" in out:
            dirs += " src/"
        return False, ["unit"], f"failing crates: {dirs}" + chr(10) + unit_red_log(out)
    if quality_on():
        t0 = now()
        q = quality_problems(TRAIN, landing=new_rows, plan="always")
        event("gate_stage", stage="quality", secs=round(now() - t0), rc=1 if q else 0)
        if q:
            return False, ["quality"], q
    tests = gate_tests(new_rows, rows)
    t0 = now()
    fails = acceptance_tests(tests)
    event("gate_stage", stage="acceptance", secs=round(now() - t0), n=len(tests), fails=len(fails), failed=sorted(fails)[:20])
    # 2026-11-13: an empty log here sent every unowned acceptance failure to bisect (~18 min a level, 6 h with 0 landed)
    log = (ST / "last-gate.txt").read_text(encoding="utf-8", errors="replace")[-20000:] if fails else ""
    return (not fails), fails, log


def build_train(batch, rows):
    """Merge every queued sha onto main in the train worktree. Returns (merged, conflicted)."""
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
    # 2026-11-11: 16 of 18 rows in one train were rejected on one hot file; each reject cost a
    # whole builder pass plus another train. Resolve a few here instead; the gate still tests the result.
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
    """Merge q again, let a short Sonnet pass resolve the conflict, accept it only if no markers remain and cargo check passes."""
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


# A proven row's prose is dead text (its test is the contract): the proven commit deletes its bullet and packet.
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
    sh(["py", "-3", f"{QS}/index_api.py"], cwd=TRAIN, check=False, timeout=600)
    git("add", "-A", "spec", cwd=TRAIN, check=False)
    git("commit", "-qam", f"train: proven {' '.join(ids)}", cwd=TRAIN, check=False)


def reject(q, reason, locked=False):
    """Record the reject AND dequeue the row (every reject path: pick checks, rulings, batch rejects). A rejected row
    left in queue.json was picked again forever. locked=True when the caller already holds the state lock."""
    with nullcontext() if locked else lock("state"):
        rj = load("rejects.json", {})
        rj.setdefault(q["row"], []).append({"t": iso(), "by": "train", "sha": q["sha"], "reason": reason[:1500]})
        save("rejects.json", rj)
        save("queue.json", [x for x in load("queue.json", []) if x["sha"] != q["sha"]])
    event("reject", row=q["row"], reason=reason[:300])


def dup_text(dups) -> str:
    """The one refusal text for duplicates: pick time, submit time and cloud collect all say it the same way."""
    why = "; ".join(f"{d['file']}::{d['fn']} duplicates {d['existing']} ({d.get('why', '')[:120]})" for d in dups)
    return ("duplicate capability (STANDARDS S1/S6): reuse or extend the owner, delete your copy: " + why) if why else ""


def dup_reason(q, dups) -> str:
    return dup_text([d for d in dups if adds_fn(q, d)])


def submit_dup(cwd) -> str:
    """Refusal text when the branch checked out in cwd adds a duplicate capability (the pick-time batch_review, same
    function), else "". Every reported duplicate is the row's own: the diff is merge-base..HEAD of one row. The reviewer
    being unavailable is not a verdict: say so on stderr and let the row queue, the pick-time review still guards it."""
    base = git("merge-base", "main", "HEAD", cwd=cwd).stdout.strip() or "main"
    try:
        return dup_text(batch_review([], cwd=cwd, base=base))
    except ReviewUnavailable as e:
        print(f"submit: duplicate reviewer unavailable twice ({e}); queued, the pick-time review decides", file=sys.stderr)
        return ""


def adds_fn(q, d) -> bool:
    """True when branch q itself adds fn d["fn"] in d["file"] (not merely touches the file)."""
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


CLAUDE_BIN = (shutil.which("claude.exe")
              or str(Path.home() / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"))


class ReviewUnavailable(Exception):
    """The duplicate reviewer gave no usable answer twice: the batch waits, it never lands unreviewed (rule zero)."""


def batch_review(merged, cwd=None, base="main") -> list:
    """Headless reviewer over the functions this batch adds, fail-CLOSED: an empty or unparseable answer is retried
    once, then ReviewUnavailable defers the batch. Builders run it before submit (`self-review --slot N`)."""
    cwd = cwd or TRAIN
    diff = git("diff", "-U0", base, "HEAD", "--", *P["src_dirs"], cwd=cwd, check=False).stdout
    added, cur = [], ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
        elif line.startswith("+") and re.match(P["fn_added_re"], line) and not re.search(P["review_skip_re"], cur):
            added.append(f"{cur}: {line[1:].strip()[:160]}")
    if not added:
        return []
    t0, last = now(), ""
    for attempt in (1, 2):
        try:
            r = sh([CLAUDE_BIN, "-p", "--output-format", "json", "--model", "sonnet", "--max-turns", "60",
                    "--dangerously-skip-permissions"], cwd=cwd, check=False, timeout=1500,
                   stdin=REVIEW_PROMPT.format(added=chr(10).join(added[:300])))
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


GREEN = {"head": ""}  # last gate-verified train head; a bisect's rejected right half stays in HEAD, so never land HEAD


def try_batch(batch, rows, depth=0, quick=False, reviewed=False):
    """Land as much of batch as possible. Returns list of landed queue entries."""
    merged, conflicted = build_train(batch, rows)
    for q in conflicted:
        reject(q, "merge conflict with main or another branch in the same train; merge main into your branch and resolve")
    if not merged:
        return []
    # review first (about a minute): a duplicate found after a 15-min gate would cost a whole second gate run
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
    ids = [q["row"] for q in merged]
    ok, fails, log = gate(ids, rows, quick)
    if ok:
        finish_spec(ids)
        GREEN["head"] = git("rev-parse", "HEAD", cwd=TRAIN).stdout.strip()
        return merged
    own = {i for i in ids for t in rows[i]["tests"] if t in fails}
    if fails not in (["clippy"], ["unit"], ["quality"]) and own and all(any(t in rows[i]["tests"] for i in own) for t in fails):
        # only the batch's own tests failed: eject those rows, retry the rest
        for q in merged:
            if q["row"] in own:
                reject(q, "own acceptance failed on merged tree: " + ", ".join(t for t in fails if t in rows[q["row"]]["tests"]))
        rest = [q for q in merged if q["row"] not in own]
        return try_batch(rest, rows, depth + 1, quick=True) if rest else []
    # before blame and probe, which pin a main-red test on an innocent row
    if quarantine.heal(sys.modules[__name__], merged, rows, fails):
        return try_batch(merged, rows, depth + 1, reviewed=True)
    # blame by file: eject branches that touched a file the failure output names
    if P["strip_root"]:  # absolute paths in the log -> repo-relative
        for root in {str(TRAIN), TRAIN.as_posix()}:
            log = (log or "").replace(root + chr(92), "").replace(root + "/", "")
    named = set(m.replace(chr(92), "/") for m in re.findall(P["path_re"], log or ""))
    m = re.search(r"failing crates: ([^\n]*)", log or "")
    if m:
        named |= set(m.group(1).split())
    for r_ in rows.values():  # a failing test points at its row's declared files
        if any(t in fails for t in r_["tests"]):
            named |= set(r_["file_list"])
    for t in fails:  # a test no row owns (a quality or regression test) points at the repo files it reads
        src = TRAIN / t.split("::")[0]
        if "::" in t and src.is_file():
            import posixpath  # a relative import ("../x.js") resolves against the test file's folder
            named |= set(posixpath.normpath(posixpath.join(posixpath.dirname(t.split("::")[0]), p)) if p.startswith(".") else p
                         for p in re.findall(P["quoted_path_re"], src.read_text(encoding="utf-8", errors="replace")))
    if len(merged) > 1 and named:
        guilty = []
        for q in merged:
            fs = set(git("diff", "--name-only", f"main...{q['sha']}", check=False).stdout.split())
            if fs & named or any(f.startswith(d) for f in fs for d in named if d.endswith("/")):
                guilty.append(q)
        if guilty and len(guilty) < len(merged):
            for q in guilty:
                reject(q, f"gate red ({fails[:3]}) in a file your branch changed: {sorted(named)[:5]} :: {log[-1200:]}")
            event("blame_by_file", ejected=[q["row"] for q in guilty])
            return try_batch([q for q in merged if q not in guilty], rows, depth + 1)
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


def probe_culprit(merged, rows, fails):
    """The one row that alone reproduces the failing tests, bisecting with only those tests; None for an interaction
    (the caller runs the full bisect)."""
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


def promote(landed):
    """Fast-forward main to the last gate-verified train head (GREEN); a no-op when main already has it."""
    head = GREEN["head"]
    before = git("rev-parse", "main").stdout.strip()
    if not head or head == before:
        return
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        git("update-ref", "refs/heads/main", head)  # main checkout dirty: move the ref only
    else:
        git("merge", "-q", "--ff-only", head)
    git("push", "-q", "origin", "main", check=False)
    event("land", rows=[q["row"] for q in landed], sha=head)
    tighten_rules_debt(before, head)


def tighten_rules_debt(before, head):
    """Lower the rules debt to the landed files' counts in the train tree; an error is an event, never a failed land."""
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
    """{path: mtime} of the train's own modules: a change to any of them restarts it between trains."""
    here = Path(__file__).resolve().parent
    return {p: p.stat().st_mtime for p in (here / n for n in TRAIN_CODE) if p.exists()}


def cmd_train(a):
    missing = [v for v in P["require_env"] if not os.environ.get(v)]
    if missing:  # measured: a train started from Git Bash (no ProgramFiles(x86)) found no MSVC toolchain, every native
        # test failed and 16 innocent rows were rejected. Refuse to start instead.
        sys.exit(f"train: {', '.join(missing)} not set (started from Git Bash?): start the train from PowerShell or cmd")
    if a.loop:  # restart and kill scripts use this pid (and its tree), never a machine-wide command-line pattern
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
            event("train_error", error=str(e)[-800:])
            print(f"{iso()} train error: {str(e)[-400:]}", flush=True)
            for n in ("train", "state"):
                release_lock(n)
            did = False
        if not a.loop:
            return
        if not did:
            time.sleep(30)


MIN_BATCH = int(os.environ.get("HARNESS_MIN_BATCH", "8"))
BATCH_WAIT_BUSY = int(os.environ.get("HARNESS_BATCH_WAIT_BUSY", str(25 * 60)))  # max wait for MIN_BATCH while claims are in flight
BATCH_WAIT = 600
# measured: batches of <=30 rows landed 20-38 rows/h; every 42-80-row batch landed 0 (one bad row or clash per ~10 rows)
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


def disjoint(queue: list) -> list:
    """Oldest first, the rows that merge cleanly onto main plus every earlier pick (git merge-tree trial merges;
    file-disjoint before git 2.38); the rest wait. A row carrying another unproven row's commits waits until that row
    is picked earlier or proven (state/pick-held.json)."""
    acc, out, held = git("rev-parse", "main").stdout.strip(), [], {}
    spec = main_spec()
    proven = {i for i, r in spec.items() if r["status"] == "proven"}
    for q in queue:
        dep = carried_rows(q, spec) - proven - {p["row"] for p in out}
        if dep:
            held[q["row"]] = sorted(dep)
            continue
        r = git("merge-tree", "--write-tree", acc, q["sha"], check=False)
        if r.returncode == 1:  # conflicts: next batch
            continue
        if r.returncode != 0 or not r.stdout.split():
            return _disjoint_files(queue)
        c = git("commit-tree", r.stdout.split()[0], "-p", acc, "-p", q["sha"], "-m", "train pick", check=False).stdout.strip()
        if not c:
            return _disjoint_files(queue)
        acc = c
        out.append(q)
    save("pick-held.json", held)
    return out


def carried_rows(q, spec: dict) -> set:
    """Rows other than q's own whose commits (not on main) q's branch carries: a commit subject starting `<row id>:`
    or `stack: <row id>` for a spec row or a CLEAN-* fix. A stacked row landing without them fails the plan check."""
    subj = git("log", "--no-merges", "--format=%s", f"main..{q['sha']}", check=False).stdout
    ids = {m.group(1).rstrip(":") for m in re.finditer(r"^(?:stack: )?(\S+)", subj, re.M)}
    return {i for i in ids if i in spec or i.startswith("CLEAN-")} - {q["row"]}


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
PICK_WT = WT / "pick"
PICK_CACHE = "pick-verdicts.json"  # {sha: {row, base, checks, built, reviewed}}: static pass per sha, compile per main
PICK_CHECKS = 2  # bump when the static pick checks change: older passes run again
PICK_IDLE_SECS = 120
PICK_BUILD_TIMEOUT = int(os.environ.get("HARNESS_PICK_BUILD_TIMEOUT", "600"))
PICK_BUILD_RED = "pick check: gate red (['unit']): the unit stage's compile fails on main + this row: "


class NoVerdict(Exception):
    pass


def pick_checkout(commit: str) -> None:
    if not (PICK_WT / ".git").exists():
        git("worktree", "add", "-q", "--detach", str(PICK_WT), commit)
    else:
        git("checkout", "-q", "-f", "--detach", commit, cwd=PICK_WT)
        git("clean", "-q", "-f", "-d", cwd=PICK_WT)


def merged_commit(base: str, sha: str) -> str | None:
    r = git("merge-tree", "--write-tree", base, sha, check=False)
    if r.returncode != 0 or not r.stdout.split():
        return None
    return git("commit-tree", r.stdout.split()[0], "-p", base, "-p", sha, "-m", "pick check").stdout.strip()


def main_static_clean(base: str, cache: dict) -> bool:
    """A main that fails the static checks itself rejects no row (its problems are not the row's)."""
    if cache.get("_main", {}).get("sha") != base:
        pick_checkout(base)
        cache["_main"] = {"sha": base, "clean": not static_problems(PICK_WT, base, plan="off")}
    return cache["_main"]["clean"]


def proc_start(pid: int) -> str:
    """pid's start time ('' if gone), from overhaul-build's drivelock."""
    if QS not in sys.path:
        sys.path.insert(0, QS)
    import drivelock
    return drivelock.proc_start(pid) or ""


def pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    return f'"{pid}"' in out


def in_acceptance() -> bool:
    mark = load(TRAIN_STAGE, {})
    return mark.get("stage") == "acceptance" and bool(mark.get("pid")) and pid_alive(mark["pid"])


def pick_compile(commit: str) -> tuple:
    """(ok, error lines) of P["pick_build"] at `commit` in PICK_WT; NoVerdict in the train's acceptance stage or on a
    timeout (the row goes on to the gate)."""
    if in_acceptance():
        raise NoVerdict("the train is in its acceptance stage")
    pick_checkout(commit)
    try:
        r = sh(P["pick_build"], cwd=PICK_WT, check=False, env=run_env(stack_env(P["pick_env"])), timeout=PICK_BUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise NoVerdict(f"pick build over {PICK_BUILD_TIMEOUT} s") from None
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


def pick_build(base: str, merged: str, cache: dict) -> str:
    """'' or the compile errors of `merged` (main + a row); a main that fails the same compile rejects no row."""
    if not P["pick_build"]:
        return ""
    t0 = now()
    ok, errs = pick_compile(merged)
    event("pick_compile", ok=ok, secs=round(now() - t0, 1))
    if ok:
        return ""
    main = cache.setdefault("_main", {})
    if main.get("built_sha") != base:
        main["built_sha"], main["built"] = base, pick_compile(base)[0]
    return PICK_BUILD_RED + errs[-3000:] if main["built"] else ""


def pick_verdict(q: dict, base: str, cache: dict) -> str:
    """'' or the reject reason of queued row q on commit `base` (a conflict is ''); raises ReviewUnavailable."""
    merged = merged_commit(base, q["sha"])
    if merged is None:
        return ""
    entry = cache.setdefault(q["sha"], {"row": q["row"]})
    pick_checkout(merged)
    if entry.get("checks") != PICK_CHECKS:
        log = chr(10).join(x for x in (static_problems(PICK_WT, base, plan="off"),
                                       *rules_gate.row_plan_problems(PICK_WT, base, WT, (q["row"],))) if x)
        if log:
            return "pick check: gate red (['quality']): " + log[-1500:]
        entry["base"], entry["checks"] = base, PICK_CHECKS
    if entry.get("built") != base:  # main moving under a row is what breaks its compile: one verdict per main
        why = pick_build(base, merged, cache)
        if why:
            return why
        entry["built"] = base
        pick_checkout(merged)
    if not entry.get("reviewed"):
        why = dup_reason(q, batch_review([q], cwd=PICK_WT, base=base))
        if why:
            return "pick check: " + why
        entry["reviewed"] = True
    return ""


# Pick acceptance: `harness.py pick-accept` (schedule it every few minutes; on while <state>/pick-accept-on exists) runs
# each queued row's owner_tests on the desk, merged onto the code the next pick sees (the batch head while a batch
# gates, else main). Never in acceptance; the gate's Desk.setup kills it (desk.pick_stop). pick_screen rejects a red
# verdict while main's code equals its base (only the spec differs); no verdict leaves the row to the gate.
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
    tests = [t for t in owner_tests(parse_spec(git("show", f"{base}:{SPEC}").stdout), files) if t not in known_red()]
    v, t0 = {"row": q["row"], "base": base, "ok": True, "fails": [], "tests": tests, "log": "", "secs": 0,
             "send_secs": 0}, now()
    if tests:
        try:
            res = (run or (lambda c, ts: desk.pick_run(sys.modules[__name__], c, ts)))(merged, tests)
        except NoVerdict as e:
            event("pick_accept", row=q["row"], ok=None, why=str(e)[-200:], tests=len(tests))
            return None
        fails = sorted(set(res.get("fails") or []) - set(known_red()))
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


def pick_screen(picks: list, budget: float = float("inf")) -> set:
    """Reject and dequeue the `picks` failing the pick check; their shas. A row with no verdict goes on to the gate."""
    cache, t0, bad = load(PICK_CACHE, {}), now(), []
    base = git("rev-parse", "main").stdout.strip()
    bad = [(q, why) for q in picks if (why := pick_accept_red(q["sha"], base))]  # before the cached-pass skip
    picks = [q for q in picks if q not in [b[0] for b in bad]]
    todo = [q for q in picks if not (cache.get(q["sha"], {}).get("checks") == PICK_CHECKS
                                     and cache[q["sha"]].get("built") == base and cache[q["sha"]].get("reviewed"))]
    if todo and not main_static_clean(base, cache):
        todo = []
    for q in todo:
        if now() - t0 > budget:
            break
        try:
            why = pick_verdict(q, base, cache)
        except Exception as e:  # noqa: BLE001 - no verdict
            event("pick_error", row=q["row"], error=f"{type(e).__name__}: {str(e)[-300:]}")
            continue
        event("pick_check", row=q["row"], ok=not why)
        if why:
            bad.append((q, why))
    shas = {q["sha"] for q, _ in bad}
    with lock("state"):
        for q, why in bad:
            reject(q, why, locked=True)
        queue = load("queue.json", [])
    live = {q["sha"] for q in queue} | {"_main"}
    save(PICK_CACHE, {k: v for k, v in cache.items() if k in live})
    return shas


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
        # a gate run costs ~15-20 min whatever the size: wait for a real batch (8 rows) or 10 min since the oldest
        # 2026-11-12: a 1-row train cost a full 22-min gate while 10 rows queued behind it. While builders still hold
        # claims, wait up to BATCH_WAIT_BUSY for a fuller batch; with no claims in flight nothing more is coming: go now.
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
        while batch:
            bad = pick_screen(batch)
            if not bad:
                break
            queue = [q for q in queue if q["sha"] not in bad]
            batch = disjoint(queue)[:BATCH_CAP]
        if not batch:  # every queued row conflicts with main: normal conflict handling, then idle (never an empty train)
            for q in queue:
                reject(q, "merge conflict with main; merge main into your branch and resolve")
            event("train_idle", queued=len(load("queue.json", [])), reason="no row merges cleanly with main")
            return False
        if fill_wait(queue, batch):
            return False
        for q in batch:
            if q["row"] not in rows:
                rows[q["row"]] = {"id": q["row"], "tests": [], "status": "cleanup", "file_list": [], "deps": [], "module": "cleanup"}
        t0 = now()
        event("train_start", rows=[q["row"] for q in batch])
        GREEN["head"] = ""
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
        event("train_end", landed=[q["row"] for q in landed], tried=len(batch), secs=round(now() - t0))
        for gen in ("dash.py",):  # dashboards are regenerated by the train, never by memory
            if (HOME / gen).exists():
                # run detached: a slow dashboard (15 min timeout) inside the train lock blocked every next batch
                subprocess.Popen(["py", "-3", str(HOME / gen)], cwd=HOME, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=NO_WINDOW)
        print(f"{iso()} train: landed {len(landed)}/{len(batch)} in {round(now() - t0)}s", flush=True)
        quarantine.release(sys.modules[__name__])
        return True


def read_events() -> list:
    """events.jsonl, skipping blank or half-written lines (2026-11-12: one blank line took status, the card and the
    dashboard down)."""
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
    p = sp.add_parser("release"); p.add_argument("--slot", type=int, required=True); p.add_argument("--row", required=True); p.add_argument("--reason", required=True)
    p = sp.add_parser("train"); p.add_argument("--loop", action="store_true")
    sp.add_parser("status")
    p = sp.add_parser("quality-check"); p.add_argument("--dir", default=""); p.add_argument("--base", default="")
    p = sp.add_parser("clean-start"); p.add_argument("--slot", type=int, required=True); p.add_argument("--name", required=True)
    p = sp.add_parser("self-review"); p.add_argument("--slot", type=int, required=True)
    p = sp.add_parser("clean-submit"); p.add_argument("--slot", type=int, required=True); p.add_argument("--name", required=True)
    p.add_argument("--front", action="store_true", help="queue ahead of every row (a fix the owner wants first)")
    sp.add_parser("note", help="append one line to the dated log").add_argument("text")
    sp.add_parser("pick-accept", help="desk acceptance of queued rows (on: state/pick-accept-on)")
    a = ap.parse_args()
    {"frontier": cmd_frontier, "claim": cmd_claim, "submit": cmd_submit, "release": cmd_release,
     "train": cmd_train, "status": cmd_status, "note": cmd_note, "pick-accept": lambda a: print(pick_accept_queue()),
     "clean-start": cmd_clean_start, "self-review": cmd_self_review, "quality-check": cmd_quality_check, "clean-submit": cmd_clean_submit}[a.cmd](a)


if __name__ == "__main__":
    main()
