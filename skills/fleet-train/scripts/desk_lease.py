"""The desk lease: one machine shared by several projects, the higher priority always first (CYCLE.md "Shared desk").

    py -3 desk_lease.py --home <dir> status
    py -3 desk_lease.py --home <dir> may --project P                  exit 0 may start, 3 not now (the reason printed)
    py -3 desk_lease.py --home <dir> want --project P [--ttl S] [--why W]   P needs the desk soon: lower ones yield
    py -3 desk_lease.py --home <dir> unwant --project P
    py -3 desk_lease.py --home <dir> acquire --project P --job J --pid N    exit 0 held, 3 not now
    py -3 desk_lease.py --home <dir> check --project P --job J        exit 0 go on, 75 yield now
    py -3 desk_lease.py --home <dir> release --project P --job J
    py -3 desk_lease.py --home <dir> run --project P --job J [--tick S] [--result F] -- CMD ...
                                                exit 3 deferred (never started), 75 yielded, else CMD's own exit code

Config <home>/projects.json, rules and probe: CYCLE.md "Shared desk". Fail closed: any error is "not now".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, Path(os.environ.get("HARNESS_QS", _HERE)), _HERE.parent.parent / "overhaul-build" / "scripts",
           Path.home() / ".claude/skills/overhaul-build/scripts"):  # an install copies drivelock.py beside this file
    if (_p / "drivelock.py").exists():
        sys.path.insert(0, str(_p))  # first, even when already on the path: never another copy's drivelock
        break
import drivelock

OK, NOT_NOW, YIELD = 0, 3, 75
TICK = 15.0
LOCK_WAIT = 30.0
NO_WINDOW = 0x08000000
CONSOLE_HOST = re.compile(r"(?i)\\(conhost|openconsole)\.exe")  # a console's host process is never work


@dataclass(frozen=True)
class Probe:
    match: tuple = ()
    ignore: tuple = ()
    children_of: tuple = ()


@dataclass(frozen=True)
class Project:
    name: str
    priority: int
    probe: Probe = Probe()


@dataclass(frozen=True)
class Hold:
    project: str
    job: str
    pid: int
    start: str
    since: float


@dataclass(frozen=True)
class Want:
    project: str
    why: str
    until: float
    since: float


@dataclass
class State:
    holds: list = field(default_factory=list)
    wants: list = field(default_factory=list)

    @classmethod
    def read(cls, p: Path) -> State:
        if not p.exists():
            return cls()
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls([Hold(**h) for h in d.get("holds", [])], [Want(**w) for w in d.get("wants", [])])

    def write(self, p: Path) -> None:
        tmp = p.with_name(p.name + f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps({"holds": [asdict(h) for h in self.holds], "wants": [asdict(w) for w in self.wants]},
                                  indent=1), encoding="utf-8")
        os.replace(tmp, p)


def read_projects(home: Path) -> dict:
    d = json.loads((home / "projects.json").read_text(encoding="utf-8"))
    out = {}
    for name, v in d["projects"].items():
        pr = v.get("probe") or {}
        for rx in [*pr.get("match", ()), *pr.get("ignore", ()), *pr.get("children_of", ())]:
            try:
                re.compile(rx)
            except re.error as e:
                raise ValueError(f"{name} probe {rx!r}: {e}") from None
        out[name] = Project(name, int(v["priority"]), Probe(tuple(pr.get("match", ())), tuple(pr.get("ignore", ())),
                                                            tuple(pr.get("children_of", ()))))
    return out


class Desk:
    """The lease over one home dir; table, liveness and clock injectable."""

    def __init__(self, home: Path, table: Callable[[], list] = drivelock.process_table,
                 live: Callable[[int, str], bool] = drivelock.live, now: Callable[[], float] = time.time):
        self.home, self.table, self.live, self.now = Path(home), table, live, now
        self.projects = read_projects(self.home)

    def _locked(self, change: Callable[[State], object]):
        path, t0 = self.home / "leases.lock", time.time()
        while True:
            try:
                drivelock.take(path, "desk_lease")
                break
            except drivelock.Held:
                if time.time() - t0 > LOCK_WAIT:
                    raise
                time.sleep(0.2)
        try:
            st = State.read(self.home / "leases.json")
            st.holds = [h for h in st.holds if self.live(h.pid, h.start)]
            st.wants = [w for w in st.wants if w.until > self.now()]
            out = change(st)
            st.write(self.home / "leases.json")
            return out
        finally:
            drivelock.drop(path)

    def project(self, name: str) -> Project:
        if name not in self.projects:
            raise KeyError(f"{name} is not in {self.home / 'projects.json'}")
        return self.projects[name]

    def probe_hits(self, q: Project, rows: list, mine: set) -> list:
        """(pid, command line) showing q's work, never the caller's own tree."""
        if not (q.probe.match or q.probe.children_of):
            return []
        ign = [re.compile(x) for x in q.probe.ignore] + [CONSOLE_HOST]
        cmds = {pid: cmd for pid, _, cmd in rows}
        idle = {pid for pid, cmd in cmds.items() if any(i.search(cmd) for i in ign)}
        hit = {pid for pid, cmd in cmds.items() if any(re.search(m, cmd) for m in q.probe.match)}
        roots = {pid for pid, cmd in cmds.items() if any(re.search(m, cmd) for m in q.probe.children_of)}
        hit |= drivelock.descendants(roots, {pid: ppid for pid, ppid, _ in rows}) - roots
        return sorted((pid, cmds[pid]) for pid in hit - idle - set(mine))

    def blockers(self, name: str, st: State, mine: set = frozenset()) -> list:
        """Why `name` may not run now ([] = it may)."""
        p = self.project(name)
        above = [q for q in self.projects.values() if q.name != name and q.priority <= p.priority]
        why = []
        for q in above:
            why += [f"{q.name} holds the desk (job {h.job}, pid {h.pid})" for h in st.holds if h.project == q.name]
            if q.priority < p.priority:
                why += [f"{q.name} wants the desk: {w.why or 'no reason given'}" for w in st.wants if w.project == q.name]
        probed = [q for q in above if q.priority < p.priority and (q.probe.match or q.probe.children_of)]
        if probed:
            rows = self.table()
            if not rows:
                return why + ["the process table could not be read: not now (fail closed)"]
            mine = set(mine) | drivelock.descendants({os.getpid()}, {pid: ppid for pid, ppid, _ in rows})
            for q in probed:
                why += [f"{q.name} work running: pid {pid} {cmd[:120]}" for pid, cmd in self.probe_hits(q, rows, mine)[:3]]
        return why

    def may(self, name: str) -> list:
        return self._locked(lambda st: self.blockers(name, st))

    def want(self, name: str, ttl: float, why: str) -> None:
        self.project(name)

        def add(st: State) -> None:
            st.wants = [w for w in st.wants if w.project != name] + [Want(name, why, self.now() + ttl, self.now())]
        self._locked(add)

    def unwant(self, name: str) -> None:
        self._locked(lambda st: setattr(st, "wants", [w for w in st.wants if w.project != name]))

    def acquire(self, name: str, job: str, pid: int, mine: set = frozenset()) -> list:
        """[] and the lease recorded, or why not."""
        def take(st: State) -> list:
            why = self.blockers(name, st, mine)
            if not why:
                st.holds = [h for h in st.holds if (h.project, h.job) != (name, job)]
                st.holds.append(Hold(name, job, pid, drivelock.proc_start(pid) or "", self.now()))
            return why
        return self._locked(take)

    def release(self, name: str, job: str) -> None:
        self._locked(lambda st: setattr(st, "holds", [h for h in st.holds if (h.project, h.job) != (name, job)]))

    def status(self) -> dict:
        def show(st: State) -> dict:
            return {"holds": [asdict(h) for h in st.holds], "wants": [asdict(w) for w in st.wants],
                    "may": {n: self.blockers(n, st) for n in sorted(self.projects)}}
        return self._locked(show)


def write_result(path: str, why: str) -> None:
    """Ends a caller's poll of `path` at once when the job wrote nothing."""
    p = Path(path)
    if path and not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"error": f"desk lease: {why}"}), encoding="utf-8")


