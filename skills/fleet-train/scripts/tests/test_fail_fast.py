"""Tests of the pick check (harness.pick_screen) and the machine-wide cargo cap (cargo_slots.py + alarm_dispatch).
Temp state, worktrees and a fixture git repo; the static checks, reviewer and launcher are fakes.

  py -3 tests/test_fail_fast.py
"""
from __future__ import annotations

import json, os, subprocess, sys, tempfile, threading, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-failfast-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt")})
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import cargo_slots as C  # noqa: E402
import test_alarm_dispatch as T  # noqa: E402  (fake_h, FakeLauncher)
import alarm_dispatch as D  # noqa: E402
import alarm_handlers as H  # noqa: E402
import harness as h  # noqa: E402

check, FAILS = T.check, T.FAILS
CARGO_ALARM = next(f"{k} x" for k, v in H.TABLE.items() if isinstance(v, D.Agent) and v.slot and not v.per_subject)


def spawn_with(ln):
    return lambda: ln.start(None, None, None, "", 0)


def test_cargo_cap_shared_by_fleets():
    with tempfile.TemporaryDirectory() as td:
        ln = T.FakeLauncher()
        a, b = C.CargoSlots(ln.alive, Path(td) / "s.json", 3), C.CargoSlots(ln.alive, Path(td) / "s.json", 3)
        for i in range(3):
            (a if i % 2 else b).claim(f"fleet{i % 2}", f"t{i}", spawn_with(ln))
        try:
            a.claim("fleet0", "t3", spawn_with(ln))
            check("claim over the cap is refused", False)
        except C.Full as x:
            check("claim over the cap is refused, naming the holders", "fleet1:t1" in str(x), x)
        check("a refused claim spawns nothing", len(ln.started) == 3)
        ln.kill(sorted(ln.live)[0])
        check("a dead holder frees its slot", len(b.holders()) == 2)
        held = json.loads((Path(td) / "s.json").read_text(encoding="utf-8"))
        ln.live[held[-1]["pid"]] = "reused"
        check("a reused pid (other start time) holds nothing", len(a.holders()) == 1)


