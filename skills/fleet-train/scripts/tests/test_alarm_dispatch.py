"""Tests of the alarm dispatcher (alarm_dispatch.py + alarm_handlers.py) and its wiring in fleet_beat.py.
Temp state, notes, targets and git repos; the claude launcher is a fake, so nothing live is touched.

  py -3 tests/test_alarm_dispatch.py [fleet_beat file]
"""
from __future__ import annotations

import importlib.util, json, os, re, shutil, subprocess, sys, tempfile, time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

HOME = Path(__file__).resolve().parent.parent
BEAT = Path(sys.argv[1]) if len(sys.argv) > 1 else HOME / "fleet_beat.py"
FAILS: list[str] = []
sys.path.insert(0, str(HOME))
import alarm_dispatch as D  # noqa: E402
import alarm_handlers as H  # noqa: E402

T0 = 1_800_000_000.0


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))
    if not ok:
        FAILS.append(name)


def fake_h(root: Path) -> SimpleNamespace:
    st = root / "state"
    st.mkdir(parents=True, exist_ok=True)
    (root / "notes" / "briefs").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "handbacks").mkdir(parents=True, exist_ok=True)
    h = SimpleNamespace(ST=st, HOME=root, REPO=root / "repo", TARGETS=root / "target", events=[], clock=T0)

    def load(name, default):
        p = st / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

    def save(name, obj):
        (st / name).write_text(json.dumps(obj), encoding="utf-8")

    @contextmanager
    def lock(name, wait=600):
        yield

    h.load, h.save, h.lock = load, save, lock
    h.event = lambda kind, **kw: h.events.append({"event": kind, **kw})
    h.now = lambda: h.clock
    return h


class FakeLauncher:
    """pid -> start time of the processes that run; a reused pid gets another start time."""

    def __init__(self):
        self.started, self.killed, self.live, self.pid = [], [], {}, 100

    def start(self, brief, cwd, out, model, calls):
        self.pid += 1
        self.started.append((brief, model))
        self.live[self.pid] = f"start-{self.pid}"
        return self.pid, f"start-{self.pid}"

    def alive(self, pid, start):  # drivelock.live: an empty start matches any process on that pid
        return pid in self.live and (not start or self.live[pid] == start)

    def kill(self, pid):
        self.killed.append(pid)
        self.live.pop(pid, None)

    def result(self, out):
        return "tried X"


def dispatcher(h, table, launcher=None, busy=None):
    return D.Dispatcher(h, table, H.alarm_id, launcher or FakeLauncher(), briefs=HOME / "alarm_briefs", busy=busy)


def acts(h, action):
    return [e for e in h.events if e.get("action") == action]


# ---------------------------------------------------------------- the table covers every alarm
def test_table_covers_every_alarm():
    src = BEAT.read_text(encoding="utf-8")
    ids = set(re.findall(r'alarms\.append\(\s*f?"([A-Z][A-Z0-9-]+)[ ":]', src))
    ids |= {H.alarm_id(t) for t in ("BREVITY STATE.md words 900 > 800", "BREVITY copies 2 > 0 (a.bak)",
                                    "BREVITY notes/ live-words 70000 > 60000")}
    ids |= set(re.findall(r'\[f?"([A-Z][A-Z0-9-]+) ', (HOME / "beat_alarms.py").read_text(encoding="utf-8")))
    ids.discard("BREVITY-COPIES")  # this brevity_check has no copy rule
    check("fleet_beat raises 20+ alarm ids", len(ids) >= 20, sorted(ids))
    missing = sorted(i for i in ids if i not in H.TABLE)
    check("every fleet_beat alarm id has a handler", not missing, missing)
    stray = sorted(set(H.TABLE) - ids)
    check("no handler for an alarm fleet_beat never raises", not stray, stray)
    for name, hd in H.TABLE.items():
        if isinstance(hd, D.Agent):
            p = HOME / "alarm_briefs" / f"{hd.template}.md"
            check(f"{name}: template {hd.template} exists and carries the facts", p.exists() and "{{facts}}" in p.read_text())


def test_render_brief():
    with tempfile.TemporaryDirectory() as td:
        h, ln = fake_h(Path(td)), FakeLauncher()
        dispatcher(h, H.TABLE, ln).run(["REPEAT-TEST tests/acceptance/r1_a.rs::test_a failed 2+ batches"])
        brief = ln.started[0][0].read_text(encoding="utf-8")
        check("brief starts with RULE ZERO", brief.startswith("> RULE ZERO"))
        check("brief carries the alarm and the slot", "r1_a.rs::test_a" in brief and "--slot 300" in brief)
        check("no placeholder left", "{{" not in brief, re.findall(r"\{\{\w+\}\}", brief))
        check("brief caps tool calls", "at most 120 tool calls" in brief or "tool calls" in brief)


