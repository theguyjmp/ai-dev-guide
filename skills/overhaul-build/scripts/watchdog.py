#!/usr/bin/env python3
"""watchdog.py: invariants of a running overhaul build, checked every 5 minutes; each breach acts or alerts at once.

    py -3 watchdog.py --repo R [--once] [--every 300]

supervise.py runs it as a thread (start()); by hand, --once checks once. Reads .overhaul/log.jsonl (its tail) and
.overhaul/state.json, never git. Each breach writes a `watchdog` event to log.jsonl and acts:
- starved: ready rows beside free builder slots (state.json `width`) for 10 min with no build_start -> a reload
  request (.overhaul/requests/reload-watchdog-<time>, which drive applies at its next cycle) and an alert;
- no_land: rows in `landing` and no land_ok for 45 min -> a stale .overhaul/lock (dead holder) is removed, else alert;
- disk: free space on the repo's drive under [limits] prune_at_gb (default 80) -> cargo incremental dirs under
  .overhaul/target untouched for 15 min are deleted, oldest first, until above it; alert when still under;
- stop: a `stop` event since the last check -> alert with its line;
- host_offline: one remote host offline twice in 30 min -> held offline for 30 min
  (.overhaul/remote/<name>.offline, read by remote.py) and alert.
An alert is one line appended to .overhaul/ALERT.txt for the manager. A breach that persists repeats its event and
alert at most every 30 min. Its memory (first seen, last fired, last check) is .overhaul/watchdog.json.

Why (pilot2026-11-09/10): 17 gaps of over 45 min between lands, 5 whole-build stops and 5 desk hangs in 13 minutes
were each found by the manager's 30-minute heartbeat or by the owner, not when they happened.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
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
TAIL_BYTES = 4 << 20
INCREMENTAL = ("*/debug/incremental/*", "*/release/incremental/*")


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


class Watchdog:
    def __init__(self, repo: Path, free_gb=None, now=None):
        self.repo, self.ov = repo, repo / ".overhaul"
        self._free_gb, self._now = free_gb, now
        self.mem_path = self.ov / "watchdog.json"

    def now(self) -> float:
        return self._now() if self._now else time.time()

    def free_gb(self) -> float:
        if self._free_gb:
            return self._free_gb()
        try:
            return shutil.disk_usage(self.repo).free / 1e9
        except OSError:
            return float("inf")

    def limits(self) -> dict:
        try:
            conf = tomllib.loads((self.repo / "overhaul.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            conf = {}
        return conf.get("limits") or {}

    # ---- one check ----
    def check(self) -> list[str]:
        """Every invariant once; returns the breaches that fired (their names)."""
        now, mem = self.now(), read_json(self.mem_path)
        evs, st = events(self.ov), read_json(self.ov / "state.json")
        since = float(mem.get("last_check") or now - EVERY_S)
        fired = []
        for name, breach in (("starved", self.starved(now, mem, evs, st)), ("no_land", self.no_land(now, evs, st)),
                             ("disk", self.disk()), ("stop", self.stopped(evs, since)),
                             ("host_offline", self.host_offline(now, evs))):
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

    def starved(self, now, mem, evs, st):
        width = st.get("width") or {}
        ready, free = int(width.get("ready") or 0), int(width.get("free") or 0)
        first = mem.setdefault("first", {})
        if not (ready and free):
            first.pop("starved", None)
            return None
        first.setdefault("starved", now)
        last_start = max((e["ts"] for e in evs if e["event"] == "build_start"), default=0.0)
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
        last = max((e["ts"] for e in evs if e["event"] == "land_ok"), default=0.0)
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

    # ---- actions ----
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


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


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
