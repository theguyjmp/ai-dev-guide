"""Alarm dispatcher: each fleet_beat alarm (state/health.json "detail") goes to its handler in alarm_handlers.TABLE.
Auto: an idempotent, time-boxed fix. Owned: an existing owner fixes it. Agent: one headless `claude -p` per lock key.
Manager: ESCALATE at once. An agent lock is live only while its exact process (pid + start time) runs before its
deadline, else the tree is killed and the attempt judged by the check: alarm still firing unchanged = failed. A failed
attempt starts a cooldown; two are ESCALATE until the alarm clears or `--reset KEY`. A cargo-building (slot) agent also
needs a free machine-wide cargo slot (cargo_slots.py, shared with every fleet on this PC) and a train outside its
acceptance stage (state/train-stage.json, written by harness.acceptance_tests).

  py -3 alarm_dispatch.py [--no-beat] [--dry] [--reset KEY]   # supervisor.py starts it every 10 min
"""
from __future__ import annotations

import argparse, os, re, subprocess, sys, time, traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cargo_slots  # noqa: E402
STATE = "dispatch.json"
TRAIN_STAGE = "train-stage.json"  # harness.TRAIN_STAGE
MAX_AGENTS = 2  # the laptop is the gate machine
MAX_ATTEMPTS = 2  # failed agents per key before ESCALATE
AUTO_FAILS = 2  # consecutive failed auto fixes before ESCALATE
AUTO_GAP = 9 * 60
FORGET = 2 * 3600  # a flapping alarm keeps its failures
BEAT_TIMEOUT = 300
ENV = {"CARGO_BUILD_JOBS": "2", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}
RULE_ZERO = ("> RULE ZERO (owner, the most important rule ever): valuable, well made, clean, extensible code; one owner per "
             "capability; never loosen a check, test or gate to get green.\n\n")
TAIL = ("\n\nRules: at most {cap} tool calls; small tool output (tail/grep, `git diff --stat`), no whole files over ~300 "
        "lines, no helper subagents; same step failing twice: stop. Log one line with `py -3 {home}/harness.py note "
        "\"<line>\"`. Hand back at most 2 lines.\n")


SUBJECT = re.compile(r"tests/\S+?\.rs::\w+|notes/(?:requests|briefs)/[\w.-]+\.md")


class Failed(Exception):
    """An auto fix could not do its job."""


@dataclass(frozen=True)
class Alarm:
    id: str
    key: str  # id, or id:subject
    text: str
    subject: str = ""


@dataclass(frozen=True)
class Auto:
    fix: Callable  # (ctx, alarm) -> what it did
    patience: float = 3 * 3600
    budget_s: float = 120


@dataclass(frozen=True)
class Owned:
    owner: str
    patience: float = 3 * 3600


@dataclass(frozen=True)
class Agent:
    template: str
    model: str = "opus"
    slot: bool = False
    cap_min: int = 90
    calls: int = 100
    cooldown_min: int = 30
    covered_by: tuple = ()  # ids that fix this one's cause
    per_subject: bool = False
    hint: str = ""  # names another lane on it


@dataclass(frozen=True)
class Manager:
    action: str


