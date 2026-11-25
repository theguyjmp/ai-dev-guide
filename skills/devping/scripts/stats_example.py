"""Reference devping stats adapter: prints devping.py's card JSON for a repo planned in markdown tables.

Reads only git at the `--main` ref (what landed, not a working tree). The inputs and counting rules are in the skill's
SKILL.md, section "Stats adapter (--stats-cmd)".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

RATE_H = 3
STALE_VERIFIED_S = 2 * 3600
MOVING_S = 30 * 60
ONCE_MIN = 10 ** 7
TAG_RE = r"\[([A-Za-z][\w.-]*\d[\w.-]*)\]"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class ReadError(Exception):
    """A source the card cannot be right without is unreadable."""


# ---------------------------------------------------------------- counting (pure)
def clean_id(cell: str) -> str:
    return cell.strip().strip("`*[] ")


def table_rows(text: str) -> list[dict[str, str]]:
    """Every row of every markdown table, as {lower-case header: cell}."""
    rows, head = [], None
    for line in text.lstrip("﻿").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            head = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if head is None:
            head = [c.lower() for c in cells]
        elif not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
            rows.append(dict(zip(head, cells)))
    return rows


def plan_index(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """{id: row} of the rows with an id and a status; a repeated id counts once (its first row)."""
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        rid = clean_id(r.get("id", ""))
        if rid and "status" in r:
            out.setdefault(rid, r)
    return out


@dataclass(frozen=True)
class Total:
    planned: int
    roadmap_open: int
    track: int

    @property
    def whole(self) -> int:
        return self.planned + self.roadmap_open + self.track


def moved_marker(plan_file: str) -> re.Pattern:
    """A roadmap cell saying its row moved into the plan: "moved to FUNCTIONS" for spec/FUNCTIONS.md."""
    stem = plan_file.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return re.compile(r"\bmoved to %s\b" % re.escape(stem), re.I)


def platform_total(plan: dict, roadmap_rows: list[dict[str, str]], moved: re.Pattern) -> Total:
    """The whole platform, each row once: the plan, the roadmap rows not in it yet, and the separate track."""
    open_ids, track_ids = set(), set()
    for r in roadmap_rows:
        rid = clean_id(r.get("id", ""))
        if not rid or rid in plan:
            continue
        if "track" in r:
            track_ids.add(rid)
        elif not any(moved.search(c) for c in r.values()):
            open_ids.add(rid)
    return Total(len(plan), len(open_ids - track_ids), len(track_ids))


def split_ids(cell: str) -> list[str]:
    return [i for i in (clean_id(x) for x in re.split(r"[,;\s]+", cell or "")) if i and i not in ("-", "none")]


def work_units(plan: dict) -> dict[str, frozenset[str]]:
    """{unit: the ids whose landing closes it}: each plan row closes itself; an older id a row absorbs (its
    `replaces` cell) is closed by its own landing or by any row absorbing it, so a re-plan keeps landed work."""
    units = {rid: {rid} for rid in plan}
    for rid, row in plan.items():
        for old in split_ids(row.get("replaces", "")):
            units.setdefault(old, {old}).add(rid)
    return {u: frozenset(c) for u, c in units.items()}


def units_done(units: dict[str, frozenset[str]], done: set[str]) -> int:
    return sum(1 for closers in units.values() if closers & done)


def landings(log: str, tag_re: str = TAG_RE) -> tuple[set[str], tuple[float, ...]]:
    """(ids tagged on main, time of every tagged push, any plan) from `git log --format='%ct %s'` lines."""
    ids, times = set(), []
    for line in log.splitlines():
        ts, _, subject = line.partition(" ")
        tags = re.findall(tag_re, subject)
        if tags and ts.isdigit():
            ids.update(tags)
            times.append(float(ts))
    return ids, tuple(sorted(times))


def gate_count(text: str) -> int:
    """Problems in a shrink-only gate list: the sum of its `path count` lines."""
    n = 0
    for line in text.splitlines():
        s = line.strip()
        count = s.rpartition(" ")[2]
        if s and not s.startswith("#") and count.isdigit():
            n += int(count)
    return n


# ---------------------------------------------------------------- snapshot
@dataclass(frozen=True)
class Snapshot:
    now: float
    total: Total
    proven: int
    units_total: int
    units_done: int
    pushes: tuple[float, ...]
    main_ts: float | None                 # None: the probe failed (unknown)
    verified_ref: str = ""                # "": the project has no verified ref
    verified_ts: float | None = None
    unverified: int | None = None         # commits on main past verified
    oldest_unverified_ts: float | None = None
    problems_base: int | None = None      # the plan's day-one count
    problems_left: int | None = None      # None: the gate list is not on main yet (not measured)

    def since(self, hours: float) -> int:
        return sum(1 for t in self.pushes if t >= self.now - hours * 3600)


def git(repo: str, *args: str) -> str | None:
    """stdout, or None when git fails: a failed probe is unknown, never empty."""
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def last_move(repo: str, ref: str) -> float | None:
    out = git(repo, "log", "-1", "--format=%ct", ref)
    return float(out) if out and out.strip().isdigit() else None


def read(a: argparse.Namespace, now: float | None = None) -> Snapshot:
    def show(path: str) -> str | None:
        return git(a.repo, "show", "%s:%s" % (a.main, path))

    text = show(a.functions)
    if text is None:
        raise ReadError("cannot read %s on %s" % (a.functions, a.main))
    roadmap = show(a.roadmap) if a.roadmap else ""
    if roadmap is None:
        raise ReadError("cannot read %s on %s" % (a.roadmap, a.main))
    log = git(a.repo, "log", "--first-parent", "--format=%ct %s", a.main)
    if log is None:
        raise ReadError("cannot read the history of %s" % a.main)
    plan = plan_index(table_rows(text))
    done_status = {x.strip().lower() for x in a.done_status.split(",")}
    proven = {rid for rid, r in plan.items() if clean_id(r.get("status", "")).lower() in done_status}
    tagged, pushes = landings(log, a.tag_re)
    units = work_units(plan)
    pending = git(a.repo, "log", "--first-parent", "--format=%ct", "%s..%s" % (a.verified, a.main)) \
        if a.verified else None
    pending_ts = [float(x) for x in pending.split()] if pending is not None else None
    gate = show(a.problems) if a.problems else None
    return Snapshot(
        now=now or time.time(), total=platform_total(plan, table_rows(roadmap), moved_marker(a.functions)), proven=len(proven),
        units_total=len(units), units_done=units_done(units, tagged | proven), pushes=pushes,
        main_ts=last_move(a.repo, a.main), verified_ref=a.verified,
        verified_ts=last_move(a.repo, a.verified) if a.verified else None,
        unverified=len(pending_ts) if pending_ts is not None else None,
        oldest_unverified_ts=min(pending_ts) if pending_ts else None,
        problems_base=a.problems_baseline, problems_left=gate_count(gate) if gate is not None else None)


# ---------------------------------------------------------------- verdicts and card
def health(s: Snapshot) -> str:
    """Amber when a probe is unknown or verified has trailed main too long; red only when reading fails."""
    if s.main_ts is None or (s.verified_ref and (s.verified_ts is None or s.unverified is None)):
        return "amber"
    if s.unverified and s.oldest_unverified_ts and s.now - s.oldest_unverified_ts > STALE_VERIFIED_S:
        return "amber"
    return "green"


def quiet_ok(s: Snapshot) -> bool:
    """The chat may sit quiet only while main measurably moves, never on red or on an unknown probe."""
    return health(s) == "green" and s.main_ts is not None and s.now - s.main_ts < MOVING_S


def headline(s: Snapshot) -> str:
    """Facts only: a relative time here would edit the card every tick."""
    if s.main_ts is None:
        return "main unknown: git probe failed"
    if not s.verified_ref:
        return "%d of %d work units done" % (s.units_done, s.units_total)
    if s.verified_ts is None or s.unverified is None:
        return "verified unknown: git probe failed"
    return "verified is level with main" if not s.unverified else "%d commits on main wait for verified" % s.unverified


def problems_text(s: Snapshot) -> str:
    if s.problems_left is None:
        return "%s (day-one baseline, not measured yet)" % format(s.problems_base, ",")
    if s.problems_base is None:
        return "%s left" % format(s.problems_left, ",")
    return "%s left of %s" % (format(s.problems_left, ","), format(s.problems_base, ","))


def card(s: Snapshot) -> dict:
    t = s.total
    fields = [
        ("Total", "**%d of %d** rows proven (plan %d, roadmap not planned yet %d, track %d)"
         % (s.proven, t.whole, t.planned, t.roadmap_open, t.track)),
        ("Plan", "**%d of %d** work units done" % (s.units_done, s.units_total)),
        ("Rate", "**%.1f** pushes per hour (last %d h), %d in the last hour" % (s.since(RATE_H) / RATE_H, RATE_H,
                                                                                  s.since(1))),
        ("main", "<t:%d:R>" % s.main_ts if s.main_ts is not None else "unknown"),
    ]
    if s.verified_ref:
        fields.append(("verified", "unknown" if s.verified_ts is None or s.unverified is None
                       else "<t:%d:R>, %d commits waiting" % (s.verified_ts, s.unverified)))
    if s.problems_base is not None or s.problems_left is not None:
        fields.append(("Problems", problems_text(s)))
    alerts = []
    if s.units_total and s.units_done >= s.units_total:
        alerts.append({"key": "plan-done", "text": "every work unit of the plan is done (%d)." % s.units_total,
                       "cooldown_min": ONCE_MIN})
    return {"health": health(s), "headline": headline(s),
            "progress": [s.proven, t.whole] if t.whole else None,
            "fields": [{"name": k, "value": v, "inline": True} for k, v in fields],
            "links": [], "alerts": alerts, "quiet_ok": quiet_ok(s)}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--main", default="main")
    ap.add_argument("--verified", default="", help="the verified ref, when the project has one")
    ap.add_argument("--functions", default="spec/FUNCTIONS.md")
    ap.add_argument("--roadmap", default="", help="e.g. spec/ROADMAP.md")
    ap.add_argument("--done-status", default="proven", help="comma-separated statuses that count as proven")
    ap.add_argument("--tag-re", default=TAG_RE, help="regex with one group: the id a commit subject tags")
    ap.add_argument("--problems", default="", help="a gate list of `path count` lines, path in the repo")
    ap.add_argument("--problems-baseline", type=int, default=None, help="the plan's day-one problem count")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> dict:
    try:
        return card(read(parse_args(argv)))
    except ReadError as e:
        return {"health": "red", "headline": "stats reader failed: %s" % e, "alerts": []}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(main(), ensure_ascii=False))