# ---------------------------------------------------------------- engine
def test_auto():
    with tempfile.TemporaryDirectory() as td:
        h = fake_h(Path(td))
        calls = []
        ok = {"X": D.Auto(lambda ctx, a: calls.append(a.key) or "did it")}
        d = dispatcher(h, ok)
        d.run(["X 1 thing"])
        check("auto fix runs and is an event", calls == ["X"] and acts(h, "auto")[0]["ok"], h.events)
        h.clock += 60
        d.run(["X 1 thing"])
        check("auto fix waits AUTO_GAP before running again", calls == ["X"])
        check("status: handled-by auto fix since <t>", D.status(h.load("dispatch.json", {})["X"], ok["X"], h.clock)
              == f"handled-by auto fix since {D.hhmm(T0)}: did it")

        def bad(ctx, a):
            raise D.Failed("cannot")
        fail = {"Y": D.Auto(bad)}
        d = dispatcher(h, fail)
        for i in range(3):
            h.clock += D.AUTO_GAP + 1
            d.run(["Y broken"])
        e = h.load("dispatch.json", {})["Y"]
        check("auto failing twice is ESCALATE", e.get("escalated", "").startswith("auto fix failed 2"), e)

        slow = {"Z": D.Auto(lambda ctx, a: "tried", patience=3600)}
        d = dispatcher(h, slow)
        for i in range(8):
            d.run(["Z still"])
            h.clock += 10 * 60
        check("auto fix past its patience is ESCALATE", "still firing" in h.load("dispatch.json", {})["Z"].get("escalated", ""))


def test_auto_time_box():
    with tempfile.TemporaryDirectory() as td:
        h = fake_h(Path(td))
        d = dispatcher(h, {"X": D.Auto(lambda ctx, a: f"waited {ctx.left()}", budget_s=-1)})
        d.run(["X 1"])
        r = h.load("dispatch.json", {})["X"]["auto"][-1]
        check("a fix past its time box fails", not r["ok"] and "time box" in r["did"], r)


def test_owned_and_manager():
    with tempfile.TemporaryDirectory() as td:
        h = fake_h(Path(td))
        t = {"O": D.Owned("supervisor step", patience=1800), "M": D.Manager("fire the trigger")}
        d = dispatcher(h, t)
        d.run(["O x", "M y"])
        st = h.load("dispatch.json", {})
        check("manager alarm is ESCALATE at once", st["M"]["escalated"].startswith("manager:"))
        check("owned alarm waits for its owner", not st["O"].get("escalated")
              and D.status(st["O"], t["O"], h.clock) == f"handled-by supervisor step since {D.hhmm(T0)}")
        h.clock += 1801
        d.run(["O x", "M y"])
        check("owned alarm past patience is ESCALATE", "has not cleared it" in h.load("dispatch.json", {})["O"]["escalated"])
        check("unknown alarm id is ESCALATE", D.status(None, None, h.clock).startswith("ESCALATE"))


def test_agent_lifecycle():
    with tempfile.TemporaryDirectory() as td:
        h, ln = fake_h(Path(td)), FakeLauncher()
        t = {"A": D.Agent("train-red", cap_min=30, cooldown_min=20)}
        d = dispatcher(h, t, ln)
        d.run(["A stuck 5"])
        at = h.load("dispatch.json", {})["A"]["attempts"][0]
        check("lock records pid, process start time and deadline",
              (at["pid"], at["start"], at["deadline"]) == (101, "start-101", T0 + 30 * 60), at)
        h.clock += 60
        d.run(["A stuck 6"])
        check("one agent per alarm while its lock is live", len(ln.started) == 1 and not acts(h, "end"))
        check("status: handled-by <agent> since <t>",
              D.status(h.load("dispatch.json", {})["A"], t["A"], h.clock) == f"handled-by a-1 since {D.hhmm(T0)}")
        h.clock += 30 * 60
        d.run(["A stuck 7"])
        e = h.load("dispatch.json", {})["A"]
        check("past its deadline the process tree is killed and the lock released", ln.killed == [101]
              and e["attempts"][0].get("end") and len(ln.started) == 1)
        check("a killed attempt is a failed attempt", e["attempts"][0]["outcome"] == "failed")
        check("status: cooldown until <t>", D.status(e, t["A"], h.clock).startswith(f"cooldown until {D.hhmm(h.clock + 20 * 60)}"),
              D.status(e, t["A"], h.clock))
        h.clock += 10 * 60
        d.run(["A stuck 8"])
        check("no new agent during the cooldown", len(ln.started) == 1)
        h.clock += 11 * 60
        d.run(["A stuck 9"])
        check("second attempt after the cooldown", len(ln.started) == 2)
        check("second brief says the first failed", "did not clear it" in ln.started[1][0].read_text(encoding="utf-8"))
        ln.live[102] = "start-other"  # the agent exited and its pid now belongs to another process
        h.clock += 60
        d.run(["A stuck 10"])
        e = h.load("dispatch.json", {})["A"]
        check("a reused pid is not the agent: lock released, no kill", e["attempts"][1].get("end") and ln.killed == [101])
        check("an agent that exits while its alarm still fires failed (the check judges, not the agent)",
              e["attempts"][1]["outcome"] == "failed" and e.get("escalated", "").startswith("2 failed attempts"), e)
        h.clock += 60 * 60
        d.run(["A stuck 11"])
        check("ESCALATE: never a third agent", len(ln.started) == 2
              and D.status(h.load("dispatch.json", {})["A"], t["A"], h.clock).startswith("ESCALATE"))
        d.run([])
        check("an ESCALATE item ends when its alarm clears", "A" not in h.load("dispatch.json", {})
              and acts(h, "resolved")[-1]["escalated"])