class Launcher:
    """Headless claude through overhaul-build's supervise.py modules."""

    def __init__(self, qs: str):
        sys.path.insert(0, qs)
        import drive, drivelock, memcap, worker  # noqa: E401  beside supervise.py
        self.drive, self.lock, self.worker = drive, drivelock, worker
        self.base = memcap.resolve(["claude"])

    def start(self, brief: Path, cwd: Path, out: Path, model: str, calls: int) -> tuple[int, str]:
        cmd = [*self.base, "-p", "--dangerously-skip-permissions", "--output-format", "json",
               *self.worker.lean_flags(self.base[0], "build"), "--model", model,
               *(["--max-turns", str(calls + 20)] if self.drive.supports_max_turns(self.base[0]) else []),
               f"Read and do {brief.as_posix()}"]
        env = {**(self.worker.env(cmd, None) or dict(os.environ)), **ENV}
        try:
            proc = self.drive.spawn(cmd, cwd, out, env, detach=True)
        except OSError:  # a job above refuses breakaway
            proc = self.drive.spawn(cmd, cwd, out, env)
        return proc.pid, self.lock.proc_start(proc.pid) or ""

    def alive(self, pid: int, start: str) -> bool:
        return self.lock.live(pid, start)

    def kill(self, pid: int) -> None:
        self.lock.kill_pid(pid, tree=True)

    def result(self, out: Path) -> str:
        try:
            return (self.drive.read_result(out)[2] or "").strip()[-300:]
        except Exception as e:  # noqa: BLE001
            return f"no result ({type(e).__name__})"


@dataclass
class Ctx:
    h: object
    now: float
    dry: bool = False
    deadline: float = float("inf")

    def left(self, cap: float = 600) -> float:
        rest = self.deadline - time.time()
        if rest <= 0:
            raise Failed("time box spent")
        return max(1.0, min(cap, rest))


def classify(text: str, table: dict, alarm_id: Callable) -> Alarm:
    aid = alarm_id(text)
    handler = table.get(aid)
    subject = ""
    if isinstance(handler, Agent) and handler.per_subject:
        m = SUBJECT.search(text)
        subject = m.group(0) if m else ""
    return Alarm(aid, f"{aid}:{subject}" if subject else aid, text, subject)


def hhmm(ts: float) -> str:
    return time.strftime("%H:%MZ", time.gmtime(ts))


def holding(at: dict) -> bool:
    return not at.get("end")


def failed(entry: dict) -> list:
    return [x for x in entry.get("attempts", []) if x.get("outcome") == "failed"]


def cooling(entry: dict, handler, now: float) -> float:
    at = entry.get("attempts", [])
    if not at or at[-1].get("outcome") != "failed":
        return 0.0
    until = at[-1]["end"] + handler.cooldown_min * 60
    return until if now < until else 0.0


def working(entry: dict, handler, now: float) -> bool:
    if not entry or entry.get("escalated"):
        return False
    if isinstance(handler, Agent):
        at = entry.get("attempts", [])
        return bool(at) and (holding(at[-1]) or bool(cooling(entry, handler, now)))
    if isinstance(handler, Auto):
        return bool(entry.get("auto")) and entry["auto"][-1]["ok"]
    return isinstance(handler, Owned)


def status(entry: dict | None, handler, now: float) -> str:
    if handler is None:
        return "ESCALATE: no handler"
    if isinstance(handler, Manager):
        return f"ESCALATE: manager: {handler.action}"
    entry = entry or {}
    if entry.get("escalated"):
        return f"ESCALATE: {entry['escalated']}"
    since = hhmm(entry.get("first", now))
    if isinstance(handler, Owned):
        return f"handled-by {handler.owner} since {since}"
    if isinstance(handler, Auto):
        a = entry.get("auto", [])
        last = f": {'' if a[-1]['ok'] else 'last failed: '}{a[-1]['did'][:80]}" if a else ": next dispatch"
        return f"handled-by auto fix since {since}{last}"
    at = entry.get("attempts", [])
    if at and holding(at[-1]):
        return f"handled-by {at[-1]['tag']} since {hhmm(at[-1]['ts'])}"
    until = cooling(entry, handler, now)
    if until:
        return f"cooldown until {hhmm(until)} after {len(failed(entry))} failed attempt(s)"
    return entry.get("note") or "agent: next dispatch"


