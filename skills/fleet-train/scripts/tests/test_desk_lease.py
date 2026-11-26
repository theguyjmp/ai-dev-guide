"""desk_lease: the higher priority always wins (holds, wants, probe hits), fail closed, yield kills the job's tree.

Usage: py -3 tests/test_desk_lease.py"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import desk_lease as dl

FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))
    if not ok:
        FAILS.append(name)


PROJECTS = {"projects": {
    "high": {"priority": 0, "probe": {"match": [r"(?i)\\high\\"], "ignore": [r"--loop", r"serve"],
                                      "children_of": [r"worker\.py serve", r"verify --loop"]}},
    "low": {"priority": 10},
    "peer": {"priority": 10}}}


class World:
    """A fake process table, liveness and clock."""

    def __init__(self):
        self.rows = [(1, 0, "init"), (2, 1, "shell")]
        self.dead: set = set()
        self.t = 1000.0

    def desk(self, home: Path) -> dl.Desk:
        return dl.Desk(home, table=lambda: list(self.rows), live=lambda pid, start: pid not in self.dead,
                       now=lambda: self.t)


def fresh() -> tuple:
    d = Path(tempfile.mkdtemp())
    (d / "projects.json").write_text(json.dumps(PROJECTS), encoding="utf-8")
    w = World()
    return d, w, w.desk(d)


d, w, desk = fresh()
check("a free desk: low may run", desk.may("low") == [], desk.may("low"))
check("low acquires", desk.acquire("low", "j1", 501) == [])
check("a low hold never blocks high", desk.may("high") == [], desk.may("high"))
check("first come between peers", any("low holds" in x for x in desk.may("peer")), desk.may("peer"))
check("high acquires over a low hold", desk.acquire("high", "g1", 601) == [])
check("a high hold blocks low", any("high holds" in x for x in desk.may("low")), desk.may("low"))
w.dead.add(601)
check("a dead holder's lease is gone", desk.may("low") == [], desk.may("low"))
desk.release("low", "j1")
check("release removes the hold", desk.status()["holds"] == [], desk.status())

d, w, desk = fresh()
desk.want("high", 600, "gate queued")
check("a want blocks low", any("wants the desk: gate queued" in x for x in desk.may("low")), desk.may("low"))
w.t += 601
check("an expired want blocks nothing", desk.may("low") == [], desk.may("low"))
desk.want("high", 600, "again")
desk.unwant("high")
check("unwant clears it", desk.may("low") == [])

d, w, desk = fresh()
w.rows += [(10, 1, r"py -3 D:\high\harness\integ.py verify --loop"), (11, 1, r"py -3 D:\high\worker.py serve")]
check("idle loops and servers do not count", desk.may("low") == [], desk.may("low"))
w.rows += [(12, 11, "cargo test --workspace"), (13, 12, r"\??\C:\WINDOWS\system32\conhost.exe 0xffffffff")]
check("a child of the server counts", any("pid 12" in x for x in desk.may("low")), desk.may("low"))
w.rows = w.rows[:4] + [(14, 10, r"cmd /c C:\x\conhost.exe")]
check("a console host under a loop is no work", desk.may("low") == [], desk.may("low"))
w.rows += [(15, 2, r"py -3 -u C:\tmp\lane.py --repo D:\high\hub gate-remote")]
check("a matching command line counts", any("pid 15" in x for x in desk.may("low")), desk.may("low"))
check("the probe never blocks the high project itself", desk.may("high") == [], desk.may("high"))

d, w, desk = fresh()
w.rows = []
check("an unreadable process table is not now", any("fail closed" in x for x in desk.may("low")), desk.may("low"))
check("unknown project refused", dl.main(["--home", str(d), "may", "--project", "nope"]) == dl.NOT_NOW)
(d / "projects.json").write_text(json.dumps({"projects": {"x": {"priority": 0, "probe": {"match": ["("]}}}}))
check("a bad probe pattern fails closed", dl.main(["--home", str(d), "may", "--project", "x"]) == dl.NOT_NOW)


class Child:
    """A job that never ends by itself; kill_pid ends it."""

    def __init__(self):
        self.pid, self.rc = 4242, None

    def wait(self, timeout=None):
        if self.rc is None:
            if timeout is None:
                raise AssertionError("waited forever")
            raise subprocess.TimeoutExpired("job", timeout)
        return self.rc

    def poll(self):
        return self.rc


d, w, desk = fresh()
child, killed, ticks = Child(), [], []
dl.drivelock.kill_pid = lambda pid, tree=True: killed.append(pid) or setattr(child, "rc", 1) or True
orig_may = desk.may


def may_then_want(name):
    ticks.append(name)
    if len(ticks) == 2:
        desk.want("high", 600, "verifier gate")
    return orig_may(name)


desk.may = may_then_want
res = d / "runs" / "r1.result.json"
rc = dl.run(desk, "low", "r1", ["job"], tick=0.01, result=str(res), spawn=lambda c: child, say=lambda s: None)
check("a want mid-run yields (exit 75)", rc == dl.YIELD, rc)
check("the job's tree is killed by pid", killed[:1] == [4242], killed)
check("the result says why", "yielded: high wants" in json.loads(res.read_text())["error"], res.read_text())
check("the lease is released after a yield", desk.status()["holds"] == [], desk.status())
desk.may = orig_may
rc = dl.run(desk, "low", "r2", ["job"], tick=0.01, result=str(d / "r2.json"), spawn=lambda c: Child(),
            say=lambda s: None)
check("deferred while high wants: exit 3, never started", rc == dl.NOT_NOW, rc)
check("deferred writes its result", "desk lease: high wants" in (d / "r2.json").read_text())
desk.unwant("high")


class Done(Child):
    def __init__(self):
        super().__init__()
        self.rc = 0


rc = dl.run(desk, "low", "r3", ["job"], tick=0.01, spawn=lambda c: Done(), say=lambda s: None)
check("a job that finishes returns its own code", rc == 0, rc)


# desk.py wiring: a shared desk runs every accept under the lease and asks it before shipping a bundle
os.environ.setdefault("HARNESS_HOME", tempfile.mkdtemp())
import desk  # noqa: E402

desk.LEASE, desk.PROJECT, desk.KIT = r"D:\lease", "low", r"D:\kit"
line = desk.leased("py -3 -u desk.py accept --rid %1")
check("accept runs under desk_lease run", line.startswith(r"py -3 D:\lease\desk_lease.py --home D:\lease run --project low"
                                                          " --job %1") and line.endswith("-- py -3 -u desk.py accept --rid %1"), line)
check("its result file is the one the caller polls", r"--result D:\kit\runs\%1.result.json" in line, line)
asked = []
desk.ssh = lambda h, cmd, timeout=180: asked.append(cmd) or subprocess.CompletedProcess(cmd, 3, "high wants the desk\n", "")
check("a refusal is named", desk.lease_refusal(None) == "desk lease: high wants the desk", desk.lease_refusal(None))
check("asked with may for this project", "may --project low" in asked[0], asked)
desk.ssh = lambda h, cmd, timeout=180: subprocess.CompletedProcess(cmd, 0, "OK\n", "")
check("a free desk is no refusal", desk.lease_refusal(None) == "")
desk.LEASE = ""
check("no lease configured: the command unchanged", desk.leased("x") == "x" and desk.lease_refusal(None) == "")

print("ALL PASS" if not FAILS else f"FAILED {FAILS}")
sys.exit(1 if FAILS else 0)
