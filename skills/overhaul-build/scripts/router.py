#!/usr/bin/env python3
"""router.py: why attempts fail, and each cause routed to its fix while drive.py runs.

Every failed attempt (a fail or a park in drive.py: needs_plan, land_fail, review_fail, builder and land timeouts,
failed dependency fixes) gets ONE class from its reason and, when the reason is thin ("builder left no committed
change", "acceptance red ..."), from the builder's own output (its BLOCKED-BY / contract-defect text):

- files_gap        the builder cannot edit a file it needs: outside its files cell, a visibility refusal, a review
                   PLAN-GAP naming another file, a BLOCKED-BY or "no change" whose output names a file outside its cell
- missing_dep      blocked by a stub or by behaviour no proven row provides yet
- missing_capability  a BLOCKED-BY whose line says a capability is missing, not a wrong result ("can only",
                   "cannot", "no way to", "does not support", "no formula kind", "that adds a new record")
- contract_defect  the builder reports the acceptance test or the row itself is wrong
- main_flake       the red test fails on clean main too
- load_flake       the red test passes when run alone
- oversized        a builder, land or acceptance timeout, or a 1M fixture red (a regression of a proven test is a retry)
- review_quality   verified blocking NOs inside its own cell
- other            anything else (a regression the branch caused, a protected file touched, ...)

Routes, at once (not at Exit 3):
- files_gap for files of the row's own module or a module it depends on (ARCHITECTURE `Depends on:`): the claim
  widens (state `files_extra`, used by drive's claims() and allowed paths; event `files_widened`) and the build resumes
  on its branch at once, no attempt charged, once per file. Files of any other module go on to a plan fix.
- missing_capability: never a depfix; a plan fix adds a row for the owning module before this one. A second BLOCKED-BY
  (its one depfix spent) is routed the same way (missing_dep when it names no capability), never "no committed change".
- files_gap / missing_dep / missing_capability / contract_defect / oversized: the function is parked needs-plan
  with the class and the evidence in its reason, and .overhaul/router/routes/<id>.json asks for a plan fix.
  supervise.py, while drive runs (`with router.watching(sup)`), starts plan-fix agents for the routed functions (template plan-fix-brief.md, the class
  and evidence in its mode line; oversized means split), one route per function at a time, at most
  [harness] plan_fix_parallel at once, then the integrator (plan-fix-merge.md) when more than one fixed, lands the plan
  under .overhaul/lock and writes .overhaul/requests/reset-<id> (live-replan's request file; drive applies it at its
  next cycle boundary once that branch is on main; until then supervise applies it with drive --reset before an
  Exit-3 round, so the fixed function is never planned twice). With live-replan's live rounds present, they take the
  needs-plan function and its reason carries the class: the router's own round steps aside.
- main_flake: drive.py's main-flake fix packet goes to the test's owner module (depfix-flake-<id>), and the lands of
  queued rows whose footprint reruns that test (same module, or a changed file in the test's module or the test file)
  are held until that fix settles.
- load_flake: no attempt charged; a land goes back on the land queue, a build back to planned (at most
  FREE_REQUEUES per function, then an ordinary attempt).
- review_quality / other: the normal retry.

Each one logs a `routed` event (`class=<c> -> <route>: <evidence>`). Holds and releases log `route_hold` (not a failure). failures() counts the last 60 min per class for
verdict.py and the dashboard; one class over 30 % of at least MIN_FAILS failures is the manager's bottleneck signal.

Why (measured 2026-11-09 on the pilot project): 7 lands since 8 AM (0.85/h) against a 10/h floor; 32 failed attempts since
12:34: 8 review failed, 7 acceptance red after merging main (several really files-cell gaps or oversized 1M rows),
6 timing/flaky tests on main, 3 builder made no change, 2 outside files cell, 2 builder timed out, 1 contract defect,
1 plan gap. Nothing classified why; the systemic detector needs one normalized reason 3 times and never fired; the
manager classified by hand, hours late.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import sys
import threading
from pathlib import Path

CLASSES = ("files_gap", "missing_dep", "missing_capability", "contract_defect", "main_flake", "load_flake", "oversized",
           "review_quality", "other")
PLAN_CLASSES = ("files_gap", "missing_dep", "missing_capability", "contract_defect", "oversized")
WORDS = {
    "files_gap": "builders cannot edit a needed file",
    "missing_dep": "builders are blocked by a stub or unproven dependency",
    "missing_capability": "builders need a capability no row provides",
    "contract_defect": "builders report the acceptance test is wrong",
    "main_flake": "tests fail on clean main too",
    "load_flake": "tests fail under load and pass alone",
    "oversized": "rows are too big (timeouts, 1M fixtures)",
    "review_quality": "reviews find real defects in the builders' own code",
    "other": "unclassified failures",
}
FIX = {
    "files_gap": "plan fix: add the needed file to the row's files cell (router routes each one at once)",
    "missing_dep": "plan fix: add the dependency row or prove it first",
    "missing_capability": "plan fix: add a row for the owning module that builds the capability, before this one",
    "contract_defect": "plan fix: correct the acceptance test or the row",
    "main_flake": "fix the flaky test in its owner module (router holds the lands that rerun it)",
    "load_flake": "lower load in the land gate ([harness] max_lands, builders) or move the test to a tier",
    "oversized": "plan fix: split the row (smaller fixture, fewer behaviours per row)",
    "review_quality": "tighten the build brief or the module's rules: builders ship what review rejects",
    "other": "read the routed events in log.jsonl and name the cause",
}
HINT = {"files_gap": "add the needed file to the files cell", "missing_dep": "add or prove the dependency first",
        "missing_capability": "add a new row (a reserved id) for the owning module that builds the missing capability "
                              "through its public entry with its own failing acceptance test, and make this row "
                              "depend on it; this row is not split and gets no depfix",
        "contract_defect": "correct the test or the row", "oversized": "split this row"}
WINDOW_MIN = 60
RED_SHARE = 0.30
MIN_FAILS = 3
FREE_REQUEUES = 2
GROUP_S = 120  # raw log events of one function this close together are one failed attempt
ROUTE_POLL_S = 30.0
LOCK_WAIT_S = 1800.0
REQUESTS_DIR = "requests"  # live-replan's .overhaul/requests/reset-<id>
FAIL_EVENTS = ("needs_plan", "land_fail", "review_fail", "blocked_by", "main_flake")
PRIORITY = ("main_flake", "load_flake", "contract_defect", "files_gap", "oversized", "missing_capability",
            "missing_dep",
            "review_quality", "other")

LOAD = re.compile(r"passe[sd] alone|flaky under load|fails? under load", re.I)
MAIN = re.compile(r"main flake|fails on clean \S+ too|on clean main|depfix-flake-", re.I)
CONTRACT = re.compile(r"contract defect|^contract |test is wrong|wrong test|BLOCKED-BY:\s*`?(?:tests/acceptance|spec/)",
                      re.I | re.M)
FILES = re.compile(r"outside (?:its|the|my) (?:files? cell|allowed (?:paths|files))|beyond visibility|visibility only|"
                   r"PLAN-GAP|plan gap|files (?:column|cell) (?:needs|must|lacks|is missing)|not in (?:its|my) files",
                   re.I)
TIMEOUT = re.compile(r"(?<!reviewer )(?<!verifier )(?<!auditor )\btimed out\b|\btimeout model=|land\.py timed out", re.I)
ONE_M = re.compile(r"(?:^|[^0-9a-z.])1m(?:[^0-9a-z]|$)|1_000_000|\b1M\b", re.I)
NODE = re.compile(r"\S*tests/\S+")
# a proven test this change broke (land.py, drive.py pre-review): a normal retry, whatever its fixture size
REGRESSION = re.compile(r"\bregression(?: before review)?: proven ")
DEP = re.compile(r"BLOCKED-BY|\bstub\b|todo!\(|unimplemented|not (?:yet )?proven|unproven|dependency fix \S+ failed|"
                 r"can(?:'|no)t build|has no \w+ kind|no \w+ kind", re.I)
REVIEW = re.compile(r"verified blocking NO", re.I)
THIN = re.compile(r"no committed change|acceptance (?:still )?red|builder exited|BLOCKED-BY", re.I)
BLOCKED = re.compile(r"BLOCKED-BY:\s*`?([^\s`:]+)")
BLOCKED_LINE = re.compile(r"^\s*BLOCKED-BY:.*$", re.M)
# a BLOCKED-BY line naming a capability its owner lacks (not a wrong result); measured on the pilot project's 55 BLOCKED-BY
# builds 2026-11-09: 11 hits (R0801 "can only", R0903 "no formula kind", R1002 "that adds a new", ...), no wrong result
CAPABILITY = re.compile(
    r"\b(?:only can|can only|cannot|can ?not|can't|no way to|does(?: not|n't) support|not supported|"
    r"has no (?:way|api|function|method|support|public)|(?:is|are) missing|missing (?:a |an |the )?"
    r"(?:capability|api|function|method|verb|op|operation|feature|support|kind|variant|write path)\b|"
    r"needs? (?:a |an )?new|is needed|that adds? (?:a )?new|no \w+ (?:kind|variant|op|operation|api|verb)\b)", re.I)
STUB = re.compile(r"todo!\(|unimplemented|\bstub\b|not yet implemented|not (?:yet )?proven|unproven", re.I)
VIS_FILES = re.compile(r"^changed (.+?) beyond visibility")
PATH = re.compile(r"(?<![\w./-])((?:[\w.-]+/)+[\w-]+\.\w+)")
DEPFIX = re.compile(r"^depfix-(?:flake-)?")
# infeasible: feasibility.py proved the row unbuildable BEFORE any build (a MISSING capability, a provider row neither
# proven nor a dep, a file outside its module and its deps, or too big for one builder); parked with its evidence
CLASSES += ("infeasible",)
PLAN_CLASSES += ("infeasible",)
PRIORITY += ("infeasible",)
WORDS["infeasible"] = "the feasibility check found rows unbuildable before a build"
FIX["infeasible"] = "plan fix: add the missing capability's row first, add the module dep, or split the row"
HINT["infeasible"] = ("add a row for each MISSING capability in its owning module before this one, add the module "
                      "dep a needed file belongs to, or split the row if it does not fit one builder")


# ---- classification --------------------------------------------------------------------------------------------------
def classify(detail: str, builder: str = "", cell: tuple[str, ...] | list[str] = ()) -> str:
    """The class of one failed attempt from its reason (detail) and, for a thin reason, the builder's output."""
    d = detail or ""
    if LOAD.search(d):
        return "load_flake"
    if MAIN.search(d):
        return "main_flake"
    if d.startswith("review FAIL"):  # the reviewer's NOs are about the builder's diff: a retry, not a plan fix
        return "review_quality"
    if CONTRACT.search(d):
        return "contract_defect"
    if FILES.search(d):
        return "files_gap"
    if TIMEOUT.search(d) or (not REGRESSION.search(d) and any(ONE_M.search(n) for n in NODE.findall(d))):
        return "oversized"
    if DEP.search(d):
        return blocked_class(d, cell)
    if builder and THIN.search(d):
        b = builder
        if CONTRACT.search(b):
            return "contract_defect"
        if FILES.search(b):
            return "files_gap"
        if BLOCKED.search(b) or DEP.search(b):
            return blocked_class(b, cell)
    if REVIEW.search(d):
        return "review_quality"
    return "other"