def test_exit_judged_by_the_check():
    with tempfile.TemporaryDirectory() as td:
        h, ln = fake_h(Path(td)), FakeLauncher()
        d = dispatcher(h, {"A": D.Agent("train-red")}, ln)
        d.run(["A stuck"])
        ln.live.clear()
        h.clock += 60
        d.run([])
        e = h.load("dispatch.json", {})["A"]
        check("an agent whose alarm cleared succeeded", e["attempts"][0]["outcome"] == "cleared" and not failed_n(e), e)


def failed_n(e):
    return len(D.failed(e))


def test_agent_guards():
    with tempfile.TemporaryDirectory() as td:
        h, ln = fake_h(Path(td)), FakeLauncher()
        t = {"A": D.Agent("train-red"), "B": D.Agent("main-red", slot=True), "C": D.Agent("fix-test", slot=True)}
        d = dispatcher(h, t, ln)
        d.run(["A 1", "B 1", "C 1"])
        st = h.load("dispatch.json", {})
        check("at most MAX_AGENTS at once", len(ln.started) == D.MAX_AGENTS, len(ln.started))
        check("the rest wait with a reason", "waiting" in st["C"].get("note", ""), st["C"])
        slots = [x["slot"] for e in st.values() for x in e["attempts"] if x["slot"]]
        check("slot agents get distinct dispatch slots", slots == [300], slots)

        h2, ln2 = fake_h(Path(td) / "b"), FakeLauncher()
        t2 = {"R": D.Agent("fix-test", slot=True, per_subject=True), "S": D.Agent("train-red", covered_by=("R",))}
        d2 = dispatcher(h2, t2, ln2)
        d2.run(["S nothing landed", "R tests/acceptance/r9_x.rs::test_x red"])
        st2 = h2.load("dispatch.json", {})
        check("per-subject lock key", "R:tests/acceptance/r9_x.rs::test_x" in st2, list(st2))
        check("an alarm whose cause is handled gets no agent of its own",
              len(ln2.started) == 1 and st2["S"].get("note", "").startswith("handled-by R:"), st2["S"])

        h3, ln3 = fake_h(Path(td) / "c"), FakeLauncher()
        d3 = dispatcher(h3, {"A": D.Agent("train-red")}, ln3, busy=lambda h, a, hd: "fix-r1-1")
        d3.run(["A 1"])
        check("no agent on an alarm another lane works", not ln3.started
              and h3.load("dispatch.json", {})["A"]["note"] == "handled-by lane fix-r1-1")


def test_progress_and_resolve():
    with tempfile.TemporaryDirectory() as td:
        h, ln = fake_h(Path(td)), FakeLauncher()
        t = {"P": D.Agent("train-red")}
        d = dispatcher(h, t, ln)
        d.run(["P rows ['a', 'b']"])
        ln.live.clear()
        h.clock += 11 * 60
        d.run(["P rows ['b']"])
        e = h.load("dispatch.json", {})["P"]
        check("a changed alarm is progress: the next agent starts at once", len(ln.started) == 2
              and e["attempts"][0]["outcome"] == "progress", e)
        ln.live.clear()
        h.clock += 60
        d.run([])
        check("cleared alarm keeps its entry for a while (flapping)", "P" in h.load("dispatch.json", {}))
        h.clock += D.FORGET + 1
        d.run([])
        check("cleared alarm is resolved and forgotten", "P" not in h.load("dispatch.json", {})
              and len(acts(h, "resolved")) == 1)


