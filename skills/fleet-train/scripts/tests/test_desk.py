"""Desk acceptance split: units cover every test once; this machine runs the parallel unit and the desk the solo units,
and this machine takes the solo units the desk has not started; a desk fail reaches the verdict and last-gate.txt; a
desk unit with no verdict (error, other tests, other head, late, env fault) runs here and is never a pass; the desk
down sends everything here; this machine busy at gate start (cpu_load() above BUSY_LOAD) keeps no unit while the
desk is up and still runs everything when the desk is down or errs (the cold start; with throughput history a
unit goes here only when this machine is not slower and finishes no later; stale history: cold start); a unit the desk could not build runs there once
more after its incremental clean, then here; state/desk-on turns it on, HARNESS_DESK=0/1 overrides; the desk side writes its result; a unit timing out here is split, never a train error.

  py -3 tests/test_desk.py
"""
from __future__ import annotations

import ast, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="test-desk-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt")})
os.environ.pop("HARNESS_DESK", None)
sys.path.insert(0, str(HERE))
import desk  # noqa: E402
import harness as h  # noqa: E402

FAILS = []
T = [f"tests/acceptance/s{i % 23}.rs::test_s{i % 23}_{i}" for i in range(140)]
SOLO = {T[3], T[40], T[77], T[101]}
PAR = sorted(set(T) - SOLO)
SHA = "abc123def4567890"


def check(name, ok, *info):
    print(("PASS " if ok else "FAIL ") + name, *([] if ok else info))
    if not ok:
        FAILS.append(name)


def fake_h(red_local=(), par_secs=0.0):
    """run_tests recorded (fails = red_local among the unit's tests; a unit with parallel tests takes par_secs); no
    throughput history (the cold start)."""
    calls = []
    (h.ST / desk.RATES).unlink(missing_ok=True)

    def run_tests(tests, timeout=1800, solo=None):
        calls.append((sorted(tests), sorted(solo or [])))
        if set(tests) - set(solo or []):
            time.sleep(par_secs)
        bad = sorted(set(tests) & set(red_local))
        if bad:
            (h.ST / "last-gate.txt").write_text("local log " + " ".join(bad), encoding="utf-8")
        return bad
    h.run_tests = run_tests
    h.git = lambda *a, **k: SimpleNamespace(stdout=SHA + "\n", returncode=0, stderr="")
    h.solo_tests = lambda tests: SOLO & set(tests)
    return calls


class FakeDesk:
    """Per started unit (0 = first): 'ok' and 'red' answer on the first poll, 'error' is no verdict, 'busy' answers
    'ok' after BUSY_POLLS polls."""
    script, why0, BUSY_POLLS, last = {}, "", 0, None

    def __init__(self, h_, gid, sha):
        self.k, self.why, self.ran, self.polls, self.tests = None, FakeDesk.why0, [], 0, set()
        FakeDesk.last = self

    def start(self, k, unit):
        self.k = k
        self.ran.append(k)
        self.tests |= set(unit["tests"])
        return True

    def poll(self, unit):
        k, mode = self.k, FakeDesk.script.get(len(self.ran) - 1, "ok")
        self.polls += 1
        if mode == "busy" and self.polls <= FakeDesk.BUSY_POLLS:
            return None
        self.k = None
        if mode == "error":
            self.why = "boom"
            return k, None
        fails = unit["tests"][:1] if mode == "red" else []
        return k, {"fails": fails, "log": "desk log" if fails else "", "secs": 5, "tests": unit["tests"]}


def ran_once(calls, desk_tests):
    here = [t for c in calls for t in c[0]]
    return sorted(here + sorted(desk_tests)) == sorted(set(T)) and not (set(here) & set(desk_tests))