def blocked_class(text: str, cell) -> str:
    """A BLOCKED-BY naming a file outside the function's cell that its text says it must edit is a files gap."""
    m = BLOCKED.search(text)
    if m and cell and not any(m.group(1).startswith(c.rstrip("*")) for c in cell) and re.search(
            r"\b(?:my|its) (?:allowed )?files|files (?:column|cell)|must (?:edit|change)", text, re.I):
        return "files_gap"
    line = BLOCKED_LINE.search(text)
    return "missing_capability" if line and capability(line.group(0)) else "missing_dep"


def capability(text: str) -> bool:
    """Whether a BLOCKED-BY text names a capability its owner lacks; a stub or unproven row is a wait (missing_dep)."""
    return bool(CAPABILITY.search(text)) and not STUB.search(text)


def builder_text(ov: Path, fid: str) -> str:
    """The latest builder's final text for fid (runs/<fid>-a<n>-build.json result), or ''."""
    runs = sorted((ov / "runs").glob(f"{fid}-a*-build.json"), key=lambda p: p.stat().st_mtime)
    for p in reversed(runs):
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for chunk in (raw, *reversed(raw.splitlines())):  # one json result, or a stream-json run's last result event
            try:
                doc = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(doc, dict) and doc.get("type") in (None, "result") and isinstance(doc.get("result"), str):
                return doc["result"][-4000:]
    return ""


