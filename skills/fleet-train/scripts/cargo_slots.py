"""Machine-wide cap on cargo-building agents, shared by every fleet on this PC.
Holders {pid, start, fleet, tag, t} live in FLEET_CARGO_SLOTS (~/.claude/fleet-cargo-slots.json); a holder whose process
(pid + start) is gone is dropped on read. Cap FLEET_CARGO_CAP (3). Writes run under an O_EXCL lock file."""
from __future__ import annotations

import json, os, time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

DEFAULT_PATH = Path.home() / ".claude" / "fleet-cargo-slots.json"
DEFAULT_CAP = 3
LOCK_STALE = 120
REPLACE_WAIT = 5


class Full(Exception):
    pass


class CargoSlots:
    def __init__(self, alive: Callable[[int, str], bool], path: Path | None = None, cap: int | None = None):
        self.alive = alive
        self.path = Path(path or os.environ.get("FLEET_CARGO_SLOTS") or DEFAULT_PATH)
        self.cap = int(cap if cap is not None else os.environ.get("FLEET_CARGO_CAP") or DEFAULT_CAP)

    def holders(self) -> list[dict]:
        return [x for x in self._read() if self.alive(x["pid"], x.get("start", ""))]

    def claim(self, fleet: str, tag: str, spawn: Callable[[], tuple[int, str]]) -> tuple[int, str]:
        """Spawn and record a holder under the lock; Full at the cap."""
        with self._locked():
            live = self.holders()
            if len(live) >= self.cap:
                raise Full(f"{len(live)}/{self.cap} machine-wide cargo slots held ("
                           + ", ".join(f"{x['fleet']}:{x['tag']}" for x in live) + ")")
            pid, start = spawn()
            live.append({"pid": pid, "start": start, "fleet": fleet, "tag": tag, "t": round(time.time())})
            self._write(live)
            return pid, start

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        if not isinstance(data, list) or not all(isinstance(x, dict) and isinstance(x.get("pid"), int) for x in data):
            raise ValueError(f"{self.path}: not a list of {{pid, start, fleet}} holders")
        return data

    def _write(self, holders: list[dict]) -> None:
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(holders, indent=1), encoding="utf-8")
        t0 = time.time()
        while True:  # Windows: a reader holds the file open
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if time.time() - t0 > REPLACE_WAIT:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(0.1)

    @contextmanager
    def _locked(self, wait: float = 30):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lk, t0 = self.path.with_name(self.path.name + ".lock"), time.time()
        while True:
            try:
                os.close(os.open(str(lk), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except FileExistsError:
                try:
                    if time.time() - lk.stat().st_mtime > LOCK_STALE:
                        lk.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() - t0 > wait:
                    raise TimeoutError(str(lk))
                time.sleep(0.2)
        try:
            yield
        finally:
            lk.unlink(missing_ok=True)
