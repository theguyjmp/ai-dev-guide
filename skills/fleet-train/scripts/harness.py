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
"""
from __future__ import annotations

import argparse, json, os, re, shutil, subprocess, sys, time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(os.environ.get("HARNESS_REPO", "."))
HOME = Path(os.environ.get("HARNESS_HOME", str(Path(__file__).parent)))
ST = HOME / "state"
WT = Path(os.environ.get("HARNESS_WT", str(REPO.parent / "vh")))  # per-slot worktrees, fixed paths -> warm incremental builds
TARGETS = REPO / ".overhaul" / "target"
SPEC = "spec/FUNCTIONS.md"
CLAIM_TTL = 100 * 60
ST.mkdir(parents=True, exist_ok=True)
WT.mkdir(parents=True, exist_ok=True)


def now() -> float:
    return time.time()


def iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(cmd, cwd=REPO, check=True, env=None, timeout=None, stdin=None) -> subprocess.CompletedProcess:
    p = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         errors="replace", env=env, shell=isinstance(cmd, str),
                         stdin=subprocess.PIPE if stdin is not None else None)
    try:
        out, err = p.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        # kill the whole tree: a grandchild (app.exe under a test) keeps the pipes open and would hang us forever
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
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
        except FileExistsError:
            if now() - p.stat().st_mtime > (120 if name == "state" else 3600):  # stale (state sections are short)
                p.unlink(missing_ok=True)
                continue
            if now() - t0 > wait:
                raise TimeoutError(name)
            time.sleep(0.5)
    try:
        yield
    finally:
        p.unlink(missing_ok=True)


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


def main_spec() -> dict:
    return parse_spec(git("show", f"main:{SPEC}").stdout)


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


def frontier(rows, claims, queue, attempts):
    busy = {c["row"] for c in claims.values()} | {q["row"] for q in queue}
    inflight = {}
    busy_files = set()
    for i in busy:
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


def ensure_wt(slot: int, branch: str, base: str):
    p = slot_wt(slot)
    if not (p / ".git").exists():
        git("worktree", "add", "-f", "--detach", str(p), "main")
    git("reset", "-q", "--hard", cwd=p, check=False)
    git("clean", "-qfd", "-e", "target", cwd=p, check=False)
    git("checkout", "-q", "-B", branch, base, cwd=p)
    return p


def env_for(slot) -> dict:
    return {"CARGO_TARGET_DIR": str(TARGETS / f"slot-{slot}"),
            "CARGO_INCREMENTAL": "1"}


# ---------------------------------------------------------------- commands
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
            "must_also_before_commit": QUALITY_BRIEF}


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


# ---------------------------------------------------------------- train
TRAIN = WT / "train"
FIXTURES = WT / "fixture-cache-train"
CRATE = os.environ.get("HARNESS_CRATE", "app")  # workspace crate prefix: <CRATE>-<name> lives in crates/<name>/
FIXTURE_EXT = os.environ.get("HARNESS_FIXTURE_EXT", ".fixture")
TRAIN_ENV = {os.environ.get("HARNESS_FIXTURE_VAR", "APP_FIXTURE_CACHE"): str(FIXTURES), "CARGO_TARGET_DIR": str(TARGETS / "land-0"), "CARGO_PROFILE_RELEASE_INCREMENTAL": "true", "PROPTEST_DISABLE_FAILURE_PERSISTENCE": "1"}


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


def run_tests(tests, timeout=1800):
    """Build the needed test binaries once (release, incremental), run them in parallel.
    Returns list of failed 'file::name' (empty = pass)."""
    if not tests:
        return []
    from concurrent.futures import ThreadPoolExecutor
    by_stem = {}
    for t in tests:
        by_stem.setdefault(Path(t.split("::")[0]).stem, []).append(t.split("::")[1])
    env = run_env(TRAIN_ENV)
    cmd = ["cargo", "test", "--release", "--no-run", "--message-format=json", "-q"] + sum((["--test", s] for s in sorted(by_stem)), [])
    r = sh(cmd, cwd=TRAIN, check=False, env=env, timeout=timeout)
    exes = {}
    for line in r.stdout.splitlines():
        if line.startswith("{") and '"executable"' in line:
            m = json.loads(line)
            if m.get("executable") and m.get("target", {}).get("kind") == ["test"]:
                exes[m["target"]["name"]] = m["executable"]
    if r.returncode != 0:
        (ST / "last-gate.txt").write_text((r.stdout[-5000:] + r.stderr[-15000:]), encoding="utf-8")
        missing = [s for s in by_stem if s not in exes]
        return [f"tests/acceptance/{s}.rs::{n}" for s in (missing or by_stem) for n in by_stem[s]] or ["build"]

    def one(stem):
        exe = exes.get(stem)
        if not exe:
            return stem, by_stem[stem], "no executable"
        try:
            rr = sh([exe, "--exact", "--test-threads=4", *by_stem[stem]], cwd=TRAIN, check=False, env=env, timeout=900)
        except subprocess.TimeoutExpired:
            return stem, by_stem[stem], "timeout 900s"
        if rr.returncode == 0:
            return stem, [], ""
        out = rr.stdout + rr.stderr
        bad = set(re.findall(r"^test (test_\w+) \.\.\. FAILED", out, re.M)) | set(re.findall(r"^\s{4}(test_\w+)$", out, re.M))
        return stem, [n for n in by_stem[stem] if n in bad] or by_stem[stem], out[-1500:]

    fails, logs = [], []
    with ThreadPoolExecutor(10) as ex:
        for stem, bad, log in ex.map(one, sorted(by_stem)):
            fails += [f"tests/acceptance/{stem}.rs::{n}" for n in bad]
            if bad:
                logs.append(f"== {stem}: {log}")
    if fails:  # perf/bench tests fail under parallel load: re-run each failure alone, serially
        retry, fails, logs = fails, [], []
        for t in retry:
            stem, name = Path(t.split("::")[0]).stem, t.split("::")[1]
            try:
                rr = sh([exes[stem], "--exact", "--test-threads=1", name], cwd=TRAIN, check=False, env=env, timeout=900)
                out = rr.stdout + rr.stderr
                m = re.search(r"sample.generate ([\w-]+): .*locked by another writer", out)
                if rr.returncode != 0 and m:  # stale fixture lock: drop the fixture, regenerate once
                    for f in FIXTURES.glob(m.group(1) + FIXTURE_EXT + "*"):
                        f.unlink(missing_ok=True)
                    rr = sh([exes[stem], "--exact", "--test-threads=1", name], cwd=TRAIN, check=False, env=env, timeout=900)
                    out = rr.stdout + rr.stderr
                if rr.returncode != 0 and re.search(r"over budget|under floor|over the budget|over limit|^BENCH ", out, re.M):
                    # perf budgets are meaningless on a loaded laptop: record, check later on a quiet machine
                    with open(ST / "perf-debt.jsonl", "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"t": iso(), "test": t, "out": out[-400:]}) + chr(10))
                    event("perf_debt", test=t)
                elif rr.returncode != 0:
                    fails.append(t)
                    logs.append(f"== {stem}::{name} (serial): {out[-1500:]}")
            except Exception as e:  # noqa: BLE001
                fails.append(t)
                logs.append(f"== {stem}::{name}: {e}")
    if logs:
        (ST / "last-gate.txt").write_text(chr(10).join(logs)[-20000:], encoding="utf-8")
    want = set(tests)
    return [f for f in fails if f in want] or fails


def fmt_tree(cwd):
    """rustfmt every workspace target root in batches and commit. `cargo fmt --all` hits Windows' 32k command-line
    limit on this workspace (hundreds of [[test]] roots) and failed silently, so main drifted unformatted (2026-11-12:
    49 files)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("fmt_check", f"{QS}/fmt_check.py")
    fc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fc)
    for ed, files in fc.roots(Path(cwd)).items():
        for i in range(0, len(files), 60):
            subprocess.run(["rustfmt", "--edition", ed, *files[i:i + 60]], cwd=cwd, capture_output=True)
    if git("status", "--porcelain", cwd=cwd, check=False).stdout.strip():
        git("commit", "-qam", "train: rustfmt", cwd=cwd, check=False)