def test_units_and_pick():
    us = desk.units(T, SOLO, {"s1": 300.0, "s2": 200.0}, {T[3]: 400}, 8)
    flat = [t for u in us for t in u["tests"]]
    check("units cover every test exactly once", sorted(flat) == sorted(set(T)) and len(flat) == len(set(flat)))
    check("unit 0 is the parallel unit", us[0]["tests"] == PAR and us[0]["solo"] == []
          and all(u["solo"] == u["tests"] for u in us[1:]))
    check("solo units longest first", [u["cost"] for u in us[1:]] == sorted((u["cost"] for u in us[1:]), reverse=True)
          and us[1]["tests"] == [T[3]])
    check("no solo tests: one unit", len(desk.units(PAR, set(), {}, {}, 8)) == 1)
    p = desk.place(us, desk.rates({}, True, 0.0))  # cold, equal rates: here the 400 s solo unit, the desk the rest
    check("place cold: both machines, the later finish below one machine's",
          p.here == {1} and max(p.expect) == round(sum(u["cost"] for u in us) - us[1]["cost"])
          and max(p.expect) < sum(u["cost"] for u in us), p)
    check("pick: desk takes its own units in order, then here's unstarted ones",
          desk.pick(us, [0, 2, 1], "desk") == [0] and desk.pick(us, [1], "desk") == [1]
          and desk.pick(us, [], "desk") == [])
    check("pick: here takes its own unit first, then the desk's first unstarted one",
          desk.pick(us, [2, 1, 0], "here") == [1] and desk.pick(us, [3, 2], "here") == [2])
    check("pick: desk out, here takes every unit left", desk.pick(us, [3, 1, 2], "here", desk_up=False) == [1, 2, 3])
    return us


def test_run_desk(us):
    desk.POLL_SECS = 0.01
    real_load, desk.cpu_load = desk.cpu_load, (lambda secs=0: 5.0)  # an idle machine (this one may be loaded)
    lv = real_load(0.2)
    check("cpu_load reads a busy % or None", lv is None or 0.0 <= lv <= 100.0, lv)
    h.save(h.ALONE_TIMES, {T[3]: 400})
    calls = fake_h(par_secs=0.3)
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    dk = FakeDesk.last
    check("solo to desk: green, here only the parallel unit", out == [] and dk.tests == SOLO and calls == [(PAR, [])])
    check("solo to desk: every test ran once", ran_once(calls, dk.tests))
    ev = h.read_events()[-1]
    check("gate_stage acceptance-desk logged", ev.get("stage") == "acceptance-desk" and ev["desk"] == len(us) - 1, ev)

    calls = fake_h(par_secs=0.05)
    FakeDesk.script, FakeDesk.BUSY_POLLS = {0: "busy"}, 30
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    dk = FakeDesk.last
    check("steal: desk ran only its first solo unit", dk.ran == [1] and dk.tests == {T[3]}, dk.ran)
    check("steal: here parallel first, then the other solo units", calls[0] == (PAR, [])
          and all(c[0] == c[1] for c in calls[1:]) and {t for c in calls[1:] for t in c[0]} == SOLO - {T[3]})
    check("steal: every test ran once, green", out == [] and ran_once(calls, dk.tests))
    FakeDesk.script, FakeDesk.BUSY_POLLS = {}, 0

    fake_h(par_secs=0.3)
    FakeDesk.script = {0: "red"}
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    lg = (h.ST / "last-gate.txt").read_text(encoding="utf-8")
    check("desk red is a fail, logged", out == [T[3]] and "desk log" in lg, out)

    calls = fake_h(red_local=T, par_secs=0.3)
    FakeDesk.script = {0: "error"}
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    check("desk error: every test ran here once", sorted(t for c in calls for t in c[0]) == sorted(set(T)))
    check("desk error: local reds count", sorted(out) == sorted(set(T)), len(out))
    FakeDesk.script = {}

    calls = fake_h()
    FakeDesk.why0 = "remote-host unreachable"
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    check("desk down: parallel unit, then every solo unit in one call",
          out == [] and calls == [(PAR, []), (sorted(SOLO), sorted(SOLO))], len(calls))
    fake_h(red_local=[T[40]])
    check("desk down: a red in the merged solo call fails", desk.run_desk(h, T, desk_cls=FakeDesk) == [T[40]])
    h.run_tests = lambda tests, timeout=1800, solo=None: ["build"] if solo else []
    check("desk down: a build failure fails the gate", desk.run_desk(h, T, desk_cls=FakeDesk) == ["build"])
    FakeDesk.why0 = ""

    check("local_ok: unknown or at most BUSY_LOAD keeps units here, above does not",
          desk.local_ok(None) and desk.local_ok(desk.BUSY_LOAD) and not desk.local_ok(desk.BUSY_LOAD + 1))
    check("pick busy: none here while the desk is up; the desk takes the parallel unit first, then solo longest",
          desk.pick(us, [2, 0, 1], "here", here=False) == [] and desk.pick(us, [2, 0, 1], "desk", here=False) == [0]
          and desk.pick(us, [2, 1], "desk", here=False) == [1])
    check("pick busy, desk out: here takes every unit left",
          desk.pick(us, [2, 0, 1], "here", desk_up=False, here=False) == [0, 1, 2])
    desk.cpu_load = lambda secs=0: 95.0
    calls = fake_h(par_secs=0.05)
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    dk, ev = FakeDesk.last, h.read_events()[-1]
    check("busy: nothing here, the desk ran every unit once, the parallel one first",
          out == [] and calls == [] and dk.ran[0] == 0 and sorted(dk.ran) == list(range(len(us)))
          and dk.tests == set(T), out, len(calls), dk.ran)
    check("busy: the gate event carries the load and the decision",
          ev.get("cpu_load") == 95 and ev.get("local_ok") is False and ev.get("desk") == len(us), ev)
    desk.cpu_load = lambda secs=0: 10.0
    calls = fake_h(par_secs=0.3)
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    check("idle: today's split, here only the parallel unit, the desk every solo unit",
          out == [] and calls == [(PAR, [])] and FakeDesk.last.tests == SOLO, out, len(calls))
    desk.cpu_load = lambda secs=0: 51.0  # a regression: a moderately loaded machine, a healthy desk, desk 0 units
    calls = fake_h(par_secs=0.3)
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    ev = h.read_events()[-1]
    check("under BUSY_LOAD (51): today's split, the gate event shows the desk ran every solo unit",
          out == [] and calls == [(PAR, [])] and ev.get("local_ok") is True and ev.get("solo_here") == 0
          and ev.get("desk") == len(us) - 1, ev)
    desk.cpu_load = lambda secs=0: 95.0
    calls = fake_h()
    FakeDesk.why0 = "remote-host unreachable"
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    check("busy, desk down: every unit here in one call, each test once",
          out == [] and calls == [(sorted(set(T)), sorted(SOLO))], len(calls))
    FakeDesk.why0 = ""
    calls = fake_h(red_local=[PAR[0]])
    FakeDesk.script = {0: "error"}
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    check("busy, desk error on its first unit: it and every other unit run here, local reds count",
          out == [PAR[0]] and FakeDesk.last.ran == [0] and ran_once(calls, []), out, FakeDesk.last.ran)
    FakeDesk.script = {}
    desk.cpu_load = lambda secs=0: 5.0


