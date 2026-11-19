"""The desk: a second Windows machine (ssh, cmd shell) running tests from a git bundle. Cargo: acceptance split (units
on both machines for the earliest finish by measured throughput, state/unit-rates.json; cold start by CPU load; no
verdict runs here; a unit that did not build runs there once more after `cargo clean -p` of what its build output
names, then here).
Other runners: every test run there (`job`); no verdict raises Unavailable.
On with <state>/desk-on or [desk] host; HARNESS_DESK=0/1 overrides.

  py -3 desk.py accept --rid R   # desk side (run.cmd)
"""
from __future__ import annotations

import argparse, hashlib, json, os, re, statistics, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Conf:
    host: str
    root: str
    env: str  # cmd.exe prefix
    configured: bool


def load_conf(environ, home: Path) -> Conf:
    """HARNESS_REMOTE_* beat [desk] host, root, env, path (dirs put first on PATH)."""
    toml = home / "harness.toml"
    t = {}
    if toml.exists():
        import tomllib
        t = tomllib.loads(toml.read_text(encoding="utf-8")).get("desk", {})
    env = environ.get("HARNESS_REMOTE_ENV") or t.get("env") or "set CARGO_BUILD_JOBS=3"
    if t.get("path"):
        env += f'&& set "PATH={";".join(t["path"])};%PATH%"'
    return Conf(environ.get("HARNESS_REMOTE_HOST") or t.get("host") or "remote-host",
                environ.get("HARNESS_REMOTE_ROOT") or t.get("root") or "D:\\fleet", env, bool(t.get("host")))


CONF = load_conf(os.environ, Path(os.environ.get("HARNESS_HOME", str(Path(__file__).parent))))
HOST, ROOT, ENV = CONF.host, CONF.root, CONF.env
KIT = ROOT + "\\desk-kit"
RREPO = ROOT + "\\repo.git"
ON = "desk-on"
MAX_UNITS = 12  # place() tries 2^n splits
UNITS = min(MAX_UNITS, int(os.environ.get("HARNESS_DESK_UNITS", "8")))  # the parallel unit + solo units
MIN_TESTS = 60  # fewer (probe, bisect): setup costs more than it saves
POOL = 10  # run_tests_cargo's parallel binaries
LINK_SECS = 6.0
SOLO_SECS = 120.0  # a solo test with no recorded alone run
LATE = (3.0, 1500.0)  # no result after 3 x the unit's cost + 25 min (a cold build): no verdict
POLL_SECS = 20
BUSY_LOAD = 70.0  # cold start: CPU % at gate start above which no unit runs here
LOAD_SECS = 2.0
RATES = "unit-rates.json"  # {machine: {kind: [[ts, wall secs / unit cost], ...]}}, newest last
RATE_KEEP = 3  # samples per machine and kind; the plan uses their median
RATE_TTL = {"here": 12 * 3600.0}  # this machine's load swings; the desk's samples do not age
MACHINES, KINDS = ("here", "desk"), ("par", "solo")
KIT_SIG = "desk-kit.json"
LOCAL_ONLY = ("HARNESS_HOME", "HARNESS_REPO", "HARNESS_WT", "HARNESS_DESK", "HARNESS_LOG_DIR", "HARNESS_QS",
              "HARNESS_REMOTE_", "HARNESS_CLOUD_")  # paths or switches of this machine: never forwarded


class Unavailable(Exception):
    """No desk verdict: never a pass or a fail."""


def on(st: Path) -> bool:
    env = os.environ.get("HARNESS_DESK", "")
    return env == "1" if env in ("0", "1") else (st / ON).exists() or CONF.configured


def offload(h) -> bool:
    """Every test run goes to the desk (cargo splits acceptance only)."""
    return h.P["accept"] != "cargo" and on(h.ST)