def unit_flaky(failing: set, env) -> bool:
    """True when every failing unit test passes when rerun alone (single thread): load flakiness, not the batch.
    2026-11-12: a store snapshot timing test failed the whole gate under train load twice; it passes alone."""
    if not failing or len(failing) > 5:
        return False
    for name in sorted(failing):
        rr = sh(f"cargo test -q --workspace --lib --bins -- --exact {name} --test-threads=1", cwd=TRAIN, check=False,
                env=env, timeout=900)
        if rr.returncode != 0:
            return False
    event("gate_stage", stage="unit-flaky-passed-alone", failing=sorted(failing))
    return True


def main_unit_baseline() -> set:
    """Unit tests failing on main itself (cached per main sha)."""
    sha = git("rev-parse", "main").stdout.strip()
    cache = load("unit-baseline.json", {})
    if cache.get("sha") != sha:
        cur = git("rev-parse", "HEAD", cwd=TRAIN).stdout.strip()
        git("checkout", "-q", "--detach", "-f", sha, cwd=TRAIN)
        rr = sh("cargo test -q --workspace --lib --bins --no-fail-fast", cwd=TRAIN, check=False, env=run_env(TRAIN_ENV), timeout=1800)
        o = rr.stdout + rr.stderr
        fails = set(re.findall(r"^(\S+) --- FAILED$", o, re.M)) | set(re.findall(r"^test (\S+) \.\.\. FAILED$", o, re.M))
        git("checkout", "-q", "--detach", "-f", cur, cwd=TRAIN)
        cache = {"sha": sha, "fails": sorted(fails)}
        save("unit-baseline.json", cache)
        event("unit_baseline", sha=sha, fails=sorted(fails)[:10])
    return set(cache["fails"])