def test_desk_poll():
    d = desk.Desk.__new__(desk.Desk)
    d.h, d.gid, d.sha, d.k, d.t0, d.why, d.env = h, "g", "abc", 0, time.time(), "", set()
    u = {"cost": 10.0, "tests": T[:5], "solo": []}
    real_poll, real_ssh = desk.poll, desk.ssh

    def answer(**kw):
        desk.poll = lambda h_, rid: {"fails": [], "tests": T[:5], "head": "abc", "events": [], "perf_debt": [], **kw}
        d.k, d.t0 = 0, time.time()
        return d.poll(u)
    check("desk result for other tests is no verdict", answer(tests=T[:4])[1] is None and "other tests" in d.why)
    check("desk result on another head is no verdict", answer(head="x")[1] is None and "head" in d.why)
    h.P["env_fail_re"] = "linker not found"
    check("desk toolchain env fault is no verdict", answer(log="error: linker not found")[1] is None and "env" in d.why)
    h.P["env_fail_re"] = ""
    check("desk result is a verdict", answer()[1] is not None and d.why == "")
    desk.poll, desk.ssh = (lambda h_, rid: None), (lambda h_, cmd, timeout=180: SimpleNamespace(stdout="", returncode=0))
    d.k, d.t0 = 0, time.time()
    check("running desk unit: keep waiting", d.poll(u) is None)
    d.k, d.t0 = 0, time.time() - 10_000
    got = d.poll(u)
    check("late desk unit is no verdict", got is not None and got[1] is None and "late" in d.why, d.why)
    desk.poll, desk.ssh = real_poll, real_ssh


def test_switch_and_routing():
    check("default: desk off", not desk.on(h.ST))
    (h.ST / desk.ON).write_text("", encoding="utf-8")
    check("desk-on flag: on", desk.on(h.ST))
    os.environ["HARNESS_DESK"] = "0"
    check("HARNESS_DESK=0 beats the flag", not desk.on(h.ST))
    (h.ST / desk.ON).unlink()
    os.environ["HARNESS_DESK"] = "1"
    check("HARNESS_DESK=1 without the flag", desk.on(h.ST))
    os.environ.pop("HARNESS_DESK")
    calls = fake_h()
    desk.run(h, T[:10])
    check("few tests (probe, bisect): one plain run_tests here", calls == [(sorted(T[:10]), [])])
    h.P["accept"], keep = "node --test {file}", h.P["accept"]
    calls = fake_h()
    desk.run(h, T)
    h.P["accept"] = keep
    check("cmd runner: plain run_tests here", len(calls) == 1)


