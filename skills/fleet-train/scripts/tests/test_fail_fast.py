"""Tests of the pick check (harness.pick_screen) and the machine-wide cargo cap (cargo_slots.py + alarm_dispatch).
Temp state, worktrees and a fixture git repo; the static checks, reviewer and launcher are fakes.

  py -3 tests/test_fail_fast.py
"""
from __future__ import annotations

import json, os, subprocess, sys, tempfile, threading
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
    for rel, text in {"spec/FUNCTIONS.md": head + fn_row("S1") + fn_row("S2"), "S1.rs": body("todo!()"),
                      "S2.rs": body("todo!()")}.items():
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"{len(FAILS)} failed" if FAILS else "ALL PASS")
    sys.exit(1 if FAILS else 0)