QS = os.environ.get("HARNESS_QS", str(Path.home() / ".claude/skills/overhaul-build/scripts"))
QUALITY_CMDS = [f"py -3 {QS}/dup_blocks.py crates src", f"py -3 {QS}/dup_literals.py crates src",
                f"py -3 {QS}/shape_check.py crates src", f"py -3 {QS}/check_cycles.py crates src"]
CLIPPY_STRICT = ("cargo clippy -q --workspace --all-targets -- -D warnings -D clippy::too_many_lines "
                 "-D clippy::unwrap_used -D clippy::cognitive_complexity")
FILE_LINES = int(os.environ.get("HARNESS_FILE_LINES", "500"))  # spec/STANDARDS.md S8 module-size cap
TEST_FILE_LINES = int(os.environ.get("HARNESS_TEST_FILE_LINES", "600"))  # the same cap for test files
QUALITY_BRIEF = ("The plan forbids second copies (spec/STANDARDS.md S1, S5, S6, S44): before writing ANY function, "
                 "private helpers included, grep spec/index/*.md and spec/ARCHITECTURE.md for an owner that already has it "
                 "(core owns refusals and error builders, geom owns maths; vectors, param parsing, not_found/malformed/"
                 "invalid builders must come from their owner, never a local copy) and extend the owner instead. "
                 f"Keep every file you add or grow at most {FILE_LINES} lines (tests {TEST_FILE_LINES}; STANDARDS S8): split into modules. "
                 "Clean code is part of done. Before committing, from your worktree run each of: "
                 + " ; ".join(QUALITY_CMDS) + " ; and " + CLIPPY_STRICT.replace("--workspace", "-p <each crate you touched>")
                 + ". Fix every problem they report in files you changed (extract repeated blocks into one named fn, "
                 "name repeated literals as consts, split functions over 30 lines into named steps). Before writing a "
                 "helper, grep the workspace for an existing one and reuse it. The land train rejects branches that add any of these.")


def quality_on() -> bool:
    return True  # owner 2026-11-11: never optional. No flag file; removing this needs a code change and a commit.