def test_desk_side_and_kit():
    runs = h.HOME / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "g-1.json").write_text(json.dumps({"rid": "g-1", "sha": SHA, "tests": [T[3]], "solo": [T[3]]}), "utf-8")
    calls = fake_h(red_local=[T[3]])
    res = desk.accept(h, "g-1")
    on_disk = json.loads((runs / "g-1.result.json").read_text(encoding="utf-8"))
    check("desk side: runs its unit with its solo set", calls == [([T[3]], [T[3]])])
    check("desk side: result written with fails, head and log", on_disk == res and res["fails"] == [T[3]]
          and res["head"] == SHA and "local log" in res["log"], res)
    names = {p.name for p in desk.kit_files(h)}
    tree = ast.parse((HERE / "harness.py").read_text(encoding="utf-8"))
    local = {a.name + ".py" for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
             if (HERE / (a.name + ".py")).exists()}
    check("kit holds the harness and every local module it imports", {"harness.py", "desk.py"} | local <= names,
          local - names)
    kit_dir = TMP / "kit-alone"  # the kit loads on its own, as on the desk (a module it lacks crashes every unit)
    kit_dir.mkdir(exist_ok=True)
    for p in desk.kit_files(h):
        shutil.copyfile(p, kit_dir / p.name)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "HARNESS_"))}
    env.update(HARNESS_HOME=str(kit_dir), HARNESS_REPO=str(TMP / "repo"), HARNESS_WT=str(TMP / "wt"))
    r = subprocess.run([sys.executable, "-E", "-s", "desk.py", "--help"], cwd=kit_dir, env=env, capture_output=True,
                       text=True, timeout=120)
    check("kit loads on its own: desk.py runs from a copy of the kit alone", r.returncode == 0,
          (r.stdout + r.stderr)[-400:])
    os.environ.update(HARNESS_REMOTE_ENV="set A=1&& set B=2", HARNESS_CRATE="app&x")
    run_cmd = desk.runner_scripts(h)["run.cmd"]
    check("runner env: quoted sets, kit paths, no local-only vars", f'set "HARNESS_HOME={desk.KIT}"' in run_cmd
          and 'set "HARNESS_CRATE=app&x"' in run_cmd and "HARNESS_REMOTE_" not in run_cmd
          and str(TMP) not in run_cmd and "desk.py accept --rid %1" in run_cmd, run_cmd)
    os.environ.pop("HARNESS_CRATE")
    import remote
    check("remote.py takes the second machine's config from desk.py", remote.HOST is desk.HOST and remote.ROOT is desk.ROOT)


def test_kit_resend():
    sent, ok = [], [True]
    real = desk.ssh, desk.scp
    desk.ssh = lambda h_, cmd, timeout=180: SimpleNamespace(stdout="", stderr="", returncode=1)
    desk.scp = lambda h_, src, dst, timeout=600: sent.append(Path(src).name) or ok[0]
    h.save(desk.KIT_SIG, {})
    ok[0] = False
    check("failed kit scp: an error, no signature saved", desk.kit(h).startswith("kit scp failed")
          and not h.load(desk.KIT_SIG, {}).get("sig"))
    ok[0] = True
    sent.clear()
    first = desk.kit(h), set(sent)
    sent.clear()
    check("kit sent whole, then not again while unchanged", first[0] == "" and {"harness.py", "run.cmd"} <= first[1]
          and desk.kit(h) == "" and sent == [], first)
    check("a unit that does not start clears the signature", desk.start(h, "g-2", {}) != ""
          and not h.load(desk.KIT_SIG, {}).get("sig"))
    sent.clear()
    check("so the next gate resends the kit", desk.kit(h) == "" and "harness.py" in sent)
    desk.ssh, desk.scp = real


LINK_FAIL = ("app.0x1.rcgu.o : error LNK2001: unresolved external symbol _RINvNtCs0_4core3ptr9drop_glue\r\n"
             "D:\\fleet\\target\\release\\deps\\app.exe : fatal error LNK1120: 2 unresolved externals\r\n"
             '{"reason":"build-finished","success":false}\n'
             'error: could not compile `app` (bin "app") due to 1 previous error\n')