def run(desk: Desk, name: str, job: str, cmd: list, tick: float = TICK, result: str = "",
        spawn: Callable[[list], subprocess.Popen] | None = None, say=print) -> int:
    """Run cmd under the lease; a higher priority's hold, want or work kills its tree at the next tick."""
    why = desk.acquire(name, job, os.getpid())
    if why:
        say(f"DEFERRED {job}: {why[0]}")
        write_result(result, why[0])
        return NOT_NOW
    spawn = spawn or (lambda c: subprocess.Popen(c, stdin=subprocess.DEVNULL,
                                                 creationflags=NO_WINDOW if os.name == "nt" else 0))
    child = None
    try:
        child = spawn(cmd)
        say(f"RUN {job} pid {child.pid}")
        while True:
            try:
                return child.wait(timeout=tick)
            except subprocess.TimeoutExpired:
                pass
            why = desk.may(name)
            if why:
                drivelock.kill_pid(child.pid)
                child.wait(timeout=60)
                say(f"YIELDED {job}: {why[0]}")
                write_result(result, f"yielded: {why[0]}")
                return YIELD
    finally:
        if child is not None and child.poll() is None:
            drivelock.kill_pid(child.pid)
        desk.release(name, job)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", default=os.environ.get("DESK_LEASE_HOME", str(_HERE)))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for c in ("may", "want", "unwant", "acquire", "check", "release", "run"):
        s = sub.add_parser(c)
        s.add_argument("--project", required=True)
        if c in ("acquire", "check", "release", "run"):
            s.add_argument("--job", required=True)
        if c == "acquire":
            s.add_argument("--pid", type=int, required=True, help="the job's process (the lease lives while it runs)")
        if c == "want":
            s.add_argument("--ttl", type=float, default=1800.0)
            s.add_argument("--why", default="")
        if c == "run":
            s.add_argument("--tick", type=float, default=TICK)
            s.add_argument("--result", default="")
            s.add_argument("argv", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    try:
        desk = Desk(Path(a.home))
        if a.cmd == "status":
            print(json.dumps(desk.status(), indent=1))
            return OK
        if a.cmd == "want":
            desk.want(a.project, a.ttl, a.why)
            return OK
        if a.cmd == "unwant":
            desk.unwant(a.project)
            return OK
        if a.cmd == "release":
            desk.release(a.project, a.job)
            return OK
        if a.cmd == "run":
            cmd = a.argv[1:] if a.argv[:1] == ["--"] else a.argv
            return run(desk, a.project, a.job, cmd, a.tick, a.result)
        why = (desk.acquire(a.project, a.job, a.pid) if a.cmd == "acquire" else desk.may(a.project))
    except (OSError, ValueError, KeyError, drivelock.Held) as e:  # fail closed: no lease decision is "not now"
        print(f"NOT NOW desk lease error: {e}")
        return NOT_NOW
    print("\n".join(why) if why else "OK")
    return (YIELD if a.cmd == "check" else NOT_NOW) if why else OK


if __name__ == "__main__":
    sys.exit(main())