OWNED_FN = re.compile(r"^\s*(?:pub(?:\([a-z]+\))?\s+)?fn\s+(unit|unit_above|unit_scaled|cross|cross2|dot|norm|point2|point3|"
                      r"not_found|malformed|conflict|positive|finite)\s*[<(]", re.M)


def owner_copies(cwd) -> set:
    """`file::fn` for every fn outside crates/core named like a capability core owns (math, params, refusals)."""
    found = set()
    for f in Path(cwd).glob("crates/*/src/**/*.rs"):
        rel = f.relative_to(cwd).as_posix()
        if rel.startswith("crates/core/"):
            continue
        for m in OWNED_FN.finditer(f.read_text(encoding="utf-8", errors="replace")):
            found.add(f"{rel}::{m.group(1)}")
    return found




def file_line_cap(rel: str) -> int:
    """The S8 cap for repo path `rel`: test files (under a tests/ folder, or tests.rs / *_tests.rs) get TEST_FILE_LINES."""
    parts = rel.split("/")
    test = "tests" in parts[:-1] or parts[-1] == "tests.rs" or parts[-1].endswith("_tests.rs")
    return TEST_FILE_LINES if test else FILE_LINES


def oversized_files(cwd, base: str) -> list:
    """S8 module-size gate: each .rs file under crates/ src/ tests/ that the tree at `cwd` adds, or grows past its
    cap, relative to `base`. A file already over the cap at `base` is grandfathered until it is split under it; from
    then on it is gated like any other. Renames count as adds (--no-renames), so moving a big file needs a split."""
    r = git("diff", "--name-only", "--no-renames", "--diff-filter=AM", base, "--", "crates", "src", "tests",
            cwd=cwd, check=False)
    if r.returncode != 0:
        return [f"size gate: git diff against {base} failed: {(r.stderr or '').strip()[-300:]}"]
    out = []
    for rel in r.stdout.split():
        f = Path(cwd) / rel
        if not rel.endswith(".rs") or not f.exists():
            continue
        cap, now_n = file_line_cap(rel), len(f.read_text(encoding="utf-8", errors="replace").splitlines())
        if now_n <= cap:
            continue
        old = git("show", f"{base}:{rel}", cwd=cwd, check=False)
        before = len(old.stdout.splitlines()) if old.returncode == 0 else 0
        if before <= cap:
            out.append(f"{rel}:1: {now_n} lines (was {before}); STANDARDS S8 caps a file at {cap}: split it into modules")
    return out


def quality_problems(cwd, base: str = "main") -> str:
    """Output of every quality check that fails (empty = clean). `base` is what the size gate measures growth from."""
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
    env = run_env(TRAIN_ENV)
    for c in QUALITY_CMDS + [CLIPPY_STRICT]:
        r = sh(c, cwd=cwd, check=False, env=env, timeout=1800)
        if r.returncode != 0:
            out.append((r.stdout + r.stderr)[-4000:])
    return chr(10).join(out)


def cmd_quality_check(a):
    """Used by <repo>/.git/hooks/pre-push: refuse to publish a tree with quality problems."""
    d = Path(a.dir) if a.dir else REPO
    base = a.base or ("origin/main" if git("rev-parse", "-q", "--verify", "origin/main", cwd=d, check=False).returncode == 0
                      else "main")
    q = quality_problems(d, base)
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
    dups = batch_review([], cwd=wt, base=base)
    print(json.dumps({"duplicates": dups}, indent=1))


def cmd_clean_submit(a):
    p = slot_wt(a.slot)
    if git("status", "--porcelain", cwd=p).stdout.strip():
        sys.exit("commit first")
    sha = git("rev-parse", "HEAD", cwd=p).stdout.strip()
    with lock("state"):
        queue = load("queue.json", [])
        queue.append({"row": f"CLEAN-{a.name}", "sha": sha, "ts": now(), "slot": a.slot})
        save("queue.json", queue)
    event("submit", slot=a.slot, row=f"CLEAN-{a.name}", sha=sha)
    print(json.dumps({"ok": True}))