RED_ALONE = ("== s1::test_s1_1 (serial): running 1 test\ntest test_s1_1 ... FAILED\n\nfailures:\n\n"
             "failures:\n    test_s1_1\n\ntest result: FAILED. 0 passed; 1 failed\n")
TGT = "D:/fleet/repo.git/.overhaul/target/land-0"
TREE = "D:\\fleet\\land-0"
STALE_RLIB = ('"/OUT:app.exe" "libserde-0123456789abcdef.rlib"\r\n'
              "libapp_render-0123456789abcdef.rlib(app_render-0123456789abcdef.x1.y2.rcgu.o) : error LNK2019: "
              "unresolved external symbol _RINvNtCs0_4core3ptr9drop_glueNtCs1_3libECs8r9_10app_render referenced in "
              "function _RNvNtCs8r9_10app_render7effects4push\\r\\n"
              "D:\\fleet\\target\\release\\deps\\app.exe : fatal error LNK1120: 1 unresolved externals\r\n"
              'error: could not compile `app` (bin "app") due to 1 previous error\n')


def test_desk_unit_env():
    """A desk unit whose test build failed (a stale incremental link: LNK1120) once counted every test of the unit as
    a fail and ejected innocent rows by file blame. It is no verdict: desk_unit_env, the unbuilt targets' incremental
    objects cleaned in the desk's own target dir, one desk re-run under a new rid, then here; a real red counts."""
    ts = sorted(T[3:6])
    u = {"cost": 10.0, "tests": ts, "solo": ts}
    built = {"fails": ts, "tests": ts, "head": SHA, "events": [], "perf_debt": [], "log": LINK_FAIL, "target": TGT,
             "tree": TREE}
    cmds, starts, h.P["accept"] = [], [], "cargo"
    real = desk.poll, desk.ssh, desk.start
    gone = {"app": 120, "app-render": 40}
    meta = json.dumps({"packages": [{"name": n, "targets": [{"name": n.replace("-", "_"), "kind": ["lib"]}]}
                                    for n in gone]})

    def fake_ssh(h_, cmd, timeout=180):  # cargo metadata, then `echo @@pkg & cargo clean -p pkg` per package
        cmds.append(cmd)
        if " metadata " in cmd:
            return SimpleNamespace(stdout=meta, returncode=0)
        return SimpleNamespace(returncode=0, stdout="".join(
            f"@@{p} \r\n     Removed {gone.get(p, 0)} files, 1.0MiB total\r\n" for p in re.findall(r"@@(\S+)", cmd)))
    desk.ssh = fake_ssh
    desk.start = lambda h_, rid, spec: starts.append(rid) and ""

    def at(answers):
        desk.poll = lambda h_, rid: answers.get(rid)
        d = desk.Desk.__new__(desk.Desk)
        d.h, d.gid, d.sha, d.k, d.t0, d.why, d.env = h, "g", SHA, 0, time.time(), "", set()
        return d
    d = at({"g-0": built, "g-0-env": {**built, "fails": [], "log": ""}})
    check("build failure: no fail, one desk re-run under a new rid", d.poll(u) is None and starts == ["g-0-env"], starts)
    check("build failure: the bin's link failed naming no stale object: nothing cleaned, no metadata call",
          cmds == [], cmds)
    built = {**built, "log": "app.cgu0.x1.rcgu.o : error LNK2001: unresolved external symbol _RNvCs0_3app1f\r\n" + LINK_FAIL}
    cmds.clear()
    d = at({"g-0": built, "g-0-env": {**built, "fails": [], "log": ""}})
    d.poll(u)
    check("build failure: the bin's own objects are stale: cargo clean -p its package, release, in the desk's tree",
          len(cmds) == 2 and " metadata --no-deps " in cmds[0] and cmds[1].endswith(
              f'&& cd /d "{TREE}" && (echo @@app & cargo clean -p app --release --target-dir "{TGT}" --offline 2>&1)'),
          cmds)
    ev = [json.loads(l) for l in (h.ST / "events.jsonl").read_text(encoding="utf-8").splitlines()
          if '"desk_unit_env"' in l]
    check("build failure: cleaned only when files were removed, logged by package",
          ev[-1].get("cleaned") is True and ev[-1].get("removed") == {"app": 120}, ev[-1:])
    k, res = d.poll(u)
    check("build failure: the clean re-run's verdict counts", res is not None and res["fails"] == [], d.why)
    d = at({"g-0": built, "g-0-env": built})
    d.poll(u)
    k, res = d.poll(u)
    check("did not build twice: no verdict (the unit runs here)", res is None and desk.ENV_UNIT in d.why, d.why)
    nc = len(cmds)
    k, res = at({"g-0": {**built, "log": RED_ALONE, "fails": [ts[0]]}}).poll(u)
    check("a real desk red counts as before, nothing cleaned", res is not None and res["fails"] == [ts[0]]
          and len(cmds) == nc, d.why)
    check("a job result without tests is never classified", desk.unit_env(h, {"fails": ["x"], "log": LINK_FAIL}) == "")
    stale = {**built, "log": STALE_RLIB}
    why = desk.unit_env(h, stale)
    check("stale dependency rlib: cargo clean -p its package only (lib crate -> package), never the bin's",
          "(echo @@app-render & cargo clean -p app-render --release " in cmds[-1] and " clean -p app " not in cmds[-1]
          and why.endswith("(cargo clean -p app-render)"), (why, cmds[-1]))
    kept = dict(gone)
    gone.clear()
    why = desk.unit_env(h, stale)
    ev = [json.loads(l) for l in (h.ST / "events.jsonl").read_text(encoding="utf-8").splitlines()
          if '"desk_unit_env"' in l]
    check("the old no-op is caught: a clean that exits 0 and removes nothing is never cleaned: true",
          ev[-1].get("cleaned") is False and ev[-1].get("removed") == {} and why.endswith("(nothing cleaned)"), ev[-1:])
    gone.update(kept)
    f = h.cf.parse(STALE_RLIB)
    check("parse: the linker's crates are the archive owner and the symbol's crate, never the argument list",
          f.build_failed and f.linked == ("app_render",) and f.lib_packages == (), f)
    h.P["accept"] = "other"
    check("a non-cargo runner is never classified", desk.unit_env(h, built) == "")
    h.P["accept"] = "cargo"

    class EnvDesk(desk.Desk):  # end to end: every desk run fails to build; only this machine's verdict counts
        def setup(self):
            return ""
    specs = {}
    desk.start = lambda h_, rid, spec: specs.update({rid: spec}) or ""
    desk.poll = lambda h_, rid: {**built, "tests": specs[rid]["tests"], "fails": specs[rid]["tests"]} if rid in specs else None
    calls = fake_h(red_local=[T[40]], par_secs=0.05)
    out = desk.run_desk(h, T, desk_cls=EnvDesk)
    here = sorted(t for c in calls for t in c[0])
    check("e2e: desk build failures never fail; every unit ran here; only this machine's red counts",
          out == [T[40]] and here == sorted(set(T)) and len(specs) == 2, out, sorted(specs))
    desk.poll, desk.ssh, desk.start = real