def test_annotate():
    with tempfile.TemporaryDirectory() as td:
        h, ln = fake_h(Path(td)), FakeLauncher()
        dispatcher(h, H.TABLE, ln).run(["MAIN-RED 2 unit tests red on main", "TRAIN-PID no pid file"])
        out = dict(D.annotate(h, ["MAIN-RED 2 unit tests red on main", "TRAIN-PID no pid file", "NEW-THING x"],
                              H.TABLE, H.alarm_id, h.clock + 60))
        check("annotate: handled-by the agent", out["MAIN-RED 2 unit tests red on main"].startswith("handled-by main-red-1 since"), out)
        check("annotate: manager items are ESCALATE", out["TRAIN-PID no pid file"].startswith("ESCALATE: manager"))
        check("annotate: an alarm without a handler is ESCALATE", out["NEW-THING x"] == "ESCALATE: no handler")


# ---------------------------------------------------------------- auto handlers
def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, check=True)


def test_free_disk():
    with tempfile.TemporaryDirectory() as td:
        h = fake_h(Path(td))
        old = T0 - 5 * 3600
        for name, t in (("slot-5", old), ("slot-6", old), ("slot-7", T0), ("qc-other", old), ("land-0", old)):
            d = h.TARGETS / name / "debug" / "deps"
            d.mkdir(parents=True)
            (d / "f").write_text("x")
            for p in (d, d.parent, d.parent.parent):
                os.utime(p, (t, t))
        h.save("claims.json", {"6": {"row": "R1", "ts": T0}})
        (h.TARGETS / "land-0" / "debug" / "incremental").mkdir()
        ctx = D.Ctx(h, T0)
        real = shutil.disk_usage
        H.shutil.disk_usage = lambda p: SimpleNamespace(free=200e9, total=1e12, used=0)
        try:
            did = H.free_disk(ctx, None)
        finally:
            H.shutil.disk_usage = real
        left = sorted(p.name for p in h.TARGETS.iterdir())
        check("free_disk deletes finished slot targets and qc-other only", left == ["land-0", "slot-6", "slot-7"], (left, did))
        check("free_disk keeps the train cache above the emergency line", (h.TARGETS / "land-0/debug/incremental").exists())
        H.shutil.disk_usage = lambda p: SimpleNamespace(free=20e9, total=1e12, used=0)
        try:
            H.free_disk(ctx, None)
        finally:
            H.shutil.disk_usage = real
        check("free_disk drops land-0 debug incremental under 50 GB", not (h.TARGETS / "land-0/debug/incremental").exists())
        big = h.TARGETS / "slot-9" / "debug" / "deps"
        big.mkdir(parents=True)
        for i in range(3):
            (big / f"f{i}").write_text("x")
        for q in (big, big.parent, big.parent.parent):
            os.utime(q, (T0 - 5 * 3600,) * 2)
        did = H.free_disk(D.Ctx(h, T0, deadline=time.time() - 1), None)
        check("free_disk stops at its time box", "partly" in did and (h.TARGETS / "slot-9").exists(), did)
        H.free_disk(D.Ctx(h, T0), None)
        check("the next run finishes the stopped delete", not (h.TARGETS / "slot-9").exists())
        try:
            H.free_disk(D.Ctx(h, T0), None)
            check("free_disk with nothing to delete fails", False)
        except D.Failed:
            check("free_disk with nothing to delete fails", True)






def test_cli():
    with tempfile.TemporaryDirectory() as td:
        st = Path(td) / "state"
        st.mkdir()
        (st / "health.json").write_text(json.dumps({"ts": T0, "detail": ["TRAIN-PID no pid file", "MAIN-RED 2 red"]}))
        env = {**os.environ, "HARNESS_HOME": td, "HARNESS_REPO": td, "HARNESS_WT": td + "/wt"}
        r = subprocess.run([sys.executable, str(HOME / "alarm_dispatch.py"), "--no-beat", "--dry"], cwd=str(HOME), env=env,
                           capture_output=True, text=True, timeout=300)
        check("the CLI dispatches with the table's own classes", "escalate TRAIN-PID" in r.stdout
              and "launch MAIN-RED" in r.stdout, (r.stdout + r.stderr)[-400:])
        check("a dry CLI run writes no state", not (st / "dispatch.json").exists())


# ---------------------------------------------------------------- supervisor wiring


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"{len(FAILS)} failed" if FAILS else "ALL PASS")
    sys.exit(1 if FAILS else 0)