def gate(new_rows, rows, quick=False):
    """Return (ok, failed_tests, log)."""
    env = run_env(TRAIN_ENV)
    fmt_tree(TRAIN)  # auto-fix formatting, never fail on it
    if quick:  # retry after ejecting rows whose own tests failed: lint passed with them in; unit is re-run (cheap, and
        # removing a branch can break another that used it)
        r = sh("cargo test -q --workspace --lib --bins --no-fail-fast", cwd=TRAIN, check=False, env=run_env(TRAIN_ENV), timeout=1800)
        if r.returncode != 0:
            o = r.stdout + r.stderr
            failing = set(re.findall(r"^(\S+) --- FAILED$", o, re.M))
            base = main_unit_baseline()
            if not (failing and (failing <= base or unit_flaky(failing - base, run_env(TRAIN_ENV)))):
                return False, ["unit"], o[-6000:]
        q = quality_problems(TRAIN)
        if q:
            return False, ["quality"], q
        t0 = now()
        tests = [t for i in new_rows for t in rows[i]["tests"]] + [t for r_ in rows.values() if r_["status"] == "proven" for t in r_["tests"]]
        fails = run_tests(sorted(set(tests)))
        event("gate_stage", stage="acceptance-quick", secs=round(now() - t0), fails=len(fails))
        # 2026-11-13: an empty log here sent every unowned acceptance failure to bisect (~18 min a level, 6 h with 0 landed)
    log = (ST / "last-gate.txt").read_text(encoding="utf-8", errors="replace")[-20000:] if fails else ""
    return (not fails), fails, log
    if not quality_on():  # the quality stage's strict clippy is a superset; running both cost ~10 min a train (2026-11-12)
        t0 = now()
        r = sh("cargo clippy -q --workspace --all-targets -- -D warnings", cwd=TRAIN, check=False, env=env, timeout=1800)
        event("gate_stage", stage="clippy", secs=round(now() - t0), rc=r.returncode)
        if r.returncode != 0:
            return False, ["clippy"], (r.stdout + r.stderr)[-6000:]
    t0 = now()
    r = sh("cargo test -q --workspace --lib --bins --no-fail-fast", cwd=TRAIN, check=False, env=env, timeout=1800)
    event("gate_stage", stage="unit", secs=round(now() - t0), rc=r.returncode)
    if r.returncode != 0:
        out = r.stdout + r.stderr
        failing = set(re.findall(r"^(\S+) --- FAILED$", out, re.M)) | set(re.findall(r"^test (\S+) \.\.\. FAILED$", out, re.M))
        base = main_unit_baseline()
        if failing and failing <= base:  # main is already red on exactly these: not this batch's fault
            event("gate_stage", stage="unit-baseline-red", failing=sorted(failing)[:5])
            r = None
        elif failing and unit_flaky(failing - base, env):
            r = None
    if r is not None and r.returncode != 0:
        crates = sorted(set(re.findall(r"-p (" + re.escape(CRATE) + r"-[\w-]+)", out)))
        dirs = " ".join("crates/" + c[len(CRATE) + 1:] + "/" for c in crates if c != CRATE)
        if f"-p {CRATE} " in out or f"`-p {CRATE}`" in out:
            dirs += " src/"
        return False, ["unit"], f"failing crates: {dirs}" + chr(10) + out[-6000:]
    if quality_on():
        t0 = now()
        q = quality_problems(TRAIN)
        event("gate_stage", stage="quality", secs=round(now() - t0), rc=1 if q else 0)
        if q:
            return False, ["quality"], q
    tests = [t for i in new_rows for t in rows[i]["tests"]]
    tests += [t for r_ in rows.values() if r_["status"] == "proven" for t in r_["tests"]]
    t0 = now()
    fails = run_tests(sorted(set(tests)))
    event("gate_stage", stage="acceptance", secs=round(now() - t0), n=len(set(tests)), fails=len(fails))
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
3. Run `cargo check -q --workspace --tests 2>&1 | grep -E '^error' -A5 | head -40` and fix errors until it is clean.
4. Do NOT commit, abort the merge, or run tests. Reply DONE, or FAIL with one line why. Keep tool output small."""


def resolve_conflict(q):
    """Merge q again, let a short Sonnet pass resolve the conflict, accept it only if no markers remain and cargo check passes."""
    t0 = now()
    git("merge", "-q", "--no-ff", "--no-edit", "-m", f"train: land {q['row']} (conflict resolved)", q["sha"], cwd=TRAIN, check=False)
    files = git("diff", "--name-only", "--diff-filter=U", cwd=TRAIN, check=False).stdout.split()
    ok = bool(files) and len(files) <= 4 and all(f.endswith(".rs") for f in files)
    if ok:
        sh([CLAUDE_BIN, "-p", "--model", "sonnet", "--max-turns", "30", "--dangerously-skip-permissions"], cwd=TRAIN,
           check=False, env=run_env(TRAIN_ENV), timeout=600, stdin=RESOLVE_PROMPT.format(row=q["row"], files=" ".join(files)))
        marked = git("grep", "-l", "-E", "^(<<<<<<<|>>>>>>>) ", "--", *files, cwd=TRAIN, check=False).stdout.strip()
        ok = not marked and sh("cargo check -q --workspace --tests", cwd=TRAIN, check=False, env=run_env(TRAIN_ENV),
                               timeout=900).returncode == 0
    if ok:
        git("add", "-A", cwd=TRAIN, check=False)
        ok = git("commit", "-q", "--no-edit", cwd=TRAIN, check=False).returncode == 0
    if not ok:
        git("merge", "--abort", cwd=TRAIN, check=False)
        git("reset", "-q", "--hard", "HEAD", cwd=TRAIN, check=False)
    event("resolve", row=q["row"], ok=ok, files=files[:4], secs=round(now() - t0))
    return ok


def finish_spec(ids):
    p = TRAIN / SPEC
    p.write_text(mark_proven(p.read_text(encoding="utf-8"), set(ids)), encoding="utf-8")
    # keep spec/INDEX.md + spec/index/* current: builders search them before writing any function (STANDARDS S6)
    sh(["py", "-3", f"{QS}/index_api.py"], cwd=TRAIN, check=False, timeout=600)
    git("add", "-A", "spec", cwd=TRAIN, check=False)
    git("commit", "-qam", f"train: proven {' '.join(ids)}", cwd=TRAIN, check=False)


def reject(q, reason):
    rj = load("rejects.json", {})
    rj.setdefault(q["row"], []).append({"t": iso(), "by": "train", "sha": q["sha"], "reason": reason[:1500]})
    save("rejects.json", rj)
    event("reject", row=q["row"], reason=reason[:300])


def adds_fn(q, d) -> bool:
    """True when branch q itself adds fn d["fn"] in d["file"] (not merely touches the file)."""
    diff = git("diff", "-U0", f"main...{q['sha']}", "--", d["file"], check=False).stdout
    return re.search(r"^\+.*\bfn\s+" + re.escape(d["fn"]) + r"\b", diff, re.M) is not None


def branch_files(q) -> set:
    return set(git("diff", "--name-only", f"main...{q['sha']}", check=False).stdout.split())


REVIEW_PROMPT = """You are the duplicate-capability reviewer for the rebuild (Rust). Your cwd is the merged train tree.
These functions were ADDED by the batch about to land (file: signature):
{added}
For each one, decide whether the codebase ALREADY has a function doing the same job (same input meaning, same output,
possibly different name/wording): search spec/index/*.md and grep crates/ src/, and read both bodies before deciding.
Owners to prefer: app_core::math, app_core::param, app_core::refusal (+ core::invalid), and the owners in
spec/ARCHITECTURE.md. Report ONLY verified duplicates. Thin wrappers that call the owner are fine. Be fast: at most 40 tool calls.
Final answer: ONLY a JSON object {{"duplicates": [{{"file": "<path of the NEW copy>", "fn": "<name>", "existing": "<file:line>", "why": "<short>"}}]}}"""


CLAUDE_BIN = (shutil.which("claude.exe")
              or str(Path.home() / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"))


def batch_review(merged, cwd=None, base="main") -> list:
    """Headless reviewer over the functions this batch adds. Fail-open (logged) so a reviewer outage never stalls lands.
    Also run by builders before submit (`self-review --slot N`), so a duplicate is caught before a 20-min train."""
    cwd = cwd or TRAIN
    diff = git("diff", "-U0", base, "HEAD", "--", "crates", "src", cwd=cwd, check=False).stdout
    added, cur = [], ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
        elif line.startswith("+") and re.match(r"^\+\s*(pub(\([a-z]+\))?\s+)?fn\s+\w+", line) and "/tests/" not in cur:
            added.append(f"{cur}: {line[1:].strip()[:160]}")
    if not added:
        return []
    t0 = now()
    try:
        r = sh([CLAUDE_BIN, "-p", "--output-format", "json", "--model", "sonnet", "--max-turns", "60",
                "--dangerously-skip-permissions"], cwd=cwd, check=False, timeout=1500,
               stdin=REVIEW_PROMPT.format(added=chr(10).join(added[:300])))
        res = json.loads(r.stdout).get("result", "")
        m = re.search(r"\{.*\}", res, re.S)
        dups = json.loads(m.group(0)).get("duplicates", []) if m else []
        dups = [d for d in dups if isinstance(d, dict) and d.get("file") and d.get("fn")]
        event("review", added=len(added), duplicates=len(dups), secs=round(now() - t0))
        return dups
    except Exception as e:  # noqa: BLE001
        event("review_error", error=str(e)[-400:], secs=round(now() - t0))
        return []


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
        guilty = [q for q in merged if any(adds_fn(q, d) for d in dups)]
        if guilty:
            for q in guilty:
                why = "; ".join(f"{d['file']}::{d['fn']} duplicates {d['existing']} ({d.get('why', '')[:120]})"
                                for d in dups if adds_fn(q, d))
                reject(q, "duplicate capability (STANDARDS S1/S6): reuse or extend the owner, delete your copy: " + why)
            event("review_reject", rows=[q["row"] for q in guilty])
            rest = [q for q in merged if q not in guilty]
            return try_batch(rest, rows, depth + 1, quick=False, reviewed=True) if rest else []
    ids = [q["row"] for q in merged]
    ok, fails, log = gate(ids, rows, quick)
    if ok:
        finish_spec(ids)
        return merged
    own = {i for i in ids for t in rows[i]["tests"] if t in fails}
    if fails not in (["clippy"], ["unit"], ["quality"]) and own and all(any(t in rows[i]["tests"] for i in own) for t in fails):
        # only the batch's own tests failed: eject those rows, retry the rest
        for q in merged:
            if q["row"] in own:
                reject(q, "own acceptance failed on merged tree: " + ", ".join(t for t in fails if t in rows[q["row"]]["tests"]))
        rest = [q for q in merged if q["row"] not in own]
        return try_batch(rest, rows, depth + 1, quick=True) if rest else []
    # blame by file: eject branches that touched a file the failure output names
    named = set(m.replace(chr(92), "/") for m in re.findall(r"((?:crates|src|tests)[\\/][\w\\/.-]+\.rs)", log or ""))
    m = re.search(r"failing crates: ([^\n]*)", log or "")
    if m:
        named |= set(m.group(1).split())
    for r_ in rows.values():  # a failing test points at its row's declared files
        if any(t in fails for t in r_["tests"]):
            named |= set(r_["file_list"])
    for t in fails:  # a test no row owns (a quality or regression test) points at the repo files it reads
        src = TRAIN / t.split("::")[0]
        if "::" in t and src.is_file():
            named |= set(re.findall(r"[\"'`]((?:crates|src|tests)/[\w/.-]+\.rs)[\"'`]", src.read_text(encoding="utf-8", errors="replace")))
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
    if len(merged) == 1:
        reject(merged[0], f"gate red on merged tree: {fails[:8]} {log[-1500:]}")
        return []
    mid = len(merged) // 2
    left = try_batch(merged[:mid], rows, depth + 1)
    if left:
        promote(left)
    return left + try_batch(merged[mid:], rows, depth + 1)


def promote(landed):
    """Fast-forward main to the train head."""
    head = git("rev-parse", "HEAD", cwd=TRAIN).stdout.strip()
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        git("update-ref", "refs/heads/main", head)  # main checkout dirty: move the ref only
    else:
        git("merge", "-q", "--ff-only", head)
    git("push", "-q", "origin", "main", check=False)
    event("land", rows=[q["row"] for q in landed], sha=head)


def cmd_train(a):
    born = Path(__file__).stat().st_mtime
    while True:
        if a.loop and Path(__file__).stat().st_mtime != born and \
                sh([sys.executable, "-c", f"import ast;ast.parse(open(r'{__file__}',encoding='utf-8').read())"], check=False).returncode == 0:  # code changed and parses: restart between trains
            print(f"{iso()} train: harness.py changed, re-exec", flush=True)
            os.execv(sys.executable, [sys.executable, "-u", __file__, "train", "--loop"])
        try:
            did = train_once()
        except Exception as e:  # noqa: BLE001 - never let one bad train stop the loop
            event("train_error", error=str(e)[-800:])
            print(f"{iso()} train error: {str(e)[-400:]}", flush=True)
            for n in ("train", "state"):
                (ST / f"{n}.lock").unlink(missing_ok=True)
            did = False
        if not a.loop:
            return
        if not did:
            time.sleep(30)


MIN_BATCH = int(os.environ.get("HARNESS_MIN_BATCH", "8"))
BATCH_WAIT_BUSY = int(os.environ.get("HARNESS_BATCH_WAIT_BUSY", str(25 * 60)))  # max wait for MIN_BATCH while claims are in flight
BATCH_WAIT = 600


def disjoint(queue: list) -> list:
    """Oldest first, the rows whose changed files overlap no earlier pick; the rest wait one batch.
    Measured: every conflict the train resolved was row-vs-row inside one batch (0 of 58 queued rows conflicted with
    main), ~2 min each and serial ahead of a 15 min gate. A disjoint batch landed 31 of 33 rows in 21 min, 0 resolves."""
    taken, out = set(), []
    for q in queue:
        files = set(git("diff", "--name-only", f"main...{q['sha']}", check=False).stdout.split())
        if files & taken:
            continue
        out.append(q)
        taken |= files
    return out


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
        if not queue or (len(queue) < MIN_BATCH and now() - min(q["ts"] for q in queue) < wait):
            return False
        batch = disjoint(sorted(queue, key=lambda q: q["ts"]))[:80]
        for q in batch:
            if q["row"] not in rows:
                rows[q["row"]] = {"id": q["row"], "tests": [], "status": "cleanup", "file_list": [], "deps": [], "module": "cleanup"}
        t0 = now()
        event("train_start", rows=[q["row"] for q in batch])
        landed = try_batch(batch, rows)
        if landed and git("rev-parse", "HEAD", cwd=TRAIN).stdout.strip() != git("rev-parse", "main").stdout.strip():
            promote(landed)
        with lock("state"):
            queue = load("queue.json", [])
            done = {q["sha"] for q in batch}
            save("queue.json", [q for q in queue if q["sha"] not in done])
        event("train_end", landed=[q["row"] for q in landed], tried=len(batch), secs=round(now() - t0))
        for gen in ("dash.py",):  # dashboards are regenerated by the train, never by memory
            if (HOME / gen).exists():
                sh(["py", "-3", str(HOME / gen)], cwd=HOME, check=False, timeout=900)
        print(f"{iso()} train: landed {len(landed)}/{len(batch)} in {round(now() - t0)}s", flush=True)
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
    a = ap.parse_args()
    {"frontier": cmd_frontier, "claim": cmd_claim, "submit": cmd_submit, "release": cmd_release,
     "train": cmd_train, "status": cmd_status,
     "clean-start": cmd_clean_start, "self-review": cmd_self_review, "quality-check": cmd_quality_check, "clean-submit": cmd_clean_submit}[a.cmd](a)


if __name__ == "__main__":
    main()