def test_throughput():
    """Placement for the earliest gate finish: parallel unit 2484 and 7 solo units 1270 cost-seconds, as on the
    source project, where a loaded machine kept the parallel unit 5016 s while the desk ran the solo units in 1114 s,
    and a later gate planned all on the desk (rates slightly worse here) where a split finishes ~24% sooner."""
    us = [{"cost": 2484.0, "tests": ["p"], "solo": []}] + [
        {"cost": c, "tests": [f"s{i}"], "solo": [f"s{i}"]} for i, c in enumerate([327, 215, 190, 180, 150, 120, 88])]
    hist = {}
    for ts, work in ((0, {"here": {"par": [6174, 2484]}, "desk": {"solo": [804, 1270]}}),
                     (1, {"here": {"par": [2209, 2484]}}), (2, {"desk": {"par": [3038, 2484], "solo": [1246, 1270]}}),
                     (3, {"here": {"par": [5016, 2484]}, "desk": {"solo": [1114, 1270]}})):
        hist = desk.learn(hist, work, ts)

    def alone(r, m):  # every unit on machine m
        return sum(u["cost"] * r[m, desk.kind(u)] for u in us)
    r = desk.rates(hist, True, 10)
    p = desk.place(us, r)
    check("loaded here (median 2.0 s per cost-second vs the desk's 1.22): the parallel unit on the desk, here no "
          "later than the desk, the gate sooner than all on the desk",
          0 not in p.here and p.expect[0] <= p.expect[1] and max(p.expect) < alone(r, "desk"), p)
    for r in ({("here", "par"): 2.019, ("here", "solo"): 2.019, ("desk", "par"): 1.719, ("desk", "solo"): 1.048},
              {("here", "par"): 1.719, ("here", "solo"): 1.048, ("desk", "par"): 1.719, ("desk", "solo"): 1.048}):
        p = desk.place(us, r)  # recorded: all on the desk 5601 s, now 4270 s; equal rates: the tie, less here
        check("slightly slower or equal here: both machines, sooner than either alone, the parallel unit on the desk",
              p.here == set(range(1, len(us))) and max(p.expect) < min(alone(r, m) for m in desk.MACHINES), p)
    slow_desk = {"here": {"par": [[0, 0.5]]}, "desk": {"par": [[0, 1.5]], "solo": [[0, 1.5]]}}
    r = desk.rates(slow_desk, True, 10)
    p = desk.place(us, r)
    check("idle here, slow desk: the parallel unit here, both machines, sooner than here alone, every desk unit "
          "stealable", 0 in p.here and len(p.here) < len(us) and p.steal == set(range(len(us))) - p.here
          and not p.back and max(p.expect) < alone(r, "here"), p)
    late = desk.RATE_TTL["here"] + 10
    cold, busy = desk.place(us, desk.rates(hist, True, late)), desk.place(us, desk.rates(hist, False, late))
    check("stale history here: cold start; not busy: the parallel unit alone on the desk, the rest here; busy: "
          "nothing here, nothing to steal", cold.here == set(range(1, len(us)))
          and max(cold.expect) == round(2484 * desk.rates(hist, True, late)["desk", "par"])
          and not busy.here and not busy.steal, (cold, busy))
    check("learn keeps RATE_KEEP samples per machine and kind",
          len(desk.learn({"here": {"par": [[i, 1.0] for i in range(9)]}}, {"here": {"par": [1, 1]}}, 9)["here"]["par"])
          == desk.RATE_KEEP)
    calls = fake_h(par_secs=0.05)
    now = time.time()
    h.save(desk.RATES, {"here": {"par": [[now, 1e3]]}, "desk": {"par": [[now, 1.2]]}})
    FakeDesk.script, FakeDesk.why0 = {}, ""
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    ev = [json.loads(l) for l in (h.ST / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    gs = [e for e in ev if e.get("stage") == "acceptance-desk"][-1]
    hist = h.load(desk.RATES, {})
    check("slow here: nothing here, the desk ran every test once; the event carries the plan; the work is learned",
          out == [] and calls == [] and FakeDesk.last.tests == set(T) and gs.get("plan_here") == []
          and gs.get("rates", {}).get("here/par") == 1e3 and len(hist["desk"]["par"]) == 2, (out, len(calls), gs))
    calls = fake_h()
    h.save(desk.RATES, {"here": {"par": [[now, 1e3]]}, "desk": {"par": [[now, 1.2]]}})
    FakeDesk.why0 = "desk unreachable"
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    check("slow here, desk down: every unit here in one call, each test once",
          out == [] and calls == [(sorted(set(T)), sorted(SOLO))], [len(c[0]) for c in calls])
    FakeDesk.why0 = ""
    calls = fake_h(par_secs=0.3)  # an idle desk takes here's unstarted units, last first, while here runs the parallel
    h.save(desk.RATES, {"here": {"par": [[now, 1.0]], "solo": [[now, 1.0]]},
                        "desk": {"par": [[now, 10.0]], "solo": [[now, 1.0]]}})
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    ev = [json.loads(l) for l in (h.ST / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    gs = [e for e in ev if e.get("stage") == "acceptance-desk"][-1]
    mine, ran = [k for k in gs["plan_here"] if k], FakeDesk.last.ran
    check("desk steals back: here only the parallel unit, the desk its own unit then here's solo units, last first",
          out == [] and gs["plan_here"][:1] == [0] and len(mine) >= 2 and calls == [(sorted(set(T) - SOLO), [])]
          and ran[len(ran) - len(mine):] == sorted(mine, reverse=True) and FakeDesk.last.tests == SOLO,
          (gs["plan_here"], ran, len(calls)))


def test_unit_timeout():
    """A unit that times out here (its test build passed the timeout on a loaded machine) is no verdict, never a train
    error: split, the parts offered to the idle desk first, each test one verdict; desk down: parts one per call; the
    smallest unit once more on the desk, with no desk it raises (no verdict)."""
    whole = {Path(t.split("::")[0]).stem for t in PAR}
    calls = []

    def arm(red=(), down=False, fixed=None):
        calls.clear()
        (h.ST / desk.RATES).unlink(missing_ok=True)
        FakeDesk.script, FakeDesk.why0, FakeDesk.BUSY_POLLS = {}, ("unreachable" if down else ""), 0

        def run_tests(ts, timeout=1800, solo=None):
            par = {Path(t.split("::")[0]).stem for t in set(ts) - set(solo or [])}
            calls.append((sorted(ts), sorted(solo or [])))
            if par == whole or (fixed and par and par <= fixed):
                raise subprocess.TimeoutExpired(["cargo", "test", "--no-run"], timeout)
            return sorted(set(ts) & set(red))
        h.run_tests = run_tests
        h.git = lambda *a, **k: SimpleNamespace(stdout=SHA + "\n", returncode=0, stderr="")
        h.solo_tests = lambda tests: SOLO & set(tests)

    def events(name):
        ev = [json.loads(l) for l in (h.ST / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        return [e for e in ev if e.get("event") == name or e.get("stage") == name]

    def verdict_runs():
        return [t for c in calls if {Path(t.split("::")[0]).stem for t in set(c[0]) - set(c[1])} != whole for t in c[0]]

    old_load, desk.cpu_load = desk.cpu_load, (lambda secs=0: 5.0)  # history keeps the parallel unit here
    arm()
    h.save(desk.RATES, {"here": {"par": [[time.time(), 0.5]]}, "desk": {"par": [[time.time(), 2.0]]}})
    n0 = len(events("unit_timeout"))
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    dk, to, gs = FakeDesk.last, events("unit_timeout")[n0:], events("acceptance-desk")[-1]
    pk = [i for i, k in enumerate(dk.ran) if k >= gs["units"] - 2]
    tail = dk.ran[pk[0]:] if pk else []
    here = verdict_runs()
    check("unit timeout: no raise, green, one unit_timeout event (size, binaries, secs), the parts on the desk first",
          out == [] and len(to) == 1 and to[0]["n"] == len(PAR) and to[0]["binaries"] == len(whole)
          and to[0]["parts"] == 2 and gs["timeouts"] == 1 and pk and tail == sorted(tail, key=lambda k: k < gs["units"] - 2),
          out, to, dk.ran, gs.get("units"))
    check("unit timeout: every test one verdict", sorted(here + sorted(dk.tests)) == sorted(set(T))
          and not set(here) & dk.tests, len(here), len(dk.tests))
    arm(red=[PAR[0]], down=True)
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    parts = [c for c in calls if c[0] and not c[1] and {Path(t.split("::")[0]).stem for t in c[0]} != whole]
    check("unit timeout, desk down: a red inside a part is red; the parts one per call; every test one verdict",
          out == [PAR[0]] and len(parts) == 2 and sorted(verdict_runs()) == sorted(set(T)), out, [len(c[0]) for c in calls])
    now = time.time()
    arm()
    h.save(desk.RATES, {"here": {"par": [[now, 1e3]], "solo": [[now, 1e3]]}, "desk": {"par": [[now, 1.0]], "solo": [[now, 1.0]]}})
    FakeDesk.script = {0: "error"}  # every unit planned on the desk; it errs: here runs every unit in one call
    out = desk.run_desk(h, T, desk_cls=FakeDesk)
    merged = [c for c in calls if c[1] and set(c[0]) - set(c[1])]
    check("desk errs, the merged call here times out: each unit alone, the parallel one in halves, green, one verdict each",
          out == [] and merged and all({Path(t.split("::")[0]).stem for t in set(c[0]) - set(c[1])} == whole for c in merged)
          and sorted(verdict_runs()) == sorted(set(T)), out, [(len(c[0]), len(c[1])) for c in calls])
    FakeDesk.script = {}
    one = {Path(PAR[0].split("::")[0]).stem}
    small = [t for t in PAR if Path(t.split("::")[0]).stem in one] + sorted(SOLO)
    arm(down=True, fixed=one)
    try:
        desk.run_desk(h, small, desk_cls=FakeDesk)
        raised = ""
    except TimeoutError as e:
        raised = str(e)
    check("one binary timing out, no desk: no verdict (raises), never a pass or a red",
          "smallest unit" in raised and events("unit_timeout")[-1]["parts"] == 0, raised)
    arm(fixed=one)
    out = desk.run_desk(h, small, desk_cls=FakeDesk)
    check("one binary timing out, desk up: once more on the desk, green",
          out == [] and set(small) - SOLO <= FakeDesk.last.tests, out)
    desk.cpu_load = old_load


def main():
    us = test_units_and_pick()
    test_run_desk(us)
    test_throughput()
    test_unit_timeout()
    test_desk_poll()
    test_desk_unit_env()
    test_switch_and_routing()
    test_desk_side_and_kit()
    test_kit_resend()
    print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