# ---- the rolling window ----------------------------------------------------------------------------------------------
def parse_t(s) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def failures(events: list[dict], t_now: dt.datetime, window_min: float = WINDOW_MIN, builder=None) -> dict:
    """Class counts of the failed attempts in the last window_min minutes. `routed` events are the record when drive
    ran with the router; an older log is classified from its raw failure events, one per function per GROUP_S
    (builder: fid -> the builder's last output, read for a thin reason)."""
    since = t_now - dt.timedelta(minutes=window_min)
    recent = [(t, e) for e in events if isinstance(e, dict) and (t := parse_t(e.get("t"))) and since <= t <= t_now]
    routed = [e for _, e in recent if e.get("event") == "routed"]
    classes: list[str] = []
    if routed:
        for e in routed:
            m = re.match(r"class=(\w+)", str(e.get("detail") or ""))
            classes.append(m.group(1) if m and m.group(1) in CLASSES else "other")
    else:
        last: dict[str, tuple[dt.datetime, int]] = {}
        for t, e in recent:
            ev, d = e.get("event"), str(e.get("detail") or "")
            if ev == "build_end" and TIMEOUT.search(d):
                pass
            elif ev == "dep_fix" and d.startswith("failed"):
                pass
            elif ev not in FAIL_EVENTS:
                continue
            fn = DEPFIX.sub("", str(e.get("fn") or "?"))
            if ev == "blocked_by" and not d.startswith("contract "):
                continue  # a BLOCKED-BY that got a dependency fix is a wait, not a failed attempt
            if ev == "main_flake" or str(e.get("fn")).startswith("depfix-flake-"):
                cls = "main_flake"
            else:
                cls = classify(d, builder(fn) if builder and THIN.search(d) else "")
            prev = last.get(fn)
            if prev and (t - prev[0]).total_seconds() <= GROUP_S:
                i = prev[1]
                if PRIORITY.index(cls) < PRIORITY.index(classes[i]):
                    classes[i] = cls
                last[fn] = (t, i)
                continue
            last[fn] = (t, len(classes))
            classes.append(cls)
    counts = {c: classes.count(c) for c in CLASSES if classes.count(c)}
    total = len(classes)
    top = max(counts, key=lambda c: (counts[c], -CLASSES.index(c))) if counts else ""
    red = bool(top) and total >= MIN_FAILS and counts[top] > RED_SHARE * total
    line = (f"failures {window_min:g}m: {total}" + (" (" + ", ".join(f"{c} {n}" for c, n in sorted(
        counts.items(), key=lambda kv: -kv[1])) + f"); top {top}" if total else ""))
    return {"total": total, "counts": counts, "top": top, "top_n": counts.get(top, 0), "red": red, "line": line,
            "words": f"most failures: {WORDS[top]} ({counts[top]} of {total})" if top else "",
            "action": FIX.get(top, "")}