class Dispatcher:
    def __init__(self, h, table: dict, alarm_id: Callable, launcher=None, dry: bool = False,
                 briefs: Path = HERE / "alarm_briefs", slots: tuple = (300, 301), busy: Callable | None = None,
                 cargo: cargo_slots.CargoSlots | None = None, fleet: str = ""):
        self.h, self.table, self.alarm_id, self.launcher, self.dry = h, table, alarm_id, launcher, dry
        self.briefs, self.slots, self.busy = briefs, slots, busy or (lambda h, a, handler: "")
        self.cargo, self.fleet = cargo, fleet or Path(getattr(h, "HOME", HERE)).name
        self.log: list[str] = []

    def act(self, action: str, key: str, **kw) -> None:
        self.log.append(f"{action} {key} " + " ".join(f"{k}={v}" for k, v in kw.items()))
        if not self.dry:
            self.h.event("dispatch", action=action, key=key, **kw)

    def run(self, texts: list[str], now: float | None = None) -> dict:
        now = self.h.now() if now is None else now
        st = self.h.load(STATE, {})
        alarms = {a.key: a for a in (classify(t, self.table, self.alarm_id) for t in texts)}
        self.reap(st, alarms, now)
        for key, a in alarms.items():
            e = st.setdefault(key, {"id": a.id, "first": now, "attempts": [], "auto": []})
            e.update(text=a.text[:400], seen=now)
            if e.get("escalated"):
                continue
            handler = self.table.get(a.id)
            if handler is None:
                self.escalate(e, key, "no handler in alarm_handlers.TABLE")
            elif isinstance(handler, Manager):
                self.escalate(e, key, f"manager: {handler.action}")
            elif isinstance(handler, Owned):
                if now - e["first"] > handler.patience:
                    self.escalate(e, key, f"{handler.owner} has not cleared it in {round(handler.patience / 3600, 1)} h")
            elif isinstance(handler, Auto):
                self.auto(a, handler, e, now)
        # agents last, causes before the symptoms they cover
        agents = [(a, self.table[a.id]) for a in alarms.values() if isinstance(self.table.get(a.id), Agent)]
        for a, handler in sorted(agents, key=lambda x: len(x[1].covered_by)):
            if not st[a.key].get("escalated"):
                self.agent(st, a, handler, st[a.key], now)
        for key in [k for k in st if k not in alarms]:
            e = st[key]
            if any(holding(x) for x in e.get("attempts", [])):
                continue
            if e.get("escalated") or e.get("seen", 0) < now - FORGET or not (e.get("attempts") or e.get("auto")):
                if e.get("attempts") or e.get("auto") or e.get("escalated"):
                    self.act("resolved", key, attempts=len(e.get("attempts", [])), failed=len(failed(e)),
                             autos=len(e.get("auto", [])), escalated=bool(e.get("escalated")))
                del st[key]
        if not self.dry:
            self.h.save(STATE, st)
        return st

    def reap(self, st: dict, alarms: dict, now: float) -> None:
        for key, e in st.items():
            for at in e.get("attempts", []):
                if not holding(at):
                    continue
                alive = self.launcher.alive(at["pid"], at.get("start", ""))
                if alive and now < at["deadline"]:
                    continue
                if alive:
                    if not self.dry:
                        self.launcher.kill(at["pid"])
                    self.act("kill", key, tag=at["tag"], pid=at["pid"], minutes=round((now - at["ts"]) / 60))
                a = alarms.get(key)
                at["end"] = now
                at["result"] = self.launcher.result(Path(at["out"]))
                at["outcome"] = ("failed" if alive else "cleared" if a is None else
                                 "progress" if fingerprint(a.text) != at["fp"] else "failed")
                self.act("end", key, tag=at["tag"], outcome=at["outcome"], killed=alive, result=at["result"][-160:])

    def escalate(self, e: dict, key: str, why: str) -> None:
        e["escalated"] = why
        self.act("escalate", key, why=why)

    def auto(self, a: Alarm, handler: Auto, e: dict, now: float) -> None:
        runs = e.setdefault("auto", [])
        fails = 0
        for r in reversed(runs):
            if r["ok"]:
                break
            fails += 1
        if fails >= AUTO_FAILS:
            return self.escalate(e, a.key, f"auto fix failed {fails} times: {runs[-1]['did'][:120]}")
        if runs and now - e["first"] > handler.patience:
            return self.escalate(e, a.key, f"still firing {round((now - e['first']) / 3600, 1)} h after auto fixes")
        if runs and now - runs[-1]["ts"] < AUTO_GAP:
            return
        t0 = time.time()
        ctx = Ctx(self.h, now, self.dry, t0 + handler.budget_s)
        try:
            did, ok = handler.fix(ctx, a), True
        except Failed as x:
            did, ok = str(x), False
        except Exception as x:  # noqa: BLE001 - a crash is a failed fix
            did, ok = f"{type(x).__name__}: {str(x)[-200:]}", False
        secs = time.time() - t0
        if ok and secs > handler.budget_s + 30:
            did, ok = f"overran its {handler.budget_s:g} s time box ({secs:.0f} s): {did}", False
        runs.append({"ts": now, "ok": ok, "did": did[:300], "secs": round(secs, 1)})
        del runs[:-5]
        self.act("auto", a.key, ok=ok, secs=round(secs, 1), did=did[:160])

    def agent(self, st: dict, a: Alarm, handler: Agent, e: dict, now: float) -> None:
        at = e["attempts"]
        if at and holding(at[-1]):
            return
        bad = failed(e)
        if len(bad) >= MAX_ATTEMPTS:
            return self.escalate(e, a.key, f"{len(bad)} failed attempts (last: {bad[-1].get('result', '')[-120:]})")
        until = cooling(e, handler, now)
        if until:
            e["note"] = f"cooldown until {hhmm(until)}"
            return
        cover = [k for k, x in st.items() if k != a.key and x.get("id") in handler.covered_by
                 and x.get("seen") == now and working(x, self.table.get(x["id"]), now)]
        if cover:
            e["note"] = f"handled-by {cover[0]} (its cause)"
            return
        running = [x for v in st.values() for x in v.get("attempts", []) if holding(x)]
        if len(running) >= MAX_AGENTS:
            e["note"] = f"waiting: {MAX_AGENTS} dispatched agents already run"
            return
        slot = None
        if handler.slot:
            busy = {x.get("slot") for x in running}
            slot = next((s for s in self.slots if s not in busy), None)
            if slot is None:
                e["note"] = "waiting for a dispatch slot"
                return
            hold = self.cargo_hold()
            if hold:
                e["note"] = hold
                return
        lane = self.busy(self.h, a, handler)
        if lane:
            e["note"] = f"handled-by lane {lane}"
            return
        e.pop("note", None)
        try:
            self.launch(a, handler, e, slot, now)
        except cargo_slots.Full as x:
            e["note"] = f"waiting: {x}"

    def cargo_hold(self) -> str:
        if train_stage(self.h, self.launcher.alive) == "acceptance":
            return "waiting: the train is in its acceptance stage (no cargo-building dispatch)"
        if self.cargo is not None:
            live = self.cargo.holders()
            if len(live) >= self.cargo.cap:
                return f"waiting: {len(live)}/{self.cargo.cap} machine-wide cargo slots held"
        return ""

    def launch(self, a: Alarm, handler: Agent, e: dict, slot, now: float) -> None:
        h = self.h
        tag = re.sub(r"[^a-z0-9]+", "-", f"{a.key}".lower()).strip("-")[:40] + f"-{len(e['attempts']) + 1}"
        work = h.ST / "dispatch"
        brief, out = work / f"{tag}.md", work / f"{tag}.json"
        bad = failed(e)
        facts = a.text if not bad else (a.text + f"\nAn earlier agent did not clear it; its last words: "
                                        f"{bad[-1].get('result', '')[-300:]}. Take a different approach.")
        values = {"facts": facts, "subject": a.subject, "slot": str(slot or ""), "name": tag.replace("-", "")[:24],
                  "home": Path(h.HOME).as_posix(), "repo": Path(getattr(h, "REPO", h.HOME)).as_posix(), "key": a.key}
        text = RULE_ZERO + render((self.briefs / f"{handler.template}.md").read_text(encoding="utf-8"), values) \
            + TAIL.format(cap=handler.calls, home=values["home"])
        if self.dry:  # in memory only, so the guards still apply
            pid, start = -1, ""
        else:
            work.mkdir(parents=True, exist_ok=True)
            brief.write_text(text, encoding="utf-8")

            def spawn() -> tuple[int, str]:
                return self.launcher.start(brief, Path(h.HOME), out, handler.model, handler.calls)
            cargo = handler.slot and self.cargo is not None
            pid, start = self.cargo.claim(self.fleet, tag, spawn) if cargo else spawn()
        e["attempts"].append({"tag": tag, "ts": now, "pid": pid, "start": start, "deadline": now + handler.cap_min * 60,
                              "brief": brief.as_posix(), "out": out.as_posix(), "slot": slot, "fp": fingerprint(a.text)})
        self.act("launch", a.key, tag=tag, template=handler.template, model=handler.model, slot=slot, pid=pid,
                 deadline=hhmm(now + handler.cap_min * 60))


