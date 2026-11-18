"""Desk acceptance split: units cover every test once; this machine runs the parallel unit and the desk the solo units,
and this machine takes the solo units the desk has not started; a desk fail reaches the verdict and last-gate.txt; a
desk unit with no verdict (error, other tests, other head, late, env fault) runs here and is never a pass; the desk
down sends everything here; this machine busy at gate start (cpu_load() above BUSY_LOAD) keeps no unit while the
desk is up and still runs everything when the desk is down or errs; state/desk-on turns it on, HARNESS_DESK=0/1 overrides; the desk side writes its result.

  py -3 tests/test_desk.py
"""
from __future__ import annotations

import ast, json, os, shutil, subprocess, sys, tempfile, time
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
    """run_tests recorded (fails = red_local among the unit's tests; a unit with parallel tests takes par_secs)."""
    calls = []

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
    check("pick: desk takes one solo unit, never the parallel one",
          desk.pick(us, [0, 2, 1], "desk") == [1] and desk.pick(us, [0], "desk") == [])
    check("pick: here takes parallel first, then the longest unstarted solo",
          desk.pick(us, [2, 1, 0], "here") == [0] and desk.pick(us, [3, 2], "here") == [2])
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
    d.h, d.gid, d.sha, d.k, d.t0, d.why = h, "g", "abc", 0, time.time(), ""
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


def main():
    us = test_units_and_pick()
    test_run_desk(us)
    test_desk_poll()
    test_switch_and_routing()
    test_desk_side_and_kit()
    test_kit_resend()
    print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