# ---- drive.py hooks --------------------------------------------------------------------------------------------------
def ov_of(d) -> Path:
    return Path(d.ov)


def state_path(ov: Path) -> Path:
    return ov / "router" / "router.json"


def load(ov: Path) -> dict:
    try:
        doc = json.loads(state_path(ov).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("window", [])
    doc.setdefault("paused", {})
    doc.setdefault("held", [])
    return doc


def save(ov: Path, doc: dict) -> None:
    path = state_path(ov)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def record(d, fid: str, cls: str, route: str, evidence: str) -> None:
    """One `routed` event in log.jsonl and the rolling window (with counts) in .overhaul/router/router.json."""
    ov = ov_of(d)
    now = dt.datetime.now(dt.timezone.utc)
    d.log(fid, "routed", f"class={cls} -> {route}: {evidence}"[:600])
    doc = load(ov)
    cut = now - dt.timedelta(minutes=WINDOW_MIN)
    doc["window"] = [w for w in doc["window"] if (parse_t(w.get("t")) or now) >= cut]
    doc["window"].append({"t": now.isoformat(timespec="seconds"), "fn": fid, "class": cls, "route": route})
    doc["counts"] = {c: n for c in CLASSES if (n := sum(w.get("class") == c for w in doc["window"]))}
    save(ov, doc)


def cell_of(d, fid: str) -> list[str]:
    try:
        return list(d.claims(d.fns[fid])) if fid in d.fns else []
    except Exception:  # noqa: BLE001 - classification never breaks the drive loop
        return []


def route_file(ov: Path, fid: str) -> Path:
    return ov / "router" / "routes" / f"{fid}.json"


def want_plan_fix(d, fid: str, cls: str, evidence: str) -> str:
    """Ask supervise for a plan fix of fid (one route per function at a time). The route line for the event."""
    path = route_file(ov_of(d), fid)
    if path.is_file():
        return "plan fix already in flight"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": fid, "class": cls, "evidence": evidence[:1500],
                                "t": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}), encoding="utf-8")
    return "plan fix" + (" (split)" if cls == "oversized" else "")


def split_ok(oversized: bool, line1: str) -> bool:
    """An oversized function's plan fix counts only as a split (owner 2026-11-10): `PLAN-FIX <id> split PLAN OK`."""
    words = line1.split()
    return not oversized or (len(words) > 2 and words[2].lower() == "split")


def oversized_reason(reason: str) -> bool:
    """drive's park reason of a function the router classed oversized (`router oversized: ...`)."""
    return str(reason or "").startswith("router oversized")


def gap_files(reason: str, builder: str = "") -> list[str]:
    """The files a files_gap names: the visibility check's list, else every repo path in the reason or the
    builder's BLOCKED-BY line (a `path:line` loses its line)."""
    m = VIS_FILES.match(reason or "")
    if m:
        return [x.strip() for x in m.group(1).split(",") if x.strip()]
    line = BLOCKED_LINE.search(builder or "")
    return list(dict.fromkeys(PATH.findall(f"{reason} {line.group(0) if line else ''}")))