def test_cargo_claims_race():
    with tempfile.TemporaryDirectory() as td:
        ln, won, lk = T.FakeLauncher(), [], threading.Lock()

        def one(i):
            def spawn():
                with lk:
                    return ln.start(None, None, None, "", 0)
            try:
                C.CargoSlots(ln.alive, Path(td) / "s.json", 3).claim("f", str(i), spawn)
                won.append(i)
            except C.Full:
                pass
        ts = [threading.Thread(target=one, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("8 racing claims: exactly 3 win", len(won) == 3 and len(ln.started) == 3, (won, len(ln.started)))


def test_dispatch_cap_and_acceptance():
    with tempfile.TemporaryDirectory() as td:
        h_, ln = T.fake_h(Path(td)), T.FakeLauncher()
        cargo = C.CargoSlots(ln.alive, Path(td) / "s.json", 1)
        cargo.claim("other", "x", spawn_with(ln))
        d = D.Dispatcher(h_, H.TABLE, H.alarm_id, ln, briefs=HERE / "alarm_briefs", cargo=cargo, fleet="me")
        st = d.run([CARGO_ALARM])
        key = next(iter(st))
        check("cap held by another fleet: no cargo agent", len(ln.started) == 1 and "cargo slots" in st[key].get("note", ""),
              st[key])
        ln.kill(sorted(ln.live)[0])
        ln.live[999] = "train"
        h_.save(D.TRAIN_STAGE, {"stage": "acceptance", "pid": 999})
        st = d.run([CARGO_ALARM])
        check("train in acceptance: no cargo agent", len(ln.started) == 1 and "acceptance" in st[key].get("note", ""), st[key])
        ln.live.pop(999)
        d.run([CARGO_ALARM])
        check("dead train's mark ignored; agent holds the slot", len(ln.started) == 2
              and [x["fleet"] for x in cargo.holders()] == ["me"], cargo.holders())


def test_harness_marks_acceptance():
    seen = {}
    with h.train_stage("acceptance"):
        seen.update(h.load(h.TRAIN_STAGE, {}))
    check("stage marked with the train's pid, cleared after", seen.get("pid") == os.getpid()
          and h.load(h.TRAIN_STAGE, None) == {} and h.TRAIN_STAGE == D.TRAIN_STAGE, seen)


def git(*a):
    return subprocess.run(["git", *a], cwd=str(h.REPO), capture_output=True, text=True, check=True).stdout.strip()


def test_pick_screen():
    h.REPO.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (h.REPO / "a.txt").write_text("a\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "main")
    queue = []
    for i, r in enumerate(("R1", "R2", "R3")):
        git("checkout", "-q", "-B", r, "main")
        (h.REPO / f"{r}.txt").write_text(r, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", r)
        queue.append({"row": r, "sha": git("rev-parse", "HEAD"), "ts": i})
        git("checkout", "-q", "main")
    h.save("queue.json", queue)
    reviews, main_red = [], [False]

    def static(cwd, base="main", plan=True):
        if main_red[0]:
            return "main problem"
        return "R2.txt: 1 x S25 expect" if (Path(cwd) / "R2.txt").exists() else ""

    def review(merged, cwd=None, base="main"):
        reviews.append(merged[0]["row"])
        return [{"file": "R3.txt", "fn": "f", "existing": "a.txt:1"}] if merged[0]["row"] == "R3" else []
    h.static_problems, h.batch_review = static, review
    h.adds_fn = lambda q, d: q["row"] == "R3"
    bad = h.pick_screen(queue)
    rej = h.load("rejects.json", {})
    check("static failure rejected with its message", "S25" in rej.get("R2", [{}])[0].get("reason", ""), rej)
    check("reviewer duplicate rejected", "duplicate capability" in rej.get("R3", [{}])[0].get("reason", ""), rej)
    check("rejected rows dequeued", bad == {queue[1]["sha"], queue[2]["sha"]}
          and [q["row"] for q in h.load("queue.json", [])] == ["R1"])
    check("no review for a row the static checks reject", "R2" not in reviews, reviews)
    n = len(reviews)
    h.pick_screen(h.load("queue.json", []))
    check("a passed sha is not checked again", len(reviews) == n, reviews)
    main_red[0] = True
    q4 = dict(queue[1], row="R4", sha=queue[1]["sha"])
    h.save("queue.json", h.load("queue.json", []) + [q4])
    h.save(h.PICK_CACHE, {})
    check("a main red on the static checks rejects no row", h.pick_screen([q4]) == set())


def test_pick_plan_stubs():
    """The pick check runs the row-local plan check with the picked row as landing; older cached passes run again."""
    head = "| id | phase | module | deps | entry | acceptance | status | files | needs |\n" + "|---" * 9 + "|\n"
    fn_row = lambda rid: f"| {rid} | 1 | a | - | cli: x {rid} | tests/acceptance/{rid}.rs::t | planned | {rid}.rs | stub |\n"  # noqa: E731
    body = lambda b: f"pub fn f() -> u32 {{\n    {b}\n}}\n"  # noqa: E731
    git("checkout", "-q", "main")
    # overhaul.toml: the plan check applies only to a repo holding an overhaul plan (rules_gate.plan_applies)
    for rel, text in {"spec/FUNCTIONS.md": head + fn_row("S1") + fn_row("S2"), "S1.rs": body("todo!()"),
                      "S2.rs": body("todo!()"), "overhaul.toml": ""}.items():
        (h.REPO / rel).parent.mkdir(parents=True, exist_ok=True)
        (h.REPO / rel).write_text(text, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "plan S1 S2")
    rows = {}
    for r, files in (("S1", {"S1.rs": body("1")}), ("S2", {"S1.rs": body("1"), "S2.rs": body("2")})):
        git("checkout", "-q", "-B", r, "main")
        for rel, text in files.items():
            (h.REPO / rel).write_text(text, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", r)
        rows[r] = {"row": r, "sha": git("rev-parse", "HEAD"), "ts": 30}
        git("checkout", "-q", "main")
    h.static_problems, h.batch_review = (lambda cwd, base="main", plan="spec", landing=(): ""), (lambda *a, **k: [])
    h.save("queue.json", list(rows.values()))
    h.save(h.PICK_CACHE, {})
    check("plan: a row that spends only its own stub passes the pick check", h.pick_screen([rows["S1"]]) == set())
    bad = h.pick_screen([rows["S2"]])
    rej = h.load("rejects.json", {}).get("S2", [{}])[-1].get("reason", "")
    check("plan: a row that spends another planned row's stub is rejected",
          bad == {rows["S2"]["sha"]} and "S1: no stub in S1.rs" in rej and "S2:" not in rej, rej)
    h.save("queue.json", [rows["S1"]])
    h.save(h.PICK_CACHE, {rows["S1"]["sha"]: {"row": "S1", "base": "x", "reviewed": True}})  # a pass from older checks
    seen = []
    h.static_problems = lambda cwd, base="main", plan="spec", landing=(): seen.append(1) or ""
    h.pick_screen([rows["S1"]])
    check("plan: a pass cached by older pick checks is run again", len(seen) >= 1, seen)


BUILD = ("import pathlib, sys\nif 'v2' in pathlib.Path('api.txt').read_text() and 'old' in pathlib.Path('caller.txt')"
         ".read_text():\n    print('error[E0599]: caller.txt calls old, api.txt is v2')\n    sys.exit(1)\n")


def test_pick_build():
    """The pick check compiles main + row (P["pick_build"]) once per main: a row whose base went stale under a landed
    row is rejected; a clean row passes; a main that fails the same compile rejects no row; acceptance = no verdict."""
    git("checkout", "-q", "main")
    for rel, text in {"build.py": BUILD, "api.txt": "v1\n", "caller.txt": "new\n"}.items():
        (h.REPO / rel).write_text(text, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "buildable main")
    shas = {}
    for r, rel, text in (("X", "api.txt", "v2\n"), ("Y", "y.txt", "y\n")):
        git("checkout", "-q", "-B", r, "main")
        (h.REPO / rel).write_text(text, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", r)
        shas[r] = {"row": r, "sha": git("rev-parse", "HEAD"), "ts": 40}
        git("checkout", "-q", "main")
    h.static_problems, h.batch_review = (lambda *a, **k: ""), (lambda *a, **k: [])
    h.P.update({"pick_build": f'"{sys.executable}" build.py', "pick_env": {}, "pick_err_re": r"^error"})
    h.save(h.PICK_CACHE, {})
    h.save("queue.json", list(shas.values()))
    check("build: both rows compile on the main they were built on", h.pick_screen(list(shas.values())) == set())
    (h.REPO / "caller.txt").write_text("old\n", encoding="utf-8")
    git("commit", "-q", "-am", "land a caller of the old api")
    h.save(h.TRAIN_STAGE, {"stage": "acceptance", "pid": os.getpid()})
    check("build: train in acceptance -> no compile, no verdict", h.pick_screen(list(shas.values())) == set())
    h.save(h.TRAIN_STAGE, {})
    bad = h.pick_screen(list(shas.values()))
    rej = h.load("rejects.json", {}).get("X", [{}])[-1].get("reason", "")
    check("build: main moved -> the stale row is compiled again and rejected with the compiler's error",
          bad == {shas["X"]["sha"]} and rej.startswith(h.PICK_BUILD_RED) and "E0599" in rej, (bad, rej))
    (h.REPO / "api.txt").write_text("v2\n", encoding="utf-8")
    git("commit", "-q", "-am", "main red on the same compile")
    h.save("queue.json", [shas["X"]])
    check("build: a main failing the same compile rejects no row", h.pick_screen([shas["X"]]) == set())


def test_pick_accept():
    """A row breaking a proven row's test (owner of a file it changes) is red on the desk and rejected at pick while
    main's code is unchanged (a spec-only commit keeps it); a code change on main drops the verdict."""
    git("checkout", "-q", "-f", "main")
    row = "| {} | 1 | m | | e | tests/{}.rs::t | {} | {} |\n"
    (h.REPO / "spec").mkdir(exist_ok=True)
    (h.REPO / "spec/FUNCTIONS.md").write_text(row.format("P1", "p1", "proven", "src/a.rs")
                                              + row.format("P2", "p2", "proven", "src/b.rs")
                                              + row.format("X", "x", "planned", "src/a.rs"), encoding="utf-8")
    (h.REPO / "src").mkdir(exist_ok=True)
    for f in ("a", "b"):
        (h.REPO / f"src/{f}.rs").write_text(f"// {f}\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "accept main")
    git("checkout", "-q", "-B", "X", "main")
    (h.REPO / "src/a.rs").write_text("// a changed\n", encoding="utf-8")
    git("commit", "-q", "-am", "X")
    x = {"row": "X", "sha": git("rev-parse", "HEAD"), "ts": 0}
    git("checkout", "-q", "main")
    main = git("rev-parse", "main")
    ran = []

    def run(commit, tests):
        ran.append(tests)
        return {"fails": [t for t in tests if "p1" in t], "log": "p1 failed", "send_secs": 1}
    v = h.pick_accept_check(x, main, run=run)
    check("accept: the owner tier is the changed file's proven owner only (never a planned row's test)",
          ran == [["tests/p1.rs::t"]], ran)
    check("accept: red on the desk", v and not v["ok"] and v["fails"] == ["tests/p1.rs::t"], v)
    h.save(h.PICK_ACCEPT, {x["sha"]: v})
    check("accept: with the switch off no verdict rejects", h.pick_accept_red(x["sha"], main) == "")
    (h.ST / h.PICK_ACCEPT_ON).write_text("", encoding="utf-8")
    h.save(h.PICK_CACHE, {})
    h.save("queue.json", [x])
    h.static_problems = lambda cwd, base="main", plan=True: ""
    h.batch_review = lambda merged, cwd=None, base="main": []
    check("accept: rejected at pick", h.pick_screen([x]) == {x["sha"]}
          and "p1.rs" in h.load("rejects.json", {})["X"][-1]["reason"], h.load("rejects.json", {}).get("X"))
    (h.REPO / "spec/FUNCTIONS.md").write_text("spec only\n", encoding="utf-8")
    git("commit", "-q", "-am", "spec only")
    check("accept: a spec-only main keeps the verdict", h.pick_accept_red(x["sha"], git("rev-parse", "main")) != "")
    (h.REPO / "src/b.rs").write_text("// b changed\n", encoding="utf-8")
    git("commit", "-q", "-am", "code")
    check("accept: other code on main drops it", h.pick_accept_red(x["sha"], git("rev-parse", "main")) == "")
    import desk, time  # noqa: E401
    desk.ssh = lambda h_, cmd, timeout=180: subprocess.CompletedProcess(cmd, 0, "", "")
    for name, start_of, want in (("accept: the pass that wrote the record is killed", h.proc_start, True),
                                 ("accept: a reused pid is never killed", lambda pid: "other", False)):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        time.sleep(0.5)
        h.save(desk.PICK, {"pid": child.pid, "start": start_of(child.pid), "rid": "pick-x"})
        desk.pick_stop(h)
        try:
            child.wait(timeout=10)
            killed = True
        except subprocess.TimeoutExpired:
            killed = False
            child.kill()
        check(name, killed == want and not (h.ST / desk.PICK).exists(), killed)


def test_pick_regen():
    """The pick check regenerates generated files from the merged tree before its static checks, as the train does
    before its quality stage: a row that changes a generator's input passes; a failing regen command rejects it."""
    git("checkout", "-q", "main")
    for rel in ("src.txt", "gen.txt"):
        (h.REPO / rel).write_text("v1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "generator input + output")
    rows = {}
    for r, v in (("G1", "v2\n"), ("G2", "v3\n")):
        git("checkout", "-q", "-B", r, "main")
        (h.REPO / "src.txt").write_text(v, encoding="utf-8")  # the input changes; the generated file is left stale
        git("add", "-A")
        git("commit", "-q", "-m", r)
        rows[r] = {"row": r, "sha": git("rev-parse", "HEAD"), "ts": 40}
        git("checkout", "-q", "main")
    gen = [sys.executable, "-c", "import shutil; shutil.copy('src.txt', 'gen.txt')"]
    real_sh, saved = h.sh, (h.P.get("regen"), h.P.get("pick_build"))
    h.sh = lambda c, **k: (real_sh(gen, **k) if c == "GEN" else
                           subprocess.CompletedProcess(c, 3, "", "boom") if c == "FAIL" else real_sh(c, **k))
    h.static_problems = lambda cwd, base="main", plan="spec", landing=(): (
        "" if (Path(cwd) / "gen.txt").read_text() == (Path(cwd) / "src.txt").read_text() else "gen.txt: drift")
    h.batch_review = lambda *a, **k: []
    h.P["pick_build"] = ""
    try:
        h.P["regen"] = ["GEN"]
        h.save("queue.json", [rows["G1"]])
        h.save(h.PICK_CACHE, {})
        check("regen: a row whose generated file the train would regenerate passes the pick check",
              h.pick_screen([rows["G1"]]) == set(), h.load("rejects.json", {}).get("G1"))
        h.P["regen"] = ["FAIL"]
        h.save("queue.json", [rows["G2"]])
        bad = h.pick_screen([rows["G2"]])
        rej = h.load("rejects.json", {}).get("G2", [{}])[-1].get("reason", "")
        check("regen: a failing regen command rejects the row with its text",
              bad == {rows["G2"]["sha"]} and "exited 3" in rej, rej)
    finally:
        h.sh, (h.P["regen"], h.P["pick_build"]) = real_sh, saved


def branches(names, file_of=lambda r: f"{r}.txt", ts=50) -> dict:
    """{row: queue entry} of one commit per row on main, each writing file_of(row)."""
    git("checkout", "-q", "-f", "main")
    out = {}
    for i, r in enumerate(names):
        git("checkout", "-q", "-B", r, "main")
        (h.REPO / file_of(r)).write_text(r, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", r)
        out[r] = {"row": r, "sha": git("rev-parse", "HEAD"), "ts": ts + i}
        git("checkout", "-q", "main")
    return out


def pick_compiles(n0: int) -> list:
    return [e for e in h.read_events()[n0:] if e["event"] == "pick_compile"]


def test_pick_parallel():
    """3 pick slots give the rejects, cache and pick_check verdicts of 1 slot, with rows really checked side by side;
    the compile stays one at a time on its one worktree."""
    rows = list(branches([f"PP{i}" for i in range(6)]).values())
    live, peak, lk = [0], [0], threading.Lock()

    def static(cwd, base="main", plan="spec", landing=()):
        with lk:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.3)
        with lk:
            live[0] -= 1
        return "PP2.txt: 1 x S25 expect" if (Path(cwd) / "PP2.txt").exists() else ""
    h.static_problems = static
    h.batch_review = lambda merged, cwd=None, base="main": (
        [{"file": "PP4.txt", "fn": "f", "existing": "a.txt:1"}] if merged[0]["row"] == "PP4" else [])
    h.adds_fn = lambda q, d: q["row"] in ("R3", "PP4")
    mark, clash = TMP / "compiling", TMP / "clash"
    (TMP / "pb.py").write_text(chr(10).join((
        "import pathlib, time", f"m, c = pathlib.Path(r'{mark}'), pathlib.Path(r'{clash}')",
        "if m.exists():", "    c.write_text('x')", "m.write_text('x')", "time.sleep(0.3)", "m.unlink()", "")),
                               encoding="utf-8")
    h.P.update({"pick_build": f'"{sys.executable}" "{TMP / "pb.py"}"', "pick_env": {}, "pick_err_re": r"^error"})
    h.save(h.TRAIN_STAGE, {})

    def run(workers):
        h.save(h.PICK_CACHE, {})
        h.save("rejects.json", {})
        h.save("queue.json", [dict(q) for q in rows])
        n0, peak[0] = len(h.read_events()), 0
        bad = h.pick_screen([dict(q) for q in rows], workers=workers)
        ev = h.read_events()[n0:]
        rej = {r: v[-1]["reason"] for r, v in h.load("rejects.json", {}).items()}
        verdicts = sorted((e["row"], e["ok"]) for e in ev if e["event"] == "pick_check")
        return bad, rej, h.load(h.PICK_CACHE, {}), verdicts, peak[0], len(pick_compiles(n0))
    one, three = run(1), run(3)
    check("parallel: 3 slots reject, cache and judge exactly as 1 slot", one[:4] == three[:4]
          and sorted(one[1]) == ["PP2", "PP4"] and len(one[2]) == 5, (one[:4], three[:4]))
    check("parallel: 1 slot checks one row at a time, 3 slots side by side", one[4] == 1 and three[4] >= 2,
          (one[4], three[4]))
    check("parallel: one compile at a time, each row past its static checks compiled once", not clash.exists()
          and one[5] == three[5] == 5, (clash.exists(), one[5], three[5]))


def test_prescreen_compile():
    """While the gate is in acceptance, prescreen compiles a queued row on the batch head in the background and never
    rejects; once the batch lands whole (main = head + a spec-only commit) the pick reuses that compile; once main's
    code moves the row compiles again."""
    rows = branches(["PX", "PB", "PR"], file_of=lambda r: "RED.txt" if r == "PR" else f"{r}.txt", ts=60)
    (TMP / "pb_ok.py").write_text("print('ok')" + chr(10), encoding="utf-8")
    h.P.update({"pick_build": f'"{sys.executable}" "{TMP / "pb_ok.py"}"', "pick_env": {}, "pick_err_re": r"^error"})
    h.static_problems = lambda cwd, base="main", plan="spec", landing=(): "red" if (Path(cwd) / "RED.txt").exists() else ""
    h.batch_review = lambda *a, **k: []
    head = rows["PB"]["sha"]  # the batch head: the code main has once the batch (PB) lands whole
    h.save(h.PICK_CACHE, {})
    h.save("rejects.json", {})
    h.save("queue.json", [rows["PX"], rows["PR"]])
    n0, stop = len(h.read_events()), threading.Event()
    t = threading.Thread(target=h.prescreen, args=({head}, stop),
                         kwargs={"poll": 0.05, "accepting": lambda: True, "head": lambda: head})
    t.start()
    for _ in range(400):
        if len([e for e in h.read_events()[n0:] if e["event"] == "pick_prescreen"]) >= 2:
            break
        time.sleep(0.05)
    stop.set()
    t.join(60)
    built = pick_compiles(n0)
    check("prescreen: one background compile, on the batch head, its pid file gone",
          [e.get("background") for e in built] == [True]
          and h.load(h.PICK_CACHE, {}).get(rows["PX"]["sha"], {}).get("built") == head
          and not (h.ST / h.PICK_BACKGROUND_PID).exists(), (built, h.load(h.PICK_CACHE, {})))
    check("prescreen: a red row is never rejected, it stays queued for the pick",
          "PR" not in h.load("rejects.json", {}) and rows["PR"]["sha"] in {q["sha"] for q in h.load("queue.json", [])})
    git("merge", "-q", "--ff-only", head)
    (h.REPO / h.SPEC).parent.mkdir(parents=True, exist_ok=True)
    (h.REPO / h.SPEC).write_text("PB proven" + chr(10), encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "finish_spec: PB proven")
    h.save("queue.json", [rows["PX"]])
    n1 = len(h.read_events())
    check("prescreen: after a whole land the pick reuses the head's compile",
          h.pick_screen([rows["PX"]]) == set() and not pick_compiles(n1), pick_compiles(n1))
    (h.REPO / "moved.txt").write_text("code" + chr(10), encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "main's code moves (a partial land)")
    n2 = len(h.read_events())
    check("prescreen: main's code moved -> the row is compiled again",
          h.pick_screen([rows["PX"]]) == set() and len(pick_compiles(n2)) == 1, pick_compiles(n2))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"{len(FAILS)} failed" if FAILS else "ALL PASS")
    sys.exit(1 if FAILS else 0)