def ssh(h, cmd: str, timeout=180) -> subprocess.CompletedProcess:
    try:
        return h.sh(["ssh", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30", "-o", "BatchMode=yes", HOST, cmd],
                    check=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, "", f"timeout {e}")


def scp(h, src, dst: str, timeout=600) -> bool:
    try:
        return h.sh(["scp", "-q", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", str(src), f"{HOST}:{dst}"],
                    check=False, timeout=timeout).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def send(h, sha: str) -> str:
    """main and sha into the desk's bare repo as a git bundle (refs/desk/base: the main it has). "" or why not."""
    work = h.ST / "desk"
    work.mkdir(exist_ok=True)
    h.git("update-ref", "refs/desk/gate", sha)
    base = h.git("rev-parse", "-q", "--verify", "refs/desk/base", check=False).stdout.strip()
    main = h.git("rev-parse", "main").stdout.strip()
    neg = [f"^{base}"] if base else []
    refs = [ref for ref, tip in (("refs/heads/main", main), ("refs/desk/gate", sha))
            if h.git("rev-list", "--count", tip, *neg, check=False).stdout.strip() != "0"]
    cmds, err = [], ""
    if refs:
        bundle = work / "send.bundle"
        bundle.unlink(missing_ok=True)
        r = h.git("bundle", "create", str(bundle), *refs, *neg, check=False)
        if r.returncode != 0 or not scp(h, bundle, f"{ROOT}\\desk.bundle"):
            return "bundle to desk failed: " + r.stderr[-200:]
        cmds.append(f"git -C {RREPO} fetch -q {ROOT}\\desk.bundle +refs/*:refs/desk-in/*")
    cmds += [f"git -C {RREPO} update-ref refs/heads/main {main}", f"git -C {RREPO} update-ref refs/desk/gate {sha}",
             f"git -C {RREPO} rev-parse refs/desk/gate"]
    r = ssh(h, " && ".join(cmds), timeout=600)
    if r.stdout.strip() != sha:
        h.git("update-ref", "-d", "refs/desk/base", check=False)  # the next gate sends everything
        err = "desk repo update failed: " + (r.stdout + r.stderr)[-200:]
    else:
        h.git("update-ref", "refs/desk/base", "main")
    return err


def kit_files(h) -> list:
    """Every module beside the harness and the stack profile: the desk runs this same code."""
    toml = h.HOME / "harness.toml"
    return sorted(Path(h.__file__).resolve().parent.glob("*.py")) + ([toml] if toml.exists() else [])


def runner_scripts(h) -> dict:
    keep = {k: v for k, v in os.environ.items() if k.startswith("HARNESS_") and not k.startswith(LOCAL_ONLY)}
    env = "".join(f'set "{k}={v}"\r\n' for k, v in sorted({**keep, "HARNESS_HOME": KIT, "HARNESS_REPO": RREPO,
                                                           "HARNESS_WT": ROOT + "\\vh"}.items()))
    return {
        "run.cmd": f"@echo off\r\n{ENV}\r\n{env}cd /d {KIT}\r\npy -3 -u desk.py accept --rid %1 > runs\\%1.out 2>&1\r\n",
        "start.ps1": "param($rid)\r\n" f"$k = '{KIT}'\r\n"
                     "Remove-Item \"$k\\runs\\$rid.result.json\" -ErrorAction SilentlyContinue\r\n"
                     "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
                     "@{CommandLine=\"cmd /c `\"$k\\run.cmd`\" $rid\"; CurrentDirectory=$k}\r\n"
                     "Set-Content \"$k\\runs\\$rid.pid\" $r.ProcessId\r\n\"pid $($r.ProcessId) rc $($r.ReturnValue)\"\r\n",
        "stop.cmd": f"@echo off\r\nif exist {KIT}\\runs\\%1.pid for /f %%p in ({KIT}\\runs\\%1.pid) do taskkill /T /F /PID %%p\r\n",
    }


def kit(h) -> str:
    """The kit into <ROOT>\\desk-kit when it changed. "" or why not."""
    local = h.ST / "desk" / "kit"
    local.mkdir(parents=True, exist_ok=True)
    for name, text in runner_scripts(h).items():
        (local / name).write_text(text, encoding="utf-8", newline="")
    files = kit_files(h) + sorted(local.iterdir())
    sig = hashlib.sha1(b"".join(p.name.encode() + p.read_bytes() for p in files) + f"{HOST}|{KIT}".encode()).hexdigest()
    if h.load(KIT_SIG, {}).get("sig") == sig:
        return ""
    ssh(h, f"mkdir {KIT}\\runs {KIT}\\state 2>nul & echo ok")
    bad = [p.name for p in files if not scp(h, p, f"{KIT}\\{p.name}")]
    if bad:
        return "kit scp failed: " + " ".join(bad)
    h.save(KIT_SIG, {"sig": sig})
    return ""


def start(h, rid: str, spec: dict) -> str:
    p = h.ST / "desk" / f"{rid}.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(spec), encoding="utf-8")
    r = ssh(h, f"powershell -NoProfile -ExecutionPolicy Bypass -File {KIT}\\start.ps1 {rid}") \
        if scp(h, p, f"{KIT}\\runs\\{rid}.json") else subprocess.CompletedProcess("", 1, "", "scp failed")
    if "rc 0" in r.stdout:
        return ""
    h.save(KIT_SIG, {})  # the desk may have lost its kit: resend it next gate
    return (r.stdout + r.stderr)[-300:] or f"exit {r.returncode}"


def poll(h, rid: str):
    r = ssh(h, f"type {KIT}\\runs\\{rid}.result.json")
    if r.returncode == 0 and r.stdout.strip().startswith("{"):
        try:
            return json.loads(r.stdout)
        except ValueError:
            pass
    return None


def absorb(h, res: dict, head: str) -> str:
    """Merge a desk result's perf debt and events here. "" or why it is no verdict."""
    with open(h.ST / "perf-debt.jsonl", "a", encoding="utf-8") as fh:
        for line in res.get("perf_debt", []):
            fh.write(line.rstrip() + "\n")
    for e in res.get("events", []):
        try:
            d = json.loads(e)
        except ValueError:
            continue
        h.event("desk_" + d.pop("event", "?"), **{k: v for k, v in d.items() if k not in ("t", "ts")})
    why = res.get("error") or ("" if res.get("head") == head else f"desk head {res.get('head')} != {head}")
    pat = h.P.get("env_fail_re")
    if not why and pat and re.search(pat, res.get("log") or ""):
        why = "desk toolchain env fault"
    return why or unit_env(h, res)


ENV_UNIT = "desk unit did not build: "
REMOVED = re.compile(r"Removed (\d+) files")


def workspace_packages(h, tree: str) -> dict:
    """{package or lib crate name: package} of the desk tree's cargo workspace (metadata --no-deps); {} unreadable."""
    r = ssh(h, f'{ENV}&& cd /d "{tree}" && cargo metadata --no-deps --format-version 1 --offline', timeout=300)
    try:
        pkgs = json.loads(r.stdout)["packages"]
    except (ValueError, KeyError, TypeError):
        return {}
    return {n: p["name"] for p in pkgs for n in [p["name"]] + [t["name"].replace("-", "_") for t in p.get("targets", [])
                                                               if "lib" in t.get("kind", []) or "rlib" in t.get("kind", [])]}


def clean_packages(h, tree: str, target: str, packages: list) -> dict:
    """`cargo clean -p <package> --release` for each package in the desk's own target dir: {package: files removed}.
    A clean that removed nothing counts 0 whatever its exit code (an incremental-dir glob once matched nothing and
    still reported cleaned)."""
    if not packages or not tree:
        return {p: 0 for p in packages}
    each = " & ".join(f'echo @@{p} & cargo clean -p {p} --release --target-dir "{target}" --offline 2>&1'
                      for p in packages)
    out = ssh(h, f'{ENV}&& cd /d "{tree}" && ({each})', timeout=1800).stdout or ""
    got = {p: 0 for p in packages}
    for part in out.split("@@")[1:]:
        name, _, rest = part.partition("\n")
        if name.strip() in got:
            got[name.strip()] = sum(int(n) for n in REMOVED.findall(rest))
    return got


def unit_env(h, res: dict) -> str:
    """A cargo unit that did not build (cargo_failures build_failed) is an env fault, never its tests' fails: event,
    `cargo clean -p` the packages its output names (cargo's unbuilt packages; the linker's archive/object owners and
    unresolved symbols' crates mapped to the desk workspace's packages) in the desk's own target dir, ENV_UNIT + why.
    cleaned is true only when files were removed. "" otherwise. Desk.poll re-runs the unit once."""
    ok = h.P.get("accept") == "cargo" and "tests" in res and res.get("fails") and res.get("target")
    f = h.cf.parse(res.get("log") or "") if ok else h.cf.CargoFailures()
    if not f.build_failed:
        return ""
    tree = res.get("tree") or ""
    ws = workspace_packages(h, tree) if f.linked and tree else {}
    stale = list(dict.fromkeys([ws[c] for c in f.linked if c in ws] + list(f.packages)))
    removed = {p: n for p, n in clean_packages(h, tree, res["target"], stale).items() if n}
    h.event("desk_unit_env", rid=res.get("rid"), unbuilt=list(f.unbuilt), linked=list(f.linked), packages=stale,
            removed=removed, tests=len(res["tests"]), cleaned=bool(removed))
    return ENV_UNIT + ", ".join(f.unbuilt) + (f" (cargo clean -p {' -p '.join(removed)})" if removed
                                              else " (nothing cleaned)")


def run_op(h, spec: dict) -> dict:
    op = spec.get("op", "tests")
    if op == "cmd":
        runs = [{"rc": r.returncode, "out": r.stdout + r.stderr}
                for r in h.unit_runs(spec["cmds"], timeout=spec["timeout"], first_fail=spec["first_fail"])]
        return {"runs": runs, "log": "\n".join(r["out"] for r in runs if r["rc"])[-20000:]}
    if op == "alone":
        return {"red": h.run_alone(spec["tests"]), "tests": spec["tests"]}
    if op != "tests":
        raise ValueError(f"unknown desk op {op!r}")
    lg = h.ST / "last-gate.txt"
    fails = h.run_tests(spec["tests"], solo=spec["solo"])
    stems = {Path(t.split("::")[0]).stem for t in spec["tests"]}
    return dict(fails=fails, tests=spec["tests"], target=h.TRAIN_ENV.get("CARGO_TARGET_DIR", ""), tree=str(h.TRAIN),
                log=lg.read_text(encoding="utf-8", errors="replace")[-20000:] if fails and lg.exists() else "",
                times={s: v for s, v in h.load(h.ACCEPT_TIMES, {}).items() if s in stems},
                alone={t: v for t, v in h.load(h.ALONE_TIMES, {}).items() if t in set(spec["solo"]) | set(fails)})


def accept(h, rid: str) -> dict:
    """Desk side: run runs/<rid>.json at its sha, write its result."""
    os.environ["HARNESS_DESK"] = "0"  # never offload from here
    runs = h.HOME / "runs"
    spec = json.loads((runs / f"{rid}.json").read_text(encoding="utf-8"))

    def lines(f):
        return (h.ST / f).read_text(encoding="utf-8").splitlines() if (h.ST / f).exists() else []
    ev0, pd0, t0, res = len(lines("events.jsonl")), len(lines("perf-debt.jsonl")), time.time(), {"rid": rid}
    try:
        if not (h.TRAIN / ".git").exists():
            h.git("worktree", "add", "-f", "--detach", str(h.TRAIN), spec["sha"])
        h.git("checkout", "-q", "--detach", "-f", spec["sha"], cwd=h.TRAIN)
        h.git("clean", "-qfd", cwd=h.TRAIN, check=False)
        (h.ST / "last-gate.txt").unlink(missing_ok=True)
        res.update(run_op(h, spec))
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e)[-2000:]
    res["head"] = h.git("rev-parse", "HEAD", cwd=h.TRAIN, check=False).stdout.strip()
    res["secs"] = round(time.time() - t0)
    res["events"], res["perf_debt"] = lines("events.jsonl")[ev0:], lines("perf-debt.jsonl")[pd0:]
    tmp = runs / f"{rid}.result.tmp"
    tmp.write_text(json.dumps(res), encoding="utf-8")
    os.replace(tmp, runs / f"{rid}.result.json")
    return res


def balance(groups: dict, cost: dict, n: int) -> list:
    """Longest-first greedy split of {key: [tests]} into at most n bins: [(cost, sorted tests)]. Deterministic."""
    bins = [[0.0, []] for _ in range(max(1, min(n, len(groups))))]
    for g in sorted(groups, key=lambda g: (-cost[g], g)):
        b = min(bins, key=lambda b: b[0])
        b[0] += cost[g]
        b[1] += groups[g]
    return [(c, sorted(ts)) for c, ts in bins if ts]


def units(tests, solo: set, times: dict, alone: dict, n: int) -> list:
    """[{"cost", "tests", "solo"}], each test in one: the parallel unit first, then up to n - 1 solo units by alone
    seconds, longest first (pick()'s priority order)."""
    par = sorted(t for t in set(tests) if t not in solo)
    sol = {t: [t] for t in sorted(set(tests) & set(solo))}
    pc = sum((times.get(s, 5.0) + LINK_SECS) / POOL for s in {Path(t.split("::")[0]).stem for t in par})
    out = [{"cost": pc, "tests": par, "solo": []}] if par else []
    if not sol:
        return out
    sc = {t: float(alone.get(t, SOLO_SECS)) for t in sol}
    return out + sorted(({"cost": c, "tests": ts, "solo": ts} for c, ts in balance(sol, sc, max(1, n - 1))),
                        key=lambda u: (-u["cost"], u["tests"]))


def cpu_load(secs: float = LOAD_SECS):
    """CPU busy % over `secs`, None if unreadable (the idle split). Never raises."""
    try:
        if os.name != "nt":
            return 100.0 * os.getloadavg()[0] / (os.cpu_count() or 1)
        import ctypes
        from ctypes import wintypes

        def snap():
            ft = [wintypes.FILETIME() for _ in range(3)]  # idle, kernel + idle, user
            if not ctypes.windll.kernel32.GetSystemTimes(*(ctypes.byref(f) for f in ft)):
                raise OSError("GetSystemTimes failed")
            return [(f.dwHighDateTime << 32) | f.dwLowDateTime for f in ft]
        a = snap()
        time.sleep(secs)
        idle, kern, user = (y - x for x, y in zip(a, snap()))
        return 100.0 * (kern + user - idle) / (kern + user) if kern + user > 0 else None
    except Exception:  # noqa: BLE001
        return None


def local_ok(load) -> bool:
    """Units may run here: load unknown or at most BUSY_LOAD."""
    return load is None or load <= BUSY_LOAD


def kind(u: dict) -> str:
    return "solo" if u["solo"] else "par"


def learn(hist: dict, work: dict, ts: float) -> dict:
    """hist plus a gate's work {machine: {kind: [wall secs, cost]}}."""
    out = {m: {k: list(v) for k, v in ks.items()} for m, ks in hist.items()}
    for m, ks in work.items():
        for k, (secs, cost) in ks.items():
            if cost > 0:
                out.setdefault(m, {}).setdefault(k, []).append([round(ts), round(secs / cost, 4)])
                out[m][k] = out[m][k][-RATE_KEEP:]
    return out


def rates(hist: dict, here: bool, now: float) -> dict:
    """{(machine, kind): wall secs per cost-second}: median of fresh samples; this machine's load slows both kinds, so
    else its other kind's; else cold: desk 1.0, here the desk's when here, never when busy."""
    def med(m, k):
        xs = [r for ts, r in hist.get(m, {}).get(k, []) if now - ts <= RATE_TTL.get(m, float("inf"))]
        return statistics.median(xs) if xs else None
    out = {}
    for m in ("desk", "here"):
        for k, o in zip(KINDS, reversed(KINDS)):
            r = med(m, k)
            r = med(m, o) if r is None and m == "here" else r
            out[m, k] = r if r is not None else 1.0 if m == "desk" else out["desk", k] if here else float("inf")
    return out


@dataclass(frozen=True)
class Plan:
    here: frozenset  # units here; the desk runs the rest
    steal: frozenset  # desk units here may take when idle (not slower at them)
    back: frozenset  # units here the idle desk may take (not slower at them)
    expect: tuple  # planned wall secs (here, desk)


def place(us: list, rate: dict) -> Plan:
    """Of every split (<= MAX_UNITS units), the one with the earliest later finish; ties: less here."""
    inf = float("inf")
    t = [{m: u["cost"] * rate[m, kind(u)] if rate[m, kind(u)] != inf else inf for m in MACHINES} for u in us]

    def cost(s):
        here = sum(t[k]["here"] for k in s)
        return max(here, sum(x["desk"] for k, x in enumerate(t) if k not in s)), here, sorted(s)
    best = min((frozenset(k for k in range(len(us)) if b >> k & 1) for b in range(1 << len(us))), key=cost)
    _, here, _ = cost(best)
    rest = frozenset(range(len(us))) - best
    return Plan(best, frozenset(k for k in rest if t[k]["here"] <= t[k]["desk"]),
                frozenset(k for k in best if t[k]["desk"] <= t[k]["here"]),
                (round(here), round(sum(t[k]["desk"] for k in rest))))


def pick(us: list, todo: list, machine: str, desk_up: bool = True, here: bool = True, plan: Plan = None) -> list:
    """The unit indices `machine` ("here" | "desk") runs next as one run_tests() call; todo: unstarted indices;
    plan default: the cold start. Own units first, then steal (here the desk's first, the desk here's last). Desk out of the gate (desk_up False): here every unit left in one call."""
    plan = plan or place(us, rates({}, here, 0.0))
    if machine == "here":
        if not desk_up:
            return sorted(todo)
        return ([k for k in sorted(todo) if k in plan.here] or [k for k in sorted(todo) if k in plan.steal])[:1]
    return ([k for k in sorted(todo) if k not in plan.here] or [k for k in sorted(todo) if k in plan.back][-1:])[:1]


PICK = "desk-pick.json"  # {"pid", "start", "rid"} of harness.pick_accept_queue's desk run


def pick_stop(h) -> None:
    """The gate takes the desk: kill the pick acceptance pass (same pid + start only), then its desk run."""
    cur = h.load(PICK, None)
    if not cur:
        return
    (h.ST / PICK).unlink(missing_ok=True)
    if not cur.get("start") or h.proc_start(cur["pid"]) != cur["start"]:
        pass  # a reused pid or no identity: kill nothing here
    elif os.name == "nt":
        h.sh(["taskkill", "/T", "/F", "/PID", str(cur["pid"])], check=False)
    else:
        try:
            os.kill(cur["pid"], 15)
        except OSError:
            pass
    ssh(h, f"{KIT}\\stop.cmd {cur['rid']}", timeout=120)
    h.event("pick_accept_preempted", rid=cur["rid"])


def remote(h, rid: str, sha: str, spec: dict, limit: float, alive=lambda: True, poll_secs: float = POLL_SECS) -> dict:
    """`spec` at `sha` on the desk, its result absorbed; Unavailable when late or not alive()."""
    t0 = time.time()
    if ssh(h, "echo ok", timeout=60).stdout.strip() != "ok":
        raise Unavailable(f"{HOST} unreachable")
    why = send(h, sha) or kit(h)
    why = why or (start(h, rid, {**spec, "rid": rid, "sha": sha}) if alive() else "preempted")
    if why:
        raise Unavailable(f"desk run did not start: {why[-200:]}")
    sent = time.time()
    while (res := poll(h, rid)) is None:
        if not alive() or time.time() - sent > limit:
            ssh(h, f"{KIT}\\stop.cmd {rid}", timeout=120)
            raise Unavailable(f"desk run {rid} preempted or late")
        time.sleep(poll_secs)
    why = absorb(h, res, sha)
    if why:
        raise Unavailable(why)
    return {**res, "send_secs": round(sent - t0)}


HELD = "desk-held.json"  # the train's desk job


def held(h) -> bool:
    cur = h.load(HELD, None)
    return bool(cur) and h.pid_alive(cur["pid"])


def job(h, op: str, limit: float, **spec) -> dict:
    """One train test run on the desk at TRAIN's HEAD: op tests, alone or cmd."""
    sha = h.git("rev-parse", "HEAD", cwd=h.TRAIN).stdout.strip()
    rid = f"{op}-{sha[:12]}-{int(time.time()) % 1000000}"
    h.save(HELD, {"pid": os.getpid(), "rid": rid})
    try:
        pick_stop(h)  # after HELD
        res = remote(h, rid, sha, {"op": op, **spec}, limit + LATE[1])
    except Unavailable as e:
        h.event("desk_error", op=op, rid=rid, reason=str(e)[-300:])
        raise
    finally:
        (h.ST / HELD).unlink(missing_ok=True)
    fill_times(h, res.get("times", {}), res.get("alone", {}))
    h.event("desk_job", op=op, rid=rid, secs=res.get("secs"), send_secs=res["send_secs"],
            fails=len(res.get("fails") or res.get("red") or []))
    return res


def fill_times(h, times: dict, alone: dict) -> None:
    """Desk seconds fill only gaps here."""
    h.save(h.ACCEPT_TIMES, {**times, **h.load(h.ACCEPT_TIMES, {})})
    h.save(h.ALONE_TIMES, {**alone, **h.load(h.ALONE_TIMES, {})})


def pick_run(h, commit: str, tests: list, run_secs: float = 1200, poll_secs: float = 15) -> dict:
    """run_tests(tests) at `commit` on the desk, plus send_secs; h.NoVerdict when not run to the end."""
    rid = f"pick-{commit[:12]}-{int(time.time()) % 1000000}"
    h.save(PICK, {"pid": os.getpid(), "start": h.proc_start(os.getpid()), "rid": rid})

    def mine() -> bool:  # pick_stop removes the record
        return (h.load(PICK, None) or {}).get("rid") == rid
    try:
        if h.in_acceptance() or held(h):
            raise h.NoVerdict("the train holds the desk")
        return remote(h, rid, commit, {"tests": tests, "solo": sorted(h.solo_tests(tests))}, run_secs,
                      alive=lambda: mine() and not h.in_acceptance(), poll_secs=poll_secs)
    except Unavailable as e:
        raise h.NoVerdict(str(e)) from None
    finally:
        if mine():
            (h.ST / PICK).unlink(missing_ok=True)


class Desk:
    """The desk for one gate, one unit at a time. why != "": not usable."""

    def __init__(self, h, gid: str, sha: str):
        self.h, self.gid, self.sha, self.k, self.t0, self.env = h, gid, sha, None, 0.0, set()
        self.why = self.setup()

    def setup(self) -> str:
        pick_stop(self.h)
        if ssh(self.h, "echo ok", timeout=60).stdout.strip() != "ok":
            return f"{HOST} unreachable"
        return send(self.h, self.sha) or kit(self.h)

    def rid(self, k: int) -> str:
        return f"{self.gid}-{k}" + ("-env" if k in self.env else "")

    def start(self, k: int, unit: dict) -> bool:
        why = start(self.h, self.rid(k), {"rid": self.rid(k), "sha": self.sha, "tests": unit["tests"],
                                          "solo": unit["solo"]})
        self.why = f"unit {k} did not start: {why}" if why else ""
        if not why:
            self.k, self.t0 = k, time.time()
        return not why

    def poll(self, unit: dict):
        """None while the unit runs, else (k, result); result None = no verdict (self.why says why)."""
        k, res = self.k, poll(self.h, self.rid(self.k))
        if res is None:
            if time.time() - self.t0 < LATE[0] * unit["cost"] + LATE[1]:
                return None
            ssh(self.h, f"{KIT}\\stop.cmd {self.rid(k)}", timeout=120)
            self.why = f"unit {k} late ({round(time.time() - self.t0)}s)"
        else:
            why = absorb(self.h, res, self.sha)
            if why.startswith(ENV_UNIT) and k not in self.env:
                self.env.add(k)
                if self.start(k, unit):
                    return None
                why = self.why
            if not why and sorted(res.get("tests") or []) != unit["tests"]:
                why = "desk ran other tests than its unit"
            self.why = f"unit {k}: {why[-300:]}" if why else ""
        self.k = None
        return k, (None if self.why else res)


def run(h, tests, timeout=1800):
    """Same contract as h.run_tests(tests)."""
    if h.P["accept"] != "cargo" or len(set(tests)) < MIN_TESTS:
        return h.run_tests(tests, timeout=timeout)
    return run_desk(h, tests, timeout)


def run_desk(h, tests, timeout=1800, desk_cls=Desk):
    t0 = time.time()
    load = cpu_load()
    here = local_ok(load)
    us = units(tests, h.solo_tests(tests), h.load(h.ACCEPT_TIMES, {}), h.load(h.ALONE_TIMES, {}), UNITS)
    hist = h.load(RATES, {})
    rate = rates(hist, here, t0)
    plan = place(us, rate)
    sha = h.git("rev-parse", "HEAD", cwd=h.TRAIN).stdout.strip()
    gid = f"{sha[:12]}-{int(t0) % 1000000}"
    fails, logs, where, todo = {}, {}, {}, list(range(len(us)))
    busy, dtimes, dalone = {"here": 0.0, "desk": 0.0}, {}, {}
    work = {m: {} for m in MACHINES}  # {machine: {kind: [wall secs, cost]}}
    lg = h.ST / "last-gate.txt"

    def local(ks):
        s0 = time.time()
        lg.unlink(missing_ok=True)
        f = h.run_tests(sorted(t for k in ks for t in us[k]["tests"]), timeout=timeout,
                        solo=sorted(t for k in ks for t in us[k]["solo"]))
        return ks, f, (lg.read_text(encoding="utf-8", errors="replace") if f and lg.exists() else ""), time.time() - s0

    def take(k, f, log, machine, secs):  # a unit's first verdict wins
        if k not in fails:
            fails[k], where[k] = f, machine
            busy[machine] += secs
            if log:
                logs[k] = log

    def did(machine, ks, wall):  # a run of one kind of unit is a throughput sample
        if len({kind(us[k]) for k in ks}) == 1:
            w = work[machine].setdefault(kind(us[ks[0]]), [0.0, 0.0])
            w[0], w[1] = w[0] + wall, w[1] + sum(us[k]["cost"] for k in ks)

    def give(machine, desk_up):
        ks = pick(us, todo, machine, desk_up, plan=plan)
        todo[:] = [k for k in todo if k not in ks]
        return ks

    with ThreadPoolExecutor(1) as pool:
        ks = give("here", True)
        fut = pool.submit(local, ks) if ks else None
        desk, skipped = None, "every unit here"
        if len(plan.here) < len(us):
            desk = desk_cls(h, gid, sha)
            skipped = desk.why
        if skipped:
            h.event("desk_skip", gate=gid, reason=skipped[-300:])
            desk = None
        while len(fails) < len(us):
            if fut is not None and fut.done():
                ks, f, log, secs = fut.result()
                mine = [{t for t in f if t in set(us[k]["tests"])} for k in ks]
                mine[0] |= set(f) - set().union(*mine)  # a name of no unit ("build") counts too
                for i, k in enumerate(ks):
                    take(k, sorted(mine[i]), log if i == 0 else "", "here", secs if i == 0 else 0.0)
                did("here", ks, secs)
                fut = None
            if desk is not None and desk.k is not None:
                got = desk.poll(us[desk.k])
                if got is not None and got[1] is None:
                    h.event("desk_error", gate=gid, reason=desk.why)
                    desk = None
                    todo.append(got[0])
                elif got is not None:
                    if got[0] not in fails:
                        did("desk", [got[0]], time.time() - dt0)
                    take(got[0], got[1]["fails"], got[1].get("log", ""), "desk", got[1].get("secs", 0))
                    dtimes.update(got[1].get("times", {}))
                    dalone.update(got[1].get("alone", {}))
            todo[:] = [k for k in todo if k not in fails]
            if fut is None:
                ks = give("here", desk is not None)
                fut = pool.submit(local, ks) if ks else None
            if desk is not None and desk.k is None:
                ks, dt0 = give("desk", True), time.time()
                if ks and not desk.start(ks[0], us[ks[0]]):
                    h.event("desk_error", gate=gid, reason=desk.why)
                    desk = None
                    todo.append(ks[0])
            if len(fails) < len(us) and (fut is None or not fut.done()):
                time.sleep(POLL_SECS)
    fill_times(h, dtimes, dalone)
    h.save(RATES, learn(hist, work, time.time()))
    work = {m: {k: [round(s), round(c)] for k, (s, c) in ks.items()} for m, ks in work.items() if ks}
    out = sorted({t for f in fails.values() for t in f})
    if out:
        lg.write_text("\n".join(logs[k] for k in sorted(logs))[-20000:], encoding="utf-8")
    h.event("gate_stage", stage="acceptance-desk", gate=gid, units=len(us),
            desk=sum(1 for v in where.values() if v == "desk"), desk_skip=skipped[-200:],
            solo_units=sum(1 for u in us if u["solo"]),
            solo_here=sum(1 for k, v in where.items() if v == "here" and us[k]["solo"]),
            here_secs=round(busy["here"]), desk_secs=round(busy["desk"]), secs=round(time.time() - t0),
            cpu_load=None if load is None else round(load), local_ok=here, n=len(set(tests)), fails=len(out),
            rates={f"{m}/{k}": round(r, 3) for (m, k), r in rate.items() if r != float("inf")},
            plan_here=sorted(plan.here), expect=list(plan.expect), work=work)
    want = set(tests)
    return [f for f in out if f in want] or out


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("accept").add_argument("--rid", required=True)
    a = ap.parse_args()
    import harness
    res = accept(harness, a.rid)
    print(json.dumps({k: res.get(k) for k in ("fails", "error", "secs")})[:2000], flush=True)


if __name__ == "__main__":
    sys.exit(main())