def add_files(d, fid: str, files: list[str], why: str = "") -> None:
    """The one writer of state `files_extra`: grow fid's claim by files (deduped, order kept) and log
    `files_widened`. Used by widen() (a files_gap) and feasibility.py (a file the check found the row needs)."""
    rec = d.st["functions"][fid]
    d.set(fid, files_extra=list(dict.fromkeys([*(rec.get("files_extra") or []), *files])))
    d.log(fid, "files_widened", (f"{', '.join(files)}: {why}" if why else ", ".join(files))[:600])


def widen(d, fid: str, files: list[str], evidence: str, deps: bool = True) -> bool:
    """A files_gap the row may fix itself: every named file outside its claim lies in its own module (or, with deps,
    a module its module depends on) and was never widened for it. The claim grows (state `files_extra`, read by
    drive's claims() and allowed paths), the build resumes on its branch at once, no attempt charged. Once per
    file: a file already in the claim is not new, so the same gap again goes to a plan fix."""
    f, rec = d.fns.get(fid), d.st["functions"].get(fid)
    if f is None or rec is None or not files:
        return False
    mod = sys.modules[type(d).__module__]
    have = [*d.claims(f), *(p for group in d.allowed_lists(f) for p in group)]
    new = [p for p in dict.fromkeys(files) if not mod.in_paths(p, have)]
    if not new or not all(mod.in_paths(p, d.widen_scope(f, deps)) for p in new):
        return False
    add_files(d, fid, new, evidence)
    d.set(fid, status="planned", resume="building",
          widened=f"its files cell now also holds {', '.join(new)}, the files it needed: {evidence[:1200]}")
    record(d, fid, "files_gap", f"claim widened to {', '.join(new)}; build resumes at once, no attempt charged",
           evidence)
    return True


def on_blocked(d, fid: str, path: str, evidence: str, spent: bool) -> bool:
    """drive.blocked_by hook for a builder's BLOCKED-BY line on `path` (evidence = `path:line in module: why`).
    A file of the row's own module outside its cell widens the claim (the builder adds what it lacks itself, once
    per file); a missing capability elsewhere goes to a plan fix (a new row for the owning module), never a depfix;
    a second BLOCKED-BY (spent: its one depfix used) is a plan fix by the same rule. False: drive's depfix route (a
    wrong result in another module) or an ordinary failure."""
    if widen(d, fid, [path], f"BLOCKED-BY {evidence}", deps=False):
        return True
    cls = "missing_capability" if capability(evidence) else ""
    if not cls and not spent:
        return False
    cls = cls or "missing_dep"
    reason = f"BLOCKED-BY {evidence}" + (" (again after its dependency fix)" if spent else "")
    record(d, fid, cls, want_plan_fix(d, fid, cls, reason), reason)
    d.park(fid, f"router {cls}: {WORDS[cls]}; {HINT[cls]}: {reason}")
    return True


def on_failure(d, fid: str, reason: str) -> bool:
    """drive.fail hook, before an attempt is charged. True when the router handled the failure."""
    try:
        cls = classify(reason, builder_text(ov_of(d), fid) if THIN.search(reason) else "", cell_of(d, fid))
    except Exception as e:  # noqa: BLE001
        d.say(f"router: classify {fid}: {type(e).__name__}: {e}")
        return False
    d._router_last = (fid, reason)
    rec = d.st["functions"].get(fid, {})
    if cls == "files_gap" and widen(d, fid, gap_files(reason, builder_text(ov_of(d), fid)), reason):
        return True
    if cls in PLAN_CLASSES and fid in d.fns:
        route = want_plan_fix(d, fid, cls, reason)
        record(d, fid, cls, route, reason)
        d.park(fid, f"router {cls}: {WORDS[cls]}; {HINT[cls]}: {reason}")
        return True
    if cls == "load_flake" and rec and int(rec.get("load_requeues", 0)) < FREE_REQUEUES:
        n = int(rec.get("load_requeues", 0)) + 1
        record(d, fid, cls, f"requeue, no attempt charged ({n} of {FREE_REQUEUES})", reason)
        if reason.startswith("land: ") and fid not in d.land_queue:
            d.set(fid, status="landing", load_requeues=n, last_reason=f"router load_flake: {reason}"[:1500])
            d.land_queue.append(fid)
        else:
            d.set(fid, status="planned", load_requeues=n, last_reason=f"router load_flake: {reason}"[:1500])
        return True
    record(d, fid, cls, "retry", reason)
    return False


