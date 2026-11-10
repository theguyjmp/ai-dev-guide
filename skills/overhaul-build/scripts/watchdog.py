#!/usr/bin/env python3
"""watchdog.py: invariants of a running overhaul build, checked every 5 minutes; each breach acts or alerts at once.

    py -3 watchdog.py --repo R [--once] [--every 300]

supervise.py runs it as a thread (start()); by hand, --once checks once. Reads .overhaul/log.jsonl (its tail),
.overhaul/state.json, overhaul.toml and the plan's FUNCTIONS.md, never git. Two kinds of invariant, one table
(INVARIANTS: name, when it is due, the evidence that it is happening, the action when it is not):
- something is wrong (starved, no_land, disk, stop, host_offline, audit_lock);
- something that should be happening is not (phase_audit, arch_sweep, periodic, progress, reload, needs_plan,
  critical_plan, proof_check, dashboard): owner 2026-11-10, "if a phase has gone from unfinished to finished, check that the
  appropriate checkers have audited or are auditing, if not trigger them" (the per-phase audit and the arch sweep
  never ran for days and nothing noticed).
Each breach writes a `watchdog` event to log.jsonl and acts: a request file drive honours at its next cycle
(.overhaul/requests/audit-phase-<N>, arch-sweep, periodic-<gate>, reload-watchdog-<time>; planfix-<id> for supervise), a stale lock removed,
incremental dirs pruned, a host held offline (.overhaul/remote/<name>.offline), the dashboard rewritten. A request
and anything it cannot fix also append one line to .overhaul/ALERT.txt for the manager; nothing passes silently.
A breach that persists repeats its event and alert at most every 30 min. Its memory (first seen, last fired, last
check) is .overhaul/watchdog.json.

Why (pilot2026-11-09/10): 17 gaps of over 45 min between lands, 5 whole-build stops and 5 desk hangs in 13 minutes
were each found by the manager's 30-minute heartbeat or by the owner, not when they happened; 0 phase audits and 0
arch sweeps ran in two days with phase_audit = true; 55 periodic reds after the last bisect drew no action.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import remote  # noqa: E402

EVERY_S = 300.0
STARVED_S = 600.0
NO_LAND_S = 45 * 60.0
OFFLINE_WINDOW_S = 30 * 60.0
HOLD_S = 30 * 60.0
REFIRE_S = 30 * 60.0
PRUNE_AT_GB = 80.0
QUIET_S = 15 * 60.0  # an incremental dir written to within this is in use
AUDIT_DUE_S = 15 * 60.0  # a phase audit or arch sweep starts within this of falling due
PROGRESS_S = 30 * 60.0  # some build/review/verify/land event within this while rows are ready and slots free
RELOAD_S = 10 * 60.0  # drive applies a reset/reload request within this
PLAN_ROUND_S = 30 * 60.0  # needs-plan rows: a plan round in flight or finished within this
CRITICAL_ROUND_S = 15 * 60.0  # a needs-plan row with critical_waiters rows waiting: a round in flight within this
CRITICAL_WAITERS = 10  # [harness] critical_waiters (supervise.py's default too)
PROOF_DUE_S = 60 * 60.0  # a proof-check after a phase end
DASH_S = 60 * 60.0  # the dashboard is rewritten within this while the build is live
AUDIT_LOCK_S = 20 * 60.0  # a module held by an audit fix (st audit_locks) longer than this (pilot2026-11-10: 30+ min)
TAIL_BYTES = 4 << 20
INCREMENTAL = ("*/debug/incremental/*", "*/release/incremental/*")
PROGRESS = ("build_start", "build_end", "verdict", "review_pass", "review_fail", "review_nos", "verify_fail",
            "land_ok", "land_fail", "polish_fix", "polish_land_ok")
PLAN_EVENTS = ("plan_reload", "reset_applied", "reset_refused", "reload_refused")
GATE_CONFIG = re.compile(r"exit 12[67]\b|command not found|not recognized as|No such file or directory|"
                         r"cannot find the path", re.I)  # the gate itself is broken: the owner's, not a bisect's

# name, due, evidence that it is happening, action when it is not
INVARIANTS = (
    ("starved", "ready rows beside free slots (state width) for 10 min", "a build_start in the last 10 min",
     "reload request + alert"),
    ("no_land", "rows in `landing`", "a land_ok in the last 45 min", "stale .overhaul/lock removed, else alert"),
    ("disk", "free space on the repo's drive under [limits] prune_at_gb (80)", "-",
     "quiet incremental dirs pruned, alert when still under"),
    ("stop", "a `stop` event since the last check", "-", "alert with its line"),
    ("host_offline", "one remote host offline twice in 30 min", "-", "host held offline 30 min + alert"),
    ("audit_lock", "a module in st audit_locks (held by an audit fix)", "held under 20 min",
     "request release-audit-locks + alert"),
    ("phase_audit", "phase N all proven 15 min ago, or [harness] phase_audit_every lands of phase N since its last "
     "audit", "st phase_polish[N] started or finished, or a phase_audit event for N",
     "request audit-phase-<N> + alert"),
    ("arch_sweep", "arch_audit_every_lands proven since the last sweep, 15 min ago",
     "st arch_sweep not idle, or an arch_audit start of the sweep", "request arch-sweep + alert"),
    ("periodic", "a periodic gate red for one cycle (5 min)",
     "a bisect, culprit or depfix event for that gate since the red",
     "request periodic-<gate> + alert; a broken gate command (exit 126/127, not found) or no green ever: owner alert"),
    ("progress", "ready rows beside free slots", "a build/review/verify/land event in the last 30 min",
     "reload request + alert"),
    ("reload", "a reset-/reload- request file", "drive removed it within 10 min", "alert (with the last refusal)"),
    ("needs_plan", "needs-plan rows for 30 min", "supervise round.json, or a plan reload/reset event in 30 min",
     "alert"),
    ("critical_plan", "a needs-plan row with [harness] critical_waiters (10) not-proven rows waiting on it, 15 min",
     "its id in supervise round.json", "request planfix-<id> (supervise starts its own round) + alert"),
    ("proof_check", "a phase finished 60 min ago", ".overhaul/proof-check.txt written after it", "alert"),
    ("dashboard", "log events in the last 60 min", "dash/index.html written in the last 60 min",
     "dash.py run, alert when still stale"),
)


def parse_t(text: str) -> float:
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except ValueError:
        return 0.0


def read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def events(ov: Path) -> list[dict]:
    """The last TAIL_BYTES of log.jsonl as events, each with `ts` (epoch seconds)."""
    try:
        with open(ov / "log.jsonl", "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and "event" in ev:
            ev["ts"] = parse_t(ev.get("t", ""))
            out.append(ev)
    return out


def last_ts(evs: list[dict], pred) -> float:
    return max((e["ts"] for e in evs if pred(e)), default=0.0)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


class Watchdog:
    def __init__(self, repo: Path, free_gb=None, now=None):
        self.repo, self.ov = repo, repo / ".overhaul"
        self._free_gb, self._now = free_gb, now
        self.mem_path = self.ov / "watchdog.json"
        self.conf: dict = {}

    def now(self) -> float:
        return self._now() if self._now else time.time()

    def free_gb(self) -> float:
        if self._free_gb:
            return self._free_gb()
        try:
            return shutil.disk_usage(self.repo).free / 1e9
        except OSError:
            return float("inf")

    def load_conf(self) -> dict:
        try:
            return tomllib.loads((self.repo / "overhaul.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    def limits(self) -> dict:
        return (self.conf or self.load_conf()).get("limits") or {}

    def harness(self) -> dict:
        return self.conf.get("harness") or {}

    def plan_rows(self) -> list:
        """The plan's active (not deferred) rows from its FUNCTIONS.md; [] when unreadable."""
        import drive  # noqa: PLC0415 - heavy, and only the plan checks need it
        rel = (self.conf.get("paths") or {}).get("functions") or drive.DEFAULTS["paths"]["functions"]
        try:
            text = (self.repo / rel).read_text(encoding="utf-8")
        except OSError:
            return []
        return [f for f in drive.parse_functions(text) if f.status != drive.DEFERRED]

    def phases(self, st: dict) -> dict[int, list[str]]:
        """Phase number -> the ids of its active rows (the plan's FUNCTIONS.md, rows drive has in state.json)."""
        import drive  # noqa: PLC0415
        recs, out = st.get("functions") or {}, {}
        for f in self.plan_rows():
            if f.id in recs:
                out.setdefault(drive.phase_key(f)[0], []).append(f.id)
        return out

    # ---- one check ----
    def check(self) -> list[str]:
        """Every invariant once; returns the breaches that fired (their names)."""
        now, mem = self.now(), read_json(self.mem_path)
        self.conf = self.load_conf()
        evs, st = events(self.ov), read_json(self.ov / "state.json")
        since = float(mem.get("last_check") or now - EVERY_S)
        fired = []
        checks = {
            "starved": lambda: self.starved(now, mem, evs, st), "no_land": lambda: self.no_land(now, evs, st),
            "disk": self.disk, "stop": lambda: self.stopped(evs, since),
            "host_offline": lambda: self.host_offline(now, evs), "audit_lock": lambda: self.audit_lock(now, st),
            "phase_audit": lambda: self.phase_audit(now, evs, st), "arch_sweep": lambda: self.arch_sweep(now, mem, st),
            "periodic": lambda: self.periodic(now, evs, st), "progress": lambda: self.progress(now, mem, evs, st),
            "reload": lambda: self.reload(now, mem, evs), "needs_plan": lambda: self.needs_plan(now, mem, evs, st),
            "critical_plan": lambda: self.critical_plan(now, mem, st),
            "proof_check": lambda: self.proof_check(now, evs, st), "dashboard": lambda: self.dashboard(now, evs)}
        for name, *_ in INVARIANTS:
            try:
                breach = checks[name]()
            except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
                breach = (f"check failed: {type(e).__name__}: {e}", None)
            if breach is None:
                continue
            detail, act = breach
            last = float(mem.setdefault("fired", {}).get(name) or 0)
            if name != "stop" and now - last < REFIRE_S:
                continue
            outcome = act() if act else ""
            line = f"{name}: {detail}" + (f"; {outcome}" if outcome else "")
            self.event(line)
            if not outcome or outcome.startswith("alert"):
                self.alert(line)
            mem["fired"][name] = now
            fired.append(name)
        mem["last_check"] = now
        self.ov.mkdir(parents=True, exist_ok=True)
        self.mem_path.write_text(json.dumps(mem), encoding="utf-8")
        return fired

    # ---- something is wrong ----
    def starved(self, now, mem, evs, st):
        width = st.get("width") or {}
        ready, free = int(width.get("ready") or 0), int(width.get("free") or 0)
        first = mem.setdefault("first", {})
        if not (ready and free):
            first.pop("starved", None)
            return None
        first.setdefault("starved", now)
        last_start = last_ts(evs, lambda e: e["event"] == "build_start")
        if now - float(first["starved"]) < STARVED_S or now - last_start < STARVED_S:
            return None
        return (f"{ready} ready rows beside {free} free slots and no build_start for "
                f"{(now - max(last_start, 0.0)) / 60:.0f} min" if last_start else
                f"{ready} ready rows beside {free} free slots and no build_start", self.reload_request)

    def no_land(self, now, evs, st):
        landing = [f for f, r in (st.get("functions") or {}).items() if isinstance(r, dict)
                   and r.get("status") == "landing"]
        if not landing:
            return None
        last = last_ts(evs, lambda e: e["event"] == "land_ok")
        if last and now - last < NO_LAND_S:
            return None
        if not last and evs and now - min(e["ts"] for e in evs) < NO_LAND_S:
            return None
        ago = f"{(now - last) / 60:.0f} min" if last else "the whole log tail"
        return f"no land_ok for {ago} with {len(landing)} landing ({' '.join(sorted(landing)[:6])})", self.stale_lock

    def disk(self):
        floor = float(self.limits().get("prune_at_gb", PRUNE_AT_GB) or 0)
        free = self.free_gb()
        if floor <= 0 or free >= floor:
            return None
        return f"free {free:.1f} GB < prune_at_gb {floor:g}", lambda: self.prune(floor)

    def stopped(self, evs, since):
        stops = [e for e in evs if e["event"] == "stop" and e["ts"] > since]
        if not stops:
            return None
        return f"drive stopped: {str(stops[-1].get('detail', ''))[:300]}", None

    def host_offline(self, now, evs):
        by: dict[str, list[float]] = {}
        for e in evs:
            if e["event"] == "remote_offline" and now - e["ts"] <= OFFLINE_WINDOW_S:
                host = str(e.get("detail", "")).partition("host=")[2].split(" ")[0] or "?"
                by.setdefault(host, []).append(e["ts"])
        twice = sorted(h for h, ts in by.items() if len(ts) >= 2)
        if not twice:
            return None
        return (f"host {' '.join(twice)} offline {max(len(by[h]) for h in twice)} times in 30 min",
                lambda: self.hold(twice))

    # ---- something that should be happening is not ----
    def audit_lock(self, now, st):
        """No module stays locked by an audit fix over 20 min (drive's st audit_locks {module: since})."""
        locks = st.get("audit_locks") or {}
        old = sorted(m for m, t in locks.items() if isinstance(t, (int, float)) and now - float(t) > AUDIT_LOCK_S)
        if not old:
            return None
        mins = (now - min(float(locks[m]) for m in old)) / 60
        return (f"{len(old)} module(s) audit-locked over {AUDIT_LOCK_S / 60:.0f} min (longest {mins:.0f} min): "
                f"{' '.join(old[:8])}", lambda: self.request("release-audit-locks"))

    def phase_audit_on(self) -> bool:
        har = self.harness()
        polish = int(self.limits().get("polish_rounds", 1) or 0) >= 1
        on = bool(har.get("phase_audit", True)) or (bool(har.get("phase_barrier", True))
                                                    and bool(har.get("phase_polish", True)))
        return polish and on

    def phase_audit(self, now, evs, st):
        """Each phase that finished (or had phase_audit_every lands) has a phase audit started or done."""
        if not self.phase_audit_on():
            return None
        phases = self.phases(st)
        if len(phases) < 2:
            return None  # one phase: the final polish is its audit
        recs, pp = st.get("functions") or {}, st.get("phase_polish") or {}
        final_idle = (st.get("polish") or {}).get("status", "idle") == "idle" and not (st.get("polish") or {}).get(
            "round")
        every = int(self.harness().get("phase_audit_every", 0) or 0)
        tail0 = min((e["ts"] for e in evs), default=now)
        due = []
        for n, ids in sorted(phases.items()):
            if n == max(phases) and final_idle:
                continue  # the last phase: the final whole-tree polish audits it
            pol = pp.get(str(n)) or {}
            running = pol.get("status") in ("gaps", "audit", "fix")
            started = running or pol.get("status") in ("done", "failed") or bool(pol.get("round"))
            audited = last_ts(evs, lambda e, n=n: (e["event"] == "phase_audit" and str(e.get("detail", "")).startswith(
                f"phase {n} ")) or (e["event"] in ("request_applied", "request_refused")
                                    and str(e.get("detail", "")).startswith(f"audit-phase-{n}")))
            ids_set = set(ids)
            lands = [e["ts"] for e in evs if e["event"] == "land_ok" and e.get("fn") in ids_set]
            if all((recs.get(i) or {}).get("status") == "proven" for i in ids):
                ended = max(lands, default=tail0)
                if not started and not (audited and audited >= ended) and now - ended >= AUDIT_DUE_S:
                    due.append((n, f"phase {n} all proven {(now - ended) / 60:.0f} min ago, no audit"))
                    continue
            if every and not running:
                since = sum(1 for t in lands if t > audited)
                if since >= every:
                    due.append((n, f"phase {n}: {since} lands since its last audit (phase_audit_every {every})"))
        if not due:
            return None
        return "; ".join(d for _, d in due), lambda: self.request(*[f"audit-phase-{n}" for n, _ in due])

    def arch_sweep(self, now, mem, st):
        """arch_audit_every_lands proven since the last sweep: the sweep starts within 15 min."""
        har = self.harness()
        every = int(har.get("arch_audit_every_lands", 25) or 0)
        first = mem.setdefault("first", {})
        sw = st.get("arch_sweep") or {"at": 0, "status": "idle"}
        recs = st.get("functions") or {}
        proven = sum(1 for r in recs.values() if isinstance(r, dict) and r.get("status") == "proven")
        if (not har.get("phase_audit", True) or every < 1 or proven == len(recs) or sw.get("status", "idle") != "idle"
                or proven - int(sw.get("at") or 0) < every):
            first.pop("arch_sweep", None)
            return None
        first.setdefault("arch_sweep", now)
        if now - float(first["arch_sweep"]) < AUDIT_DUE_S:
            return None
        return (f"{proven - int(sw.get('at') or 0)} proven since the last sweep (at {sw.get('at', 0)}, every "
                f"{every}) and no sweep for {(now - float(first['arch_sweep'])) / 60:.0f} min",
                lambda: self.request("arch-sweep"))

    def periodic(self, now, evs, st):
        """A red periodic gate is bisected or depfixed within one cycle; a broken gate command is the owner's."""
        per = st.get("periodic") or {}
        if (per.get("bisect") or {}).get("gate"):
            return None  # a bisect is running
        owner, ask = [], []
        for gate, rec in sorted((per.get("red") or {}).items()):
            red = last_ts(evs, lambda e, g=gate: e["event"] == "periodic_red" and str(e.get("detail", "")).startswith(
                f"{g} "))
            if not red or now - red < EVERY_S:
                continue
            tag = f"depfix-periodic-{gate}"
            acted = last_ts(evs, lambda e, g=gate, t=tag: (
                (e["event"] == "periodic_start" and str(e.get("detail", "")).startswith(f"{g} bisect"))
                or (e["event"] == "periodic_culprit" and str(e.get("detail", "")).startswith(f"{g}:"))
                or e.get("fn") == t or t in str(e.get("detail", ""))
                or (e["event"] == "request_applied" and e.get("detail") == f"periodic-{g}")))
            if acted >= red:
                continue
            reason = str(rec.get("reason", ""))[:160]
            refused = [e for e in evs if e["event"] == "request_refused" and e["ts"] >= red
                       and str(e.get("detail", "")).startswith(f"periodic-{gate}:")]
            if GATE_CONFIG.search(reason):
                owner.append(f"{gate} gate command broken ({reason})")
            elif "no earlier green" in str(rec.get("culprit", "")):
                owner.append(f"{gate} red with no earlier green ({reason})")
            elif refused:
                owner.append(f"{gate}: {str(refused[-1].get('detail', ''))[:200]}")
            else:
                ask.append(gate)
        if not owner and not ask:
            return None
        detail = "; ".join([f"{g} red with no bisect or depfix since" for g in ask] + [f"owner: {o}" for o in owner])
        return detail, (lambda: self.request(*[f"periodic-{g}" for g in ask])) if ask else None

    def progress(self, now, mem, evs, st):
        """Rows ready beside free slots: some build, review, verify or land event within 30 min."""
        width = st.get("width") or {}
        first = mem.setdefault("first", {})
        if not (int(width.get("ready") or 0) and int(width.get("free") or 0)):
            first.pop("progress", None)
            return None
        first.setdefault("progress", now)
        last = last_ts(evs, lambda e: e["event"] in PROGRESS)
        if now - float(first["progress"]) < PROGRESS_S or now - last < PROGRESS_S:
            return None
        ago = f"{(now - last) / 60:.0f} min" if last else "the whole log tail"
        return f"no build/review/verify/land event for {ago} with rows ready and slots free", self.reload_request

    def reload(self, now, mem, evs):
        """A reset-/reload- request is applied (removed) by drive within 10 min (first seen by the watchdog)."""
        req, seen = self.ov / "requests", mem.setdefault("requests", {})
        names = sorted(p.name for p in req.glob("re*-*") if p.is_file()) if req.is_dir() else []
        for gone in set(seen) - set(names):
            del seen[gone]
        for n in names:
            seen.setdefault(n, now)
        old = [n for n in names if now - float(seen[n]) >= RELOAD_S]
        if not old:
            return None
        refusals = [e for e in evs if e["event"] == "reload_refused"]
        why = f"; last refusal: {str(refusals[-1].get('detail', ''))[:200]}" if refusals else "; drive not running?"
        return f"{len(old)} plan requests waiting over 10 min ({' '.join(old[:4])}){why}", None

    def needs_plan(self, now, mem, evs, st):
        """needs-plan rows have a plan round in flight or one finished within 30 min."""
        first = mem.setdefault("first", {})
        rows = sorted(f for f, r in (st.get("functions") or {}).items() if isinstance(r, dict)
                      and r.get("status") == "needs-plan")
        if not rows:
            first.pop("needs_plan", None)
            return None
        first.setdefault("needs_plan", now)
        if (self.ov / "supervise" / "round.json").is_file():
            return None
        last = last_ts(evs, lambda e: e["event"] in PLAN_EVENTS)
        if now - float(first["needs_plan"]) < PLAN_ROUND_S or now - last < PLAN_ROUND_S:
            return None
        return (f"{len(rows)} needs-plan rows ({' '.join(rows[:6])}) and no plan round for "
                f"{(now - max(last, float(first['needs_plan']))) / 60:.0f} min", None)

    def critical_plan(self, now, mem, st):
        """A needs-plan row that critical_waiters or more not-proven rows wait on (drive.waiters, the same weight
        drive and supervise rank by) is in a plan round within 15 min (pilot2026-11-10: Q09 blocked 80 rows and
        sat needs-plan for hours while rounds fixed low-value rows)."""
        import drive  # noqa: PLC0415
        recs = st.get("functions") or {}
        seen = mem.setdefault("critical", {})
        needs = [fid for fid, r in recs.items() if isinstance(r, dict) and r.get("status") == "needs-plan"]
        if not needs:
            seen.clear()
            return None
        wait = drive.waiters(self.plan_rows(), recs)
        limit = int(self.harness().get("critical_waiters") or CRITICAL_WAITERS)
        flying = set(read_json(self.ov / "supervise" / "round.json").get("ids") or [])
        crit = sorted((f for f in needs if wait.get(f, 0) >= limit and f not in flying), key=lambda f: (-wait[f], f))
        for fid in [f for f in seen if f not in crit]:
            del seen[fid]
        late = [fid for fid in crit if now - float(seen.setdefault(fid, now)) >= CRITICAL_ROUND_S]
        if not late:
            return None
        return ("; ".join(f"{fid} needs-plan with {wait[fid]} rows waiting and no plan round for "
                          f"{(now - float(seen[fid])) / 60:.0f} min" for fid in late[:4]),
                lambda: self.request(*[f"planfix-{fid}" for fid in late]))

    def proof_check(self, now, evs, st):
        """A blind proof-check after each phase end (the manager runs it; supervise.py runs it at the very end)."""
        phases, recs = self.phases(st), st.get("functions") or {}
        tail0 = min((e["ts"] for e in evs), default=now)
        ended = []
        for n, ids in phases.items():
            if all((recs.get(i) or {}).get("status") == "proven" for i in ids):
                ids_set = set(ids)
                ended.append((max((e["ts"] for e in evs if e["event"] == "land_ok" and e.get("fn") in ids_set),
                                  default=tail0), n))
        if not ended:
            return None
        t, n = max(ended)
        done = max(_mtime(self.ov / "proof-check.txt"), _mtime(self.ov / "proof-check-1.txt"))
        if done >= t or now - t < PROOF_DUE_S:
            return None
        return (f"phase {n} ended {(now - t) / 60:.0f} min ago and no proof-check since "
                f"(references/proof-check.md)", None)

    def dashboard(self, now, evs):
        """While the build is live, dash/index.html is rewritten at least hourly."""
        last = max((e["ts"] for e in evs), default=0.0)
        page = self.repo / "dash" / "index.html"
        if not page.exists() or not last or now - last >= DASH_S or now - _mtime(page) < DASH_S:
            return None  # never written: dash.py is not installed beside drive (drive says so itself)
        return f"dash/index.html {(now - _mtime(page)) / 60:.0f} min old while the build is live", self.redash

    # ---- actions ----
    def request(self, *names: str) -> str:
        """Write .overhaul/requests/<name> for drive; one already waiting is reported, not rewritten."""
        req, made, waiting = self.ov / "requests", [], []
        req.mkdir(parents=True, exist_ok=True)
        for name in names:
            path = req / name
            if path.exists():
                waiting.append(name)
                continue
            path.write_text(f"watchdog {datetime.fromtimestamp(self.now()).isoformat(timespec='seconds')}\n",
                            encoding="utf-8")
            made.append(name)
        parts = ([f"requested {' '.join(made)}"] if made else []) + (
            [f"{' '.join(waiting)} still waiting for drive"] if waiting else [])
        return "alert: " + "; ".join(parts)

    def reload_request(self) -> str:
        req = self.ov / "requests"
        if any(req.glob("reload-watchdog-*")):
            return "alert: a watchdog reload request is still waiting for drive"
        req.mkdir(parents=True, exist_ok=True)
        (req / f"reload-watchdog-{int(self.now())}").write_text("watchdog: starved\n", encoding="utf-8")
        return "alert: reload requested"

    def stale_lock(self) -> str:
        lock = self.ov / "lock"
        try:
            import land  # noqa: PLC0415
            stale = lock.exists() and land.lock_is_stale(str(lock))
        except Exception:  # noqa: BLE001
            stale = False
        if stale:
            lock.unlink(missing_ok=True)
            return "removed a stale .overhaul/lock (holder dead)"
        return "alert"

    def prune(self, floor: float) -> str:
        root, now = self.ov / "target", self.now()
        dirs = [d for pat in INCREMENTAL for d in root.glob(pat) if d.is_dir()]
        dirs = sorted((d for d in dirs if now - _mtime(d) >= QUIET_S), key=_mtime)
        n = 0
        for d in dirs:
            if self.free_gb() >= floor:
                break
            shutil.rmtree(d, ignore_errors=True)
            n += 1
        free = self.free_gb()
        return (f"pruned {n} incremental dirs, free {free:.1f} GB" if free >= floor
                else f"alert: pruned {n} incremental dirs, still {free:.1f} GB free")

    def hold(self, hosts: list[str]) -> str:
        for h in hosts:
            path = remote.hold_path(self.repo, h)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"until": self.now() + HOLD_S, "why": "offline twice in 30 min"}),
                            encoding="utf-8")
        return f"alert: held offline {HOLD_S / 60:.0f} min: {' '.join(hosts)}"

    def redash(self) -> str:
        page = self.repo / "dash" / "index.html"
        before = _mtime(page)
        try:
            subprocess.run([sys.executable, str(HERE / "dash.py"), "--repo", str(self.repo)], cwd=self.repo,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as e:
            return f"alert: dash.py failed: {type(e).__name__}"
        return "dashboard rewritten" if _mtime(page) > before else "alert: dash.py ran and wrote nothing"

    # ---- output ----
    def event(self, detail: str) -> None:
        stamp = datetime.fromtimestamp(self.now(), timezone.utc).astimezone().isoformat(timespec="seconds")
        line = json.dumps({"t": stamp, "fn": None, "event": "watchdog", "detail": detail, "seconds": 0.0,
                           "tokens": None})
        self.ov.mkdir(parents=True, exist_ok=True)
        with open(self.ov / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def alert(self, line: str) -> None:
        stamp = datetime.fromtimestamp(self.now()).isoformat(timespec="seconds")
        with open(self.ov / "ALERT.txt", "a", encoding="utf-8") as f:
            f.write(f"{stamp} {line}\n")


def start(repo: Path, log=None, every: float = EVERY_S) -> threading.Event:
    """supervise.py: check every `every` s in a daemon thread until the returned event is set."""
    stop, dog = threading.Event(), Watchdog(repo)

    def loop():
        while not stop.wait(every):
            try:
                fired = dog.check()
                if fired and log:
                    log(f"watchdog: {' '.join(fired)} (see .overhaul/log.jsonl)")
            except Exception as e:  # noqa: BLE001 - never kill the supervisor from the side thread
                if log:
                    log(f"watchdog: error: {type(e).__name__}: {e}")
    threading.Thread(target=loop, daemon=True, name="watchdog").start()
    return stop


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--every", type=float, default=EVERY_S)
    a = ap.parse_args(argv)
    dog = Watchdog(Path(os.path.abspath(a.repo)))
    while True:
        fired = dog.check()
        print(f"watchdog: {' '.join(fired) or 'ok'}", flush=True)
        if a.once:
            return 0
        time.sleep(a.every)


if __name__ == "__main__":
    sys.exit(main())