def annotate(h, texts: list, table: dict, alarm_id: Callable, now: float) -> list:
    st = h.load(STATE, {})
    out = []
    for t in texts:
        a = classify(t, table, alarm_id)
        out.append((t, status(st.get(a.key), table.get(a.id), now)))
    return out


def train_stage(h, alive: Callable[[int, str], bool]) -> str:
    """The gate stage the train marked in state/train-stage.json ('' when none, or its process is gone)."""
    mark = h.load(TRAIN_STAGE, {})
    return mark.get("stage", "") if mark.get("pid") and alive(mark["pid"], "") else ""


def fingerprint(text: str) -> str:
    """The alarm without its numbers, so it stays the same alarm as it ages."""
    return re.sub(r"\d+(\.\d+)?", "#", text)[:300]


def render(template: str, values: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def run_beat(h) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    py = sys.executable.replace("pythonw", "python")
    subprocess.run([py, "fleet_beat.py"], cwd=str(h.HOME), capture_output=True, timeout=BEAT_TIMEOUT, creationflags=flags)


def main(argv=None) -> int:
    import harness as h
    import alarm_handlers
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-beat", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--reset", default="", help="forget a lock key (after the manager fixed an ESCALATE item)")
    a = ap.parse_args(argv)
    if a.reset:
        with h.lock("dispatch", wait=900):
            st = h.load(STATE, {})
            print(f"reset {a.reset}: {'forgotten' if st.pop(a.reset, None) else 'no such key'}")
            h.save(STATE, st)
            h.event("dispatch", action="reset", key=a.reset)
        return 0
    try:
        with h.lock("dispatch", wait=0):
            if not a.no_beat:
                run_beat(h)
            texts = h.load("health.json", {}).get("detail", [])
            launcher = Launcher(str(h.QS))
            d = Dispatcher(h, alarm_handlers.TABLE, alarm_handlers.alarm_id, launcher, dry=a.dry,
                           busy=alarm_handlers.manual_lane, cargo=cargo_slots.CargoSlots(launcher.alive),
                           fleet=os.environ.get("FLEET_NAME", ""))
            d.run(texts)
            print("\n".join(d.log) or "dispatch: nothing to do")
    except TimeoutError:
        print("dispatch: another run holds state/dispatch.lock")
    except Exception:  # noqa: BLE001 - detached: the error must reach events.jsonl
        h.event("dispatch_error", error=traceback.format_exc()[-1500:])
        raise
    return 0


if __name__ == "__main__":  # run the importable module: alarm_handlers' Agent/Auto classes must be this module's
    import alarm_dispatch
    sys.exit(alarm_dispatch.main())