def on_park(d, fid: str, reason: str) -> str:
    """drive.park hook: count and route a park no fail() routed already. Returns the reason to park with, or ''
    when the park is not needed (a files_gap the router widened: the build resumes at once)."""
    last = getattr(d, "_router_last", None)
    d._router_last = None
    if reason.startswith("router ") or (last and last == (fid, reason)):
        return reason
    try:
        cls = classify(reason, builder_text(ov_of(d), fid) if THIN.search(reason) else "", cell_of(d, fid))
    except Exception:  # noqa: BLE001
        return reason
    # mainred's dead-depfix "plan gap: <dep file>" is never widened: two depfixes died in that module, so the plan,
    # not this row's claim, is wrong (widening would let the row rewrite a dep's proven code)
    if cls == "files_gap" and not reason.startswith("plan gap: ") and widen(d, fid, gap_files(reason), reason):
        return ""
    if cls in PLAN_CLASSES and fid in d.fns:
        record(d, fid, cls, want_plan_fix(d, fid, cls, reason), reason)
        return f"router {cls}: {WORDS[cls]}; {HINT[cls]}: {reason}"
    record(d, fid, cls, "needs-plan", reason)
    return reason


def on_main_flake(d, fid: str, test: str, path: str, module: str, dfid: str) -> None:
    """drive.main_flake hook: count it and hold the lands that rerun `test` until its fix (dfid) settles."""
    record(d, fid, "main_flake", f"fix packet {dfid} to {module}; lands that rerun {test} held", f"{test} on clean main")
    ov = ov_of(d)
    doc = load(ov)
    doc["paused"][test] = {"path": path, "module": module, "dfid": dfid,
                           "t": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    save(ov, doc)


def on_depfix_settled(d, dfid: str, ok: bool) -> None:
    """drive.depfix_settled hook: a main-flake fix landed (green) or failed: its held lands go back on the queue."""
    ov = ov_of(d)
    doc = load(ov)
    gone = [t for t, p in doc["paused"].items() if p.get("dfid") == dfid]
    if not gone:
        return
    for t in gone:
        doc["paused"].pop(t)
        d.log(None, "route_hold", f"lands rerunning {t} released: {dfid} {'landed' if ok else 'failed'}")
    save(ov, doc)


def reruns(d, fid: str, pause: dict) -> bool:
    """Whether fid's land reruns the paused test: same module, or its footprint touches the test or its module."""
    mod = sys.modules[type(d).__module__]
    f = d.fns.get(fid)
    if f is None:
        return False
    if pause.get("module") and f.module == pause["module"]:
        return True
    try:
        foot = d.footprint(fid)
        paths = [pause["path"]] if pause.get("path") else []
        if pause.get("module") and hasattr(mod, "module_paths"):
            paths += mod.module_paths(mod.module_section(d.arch, pause["module"]), pause["module"])
        return bool(paths) and mod.paths_overlap(foot, paths)
    except Exception:  # noqa: BLE001 - never hold a land on an error
        return False


def hold_lands(d) -> None:
    """drive.pump_land hook: queued lands that rerun a paused main-flake test wait off the queue; back when released."""
    ov = ov_of(d)
    doc = load(ov)
    held = [fid for fid in doc["held"] if fid in d.st["functions"]]
    if not doc["paused"] and not held:
        return
    pauses = list(doc["paused"].values())
    back = [fid for fid in held if not any(reruns(d, fid, p) for p in pauses)]
    for fid in back:
        if fid not in d.land_queue and d.st["functions"][fid].get("status") == "landing":
            d.land_queue.append(fid)
    keep = [fid for fid in held if fid not in back]
    for fid in list(d.land_queue):
        if any(reruns(d, fid, p) for p in pauses):
            d.land_queue.remove(fid)
            keep.append(fid)
            d.log(fid, "route_hold", "land held: it reruns a test that fails on clean main until that test's fix settles")
    if keep != doc["held"]:
        doc["held"] = keep
        save(ov, doc)


# ---- supervise.py: plan-fix rounds for routed functions while drive runs ---------------------------------------------
def routes(ov: Path) -> list[dict]:
    out = []
    for p in sorted((ov / "router" / "routes").glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("id") and not doc.get("done"):
            out.append(doc)
    return out


class Watch:
    """While drive runs: every ROUTE_POLL_S, one plan-fix round for the routed functions (one round at a time)."""

    def __init__(self, sup) -> None:
        self.sup = sup
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.n = 0

    def run(self) -> None:
        while not self.stop.wait(ROUTE_POLL_S):
            try:
                self.step()
            except Exception as e:  # noqa: BLE001 - never kill the supervisor from the side thread
                self.sup.log(f"router: error: {type(e).__name__}: {e}")

    def step(self) -> bool:
        """One round for up to plan_fix_parallel routed needs-plan functions. True when one ran."""
        sup = self.sup
        recs = sup.state()
        todo = [r for r in routes(sup.ov) if recs.get(r["id"], {}).get("status") == "needs-plan"]
        todo = todo[:sup.parallel]
        if not todo:
            return False
        self.n += 1
        round_(sup, f"X{self.n}", todo)
        return True


def round_(sup, label: str, todo: list[dict]) -> None:
    """Plan-fix agents (class and evidence in the brief), the integrator, the plan land, then reset requests."""
    import drive  # noqa: PLC0415 - supervise already imported it
    import land as land_mod  # noqa: PLC0415
    import supervise  # noqa: PLC0415
    fixing = [r["id"] for r in todo]
    by = {r["id"]: r for r in todo}
    rows, recs = sup.rows(), sup.state()
    ids = supervise.reserve([f.id for f in rows], fixing)
    work = sup.ov / "supervise" / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{label}"
    work.mkdir(parents=True, exist_ok=True)
    sup.git("worktree", "prune", check=False)
    sup.log(f"router round {label}: plan-fix " + " ".join(f"{fid} ({by[fid]['class']})" for fid in fixing))
    jobs = []
    try:
        for fid in fixing:
            wt, branch = work / "wt" / fid, f"plan/fix-{fid}"
            sup.git("worktree", "add", "-q", "-f", "-B", branch, str(wt), sup.main)
            report = work / f"plan-fix-{fid}.md"
            r = by[fid]
            mode = (f"The router classed this failure `{r['class']}` ({WORDS[r['class']]}): {HINT[r['class']]}."
                    f" Evidence: {str(r.get('evidence', '')).replace(chr(10), ' ')[:900]}")
            if int(recs.get(fid, {}).get("resets", 0)) >= drive.PLAN_FIXES:
                mode += (" This function already had two plan fixes and is never reset again: replace it with new "
                         "rows using the reserved ids and turn its own row into `deferred` naming them.")
            brief = work / f"brief-{fid}.md"
            brief.write_text(drive.render("plan-fix-brief.md", {
                "id": fid, "round": label, "skill": supervise.HERE.parent.as_posix(),
                "plan_skill": supervise.PLAN_SKILL.as_posix(), "wt": wt.as_posix(), "branch": branch,
                "main": sup.main, "repo": sup.repo.as_posix(), "ids": ", ".join(ids[fid]),
                "resets": str(recs.get(fid, {}).get("resets", 0)),
                "reason": (recs.get(fid, {}).get("last_reason") or "-").replace("\n", " ")[:400],
                "report": report.as_posix(), "functions": f"{wt.as_posix()}/{sup.functions}",
                "validate": (supervise.HERE / "plan_validate.py").as_posix(), "mode": mode}), encoding="utf-8")
            jobs.append((fid, wt, brief, work / f"plan-fix-{fid}.json", sup.fix_model))
        sup.agents(jobs)
        fixed = []
        for fid in fixing:
            ahead = int(sup.git("rev-list", "--count", f"{sup.main}..plan/fix-{fid}").strip() or 0)
            line1 = supervise.first_line(drive.read_text(work / f"plan-fix-{fid}.md"))
            ok = bool(ahead) and "PLAN OK" in line1 and split_ok(by[fid]["class"] == "oversized", line1)
            (fixed.append(fid) if ok else None)
            sup.log(f"router plan-fix {fid}: {ahead} commit(s); {line1 or 'no report'}")
        if not fixed:
            done(sup.ov, fixing, "no plan fix succeeded; left needs-plan for Exit 3")
            return
        branch, merge_wt = f"plan/fixes-{label}", work / "wt" / "_merge"
        sup.git("worktree", "add", "-q", "-f", "-B", branch, str(merge_wt), sup.main)
        if len(fixed) == 1:
            sup.git("merge", "-q", "--ff-only", f"plan/fix-{fixed[0]}", cwd=merge_wt)
        else:
            report = work / "plan-fix-merge.md"
            listing = "\n".join(f"- `plan/fix-{fid}`: reserved {', '.join(ids[fid])}; report "
                                f"`{(work / f'plan-fix-{fid}.md').as_posix()}`" for fid in fixed)
            brief = work / "brief-merge.md"
            brief.write_text(drive.render("plan-fix-merge.md", {
                "round": label, "wt": merge_wt.as_posix(), "branch": branch, "main": sup.main,
                "repo": sup.repo.as_posix(), "max_phase": str(sup.max_phase), "report": report.as_posix(),
                "branches": listing, "validate": (supervise.HERE / "plan_validate.py").as_posix()}), encoding="utf-8")
            sup.agents([("merge", merge_wt, brief, work / "plan-fix-merge.json", sup.merge_model)])
            sup.log(f"router integrator: {supervise.first_line(drive.read_text(report)) or 'no report'}")
        land_plan(sup, land_mod, branch, merge_wt, work, label)
        active = {f.id for f in sup.rows() if f.status != drive.DEFERRED}
        req = sup.ov / REQUESTS_DIR
        req.mkdir(parents=True, exist_ok=True)
        resets = [fid for fid in fixed if fid in active]
        for fid in resets:
            (req / f"reset-{fid}").write_text(f"{label}\n", encoding="utf-8")
        if not resets:
            (req / f"reload-{label}").write_text(f"{label}\n", encoding="utf-8")
        done(sup.ov, fixed, f"landed in {label}")
        done(sup.ov, [f for f in fixing if f not in fixed], "no plan fix; left needs-plan for Exit 3")
        sup.log(f"router round {label}: landed; reset requests {' '.join(resets) or '-'}")
    except Exception as e:  # noqa: BLE001
        done(sup.ov, fixing, f"round failed: {e}")
        sup.log(f"router round {label}: {type(e).__name__}: {e}; still needs-plan for Exit 3")
    finally:
        sup.drop_worktrees(work)


def land_plan(sup, land_mod, branch: str, merge_wt: Path, work: Path, label: str) -> None:
    """--run in the round's worktree, then under .overhaul/lock (no land in between): rebase onto main, PLAN OK,
    fast-forward main."""
    import supervise  # noqa: PLC0415
    code, line = sup.run([*sup.validate_cmd, "--repo", str(merge_wt), "--run", "--timeout",
                          str(sup.validate_timeout)], work / "validate-run.txt")
    sup.log(f"router plan_validate --run {branch}: {line}")
    if code or not line.startswith("PLAN OK"):
        raise supervise.Stop(f"{branch} is not PLAN OK with --run: {line}")
    if not land_mod.acquire_lock(str(sup.repo), wait=LOCK_WAIT_S, owner="router.py"):
        raise supervise.Stop(".overhaul/lock held too long by land.py")
    try:
        sup.git("rebase", "-q", sup.main, cwd=merge_wt)
        code, line = sup.run([*sup.validate_cmd, "--repo", str(merge_wt)], work / "validate-static.txt")
        if code or not line.startswith("PLAN OK"):
            raise supervise.Stop(f"{branch} after rebase is not PLAN OK: {line}")
        sup.git("merge", "-q", "--ff-only", branch)
        sup.log(f"router main -> {branch} {sup.git('rev-parse', '--short=12', 'HEAD').strip()}")
    finally:
        land_mod.release_lock(str(sup.repo))


def done(ov: Path, fids: list[str], why: str) -> None:
    for fid in fids:
        p = route_file(ov, fid)
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        doc.update(done=why, t_done=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        p.write_text(json.dumps(doc), encoding="utf-8")


@contextlib.contextmanager
def watching(sup):
    """supervise.py hook around a drive run: route rounds while it runs; joined (finished) before it returns.
    With live-replan's live rounds (sup.live_step) present they own plan fixes while drive runs: nothing starts."""
    if hasattr(sup, "live_step"):
        yield None
        return
    w = Watch(sup)
    w.thread = threading.Thread(target=w.run, daemon=True)
    w.thread.start()
    try:
        yield w
    finally:
        w.stop.set()
        w.thread.join()


def apply_requests(sup) -> list[str]:
    """supervise.py hook after drive exits (drive is not running): apply .overhaul/requests/reset-<id> with
    drive --reset, drop reload-* (drive reloads the plan at start). The ids reset; an Exit-3 round is not needed for
    them."""
    req = sup.ov / REQUESTS_DIR
    if not req.is_dir():
        return []
    applied = []
    for p in sorted(req.glob("reset-*")):
        fid = p.name[len("reset-"):]
        recs = sup.state()
        if recs.get(fid, {}).get("status") == "needs-plan":
            code, line = sup.run([*sup.drive_cmd, "--repo", str(sup.repo), "--reset", fid],
                                 sup.ov / "supervise" / f"reset-{fid}.txt")
            sup.log(f"router reset {fid}: " + ("ok" if not code else line))
            if not code:
                applied.append(fid)
        p.unlink(missing_ok=True)
    for p in req.glob("reload-*"):
        p.unlink(missing_ok=True)
    return applied


def main(argv: list[str] | None = None) -> int:
    """py -3 router.py --repo R: the failure classes of the last 60 min from .overhaul/log.jsonl."""
    import argparse  # noqa: PLC0415
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--minutes", type=float, default=WINDOW_MIN)
    a = ap.parse_args(argv)
    events = []
    log = Path(a.repo) / ".overhaul" / "log.jsonl"
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if log.is_file() else []:
        with contextlib.suppress(ValueError):
            events.append(json.loads(line))
    f = failures(events, dt.datetime.now(dt.timezone.utc), a.minutes)
    print(f["line"] + (f"; RED {f['words']} -> {f['action']}" if f["red"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
