"""Every alarm id fleet_beat.py raises -> its handler (alarm_dispatch.py). Auto fixes are idempotent and time-boxed:
subprocess timeouts come from ctx.left(), deletes stop at ctx.deadline and the next run goes on."""
from __future__ import annotations

import os, re, shutil, subprocess, sys, time
from pathlib import Path

from alarm_dispatch import Agent, Auto, Failed, Manager, Owned, live_attempt

FINISHED_AGE = 3 * 3600  # untouched this long = finished work
EMERGENCY_GB = 50  # below this the train's debug incremental cache goes too
STALE_CLAIM = 3 * 3600
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def alarm_id(text: str) -> str:
    """First word; BREVITY split by what code can fix."""
    head = text.split(" ", 1)[0]
    if head == "BREVITY":
        if text.startswith("BREVITY copies"):
            return "BREVITY-COPIES"
        if text.startswith("BREVITY notes/ live-words"):
            return "BREVITY-NOTES"
    return head


# ---------------------------------------------------------------- auto fixes
def push_origin(ctx, a) -> str:
    if ctx.dry:
        return "would push origin main"
    r = subprocess.run(["git", "push", "-q", "origin", "main"], cwd=str(ctx.h.REPO), capture_output=True, text=True,
                       timeout=ctx.left(300), creationflags=NOWIN)
    if r.returncode:
        raise Failed(f"git push origin main: {(r.stderr or r.stdout).strip()[-200:]}")
    return "pushed main to origin"




def newest_mtime(d: Path) -> float:
    """Newest mtime of a cargo target and the dirs every build rewrites."""
    times = [d.stat().st_mtime]
    for prof in ("debug", "release"):
        for sub in ("", "deps", ".fingerprint", "incremental", "build"):
            p = d / prof / sub
            if p.exists():
                times.append(p.stat().st_mtime)
    return max(times)


def drop_stale_fingerprints(root: Path) -> int:
    """Delete cargo fingerprints whose library output is gone, so cargo rebuilds the crate instead of failing with
    'extern location does not exist'. Any prune of deps/ by age leaves such fingerprints: mtime is not use, an rlib
    the gate links can be days old. Run it after every deps prune."""
    n = 0
    for prof in ("debug", "release"):
        fp, deps = root / prof / ".fingerprint", root / prof / "deps"
        if not fp.is_dir():
            continue
        have = {e.name for e in os.scandir(deps)} if deps.is_dir() else set()
        for e in os.scandir(fp):
            name, _, sha = e.name.rpartition("-")
            if not e.is_dir() or not any(f.startswith("lib-") for f in os.listdir(e.path)):
                continue
            stem = name.replace("-", "_")
            if not any(x.startswith((f"lib{stem}-{sha}.", f"{stem}-{sha}.")) for x in have):
                shutil.rmtree(e.path, ignore_errors=True)
                n += 1
    return n


def fix_cache(ctx, a) -> str:
    """CACHE-BROKEN: drop the stale fingerprints in the train target so the next gate rebuilds those crates."""
    n = 0 if ctx.dry else drop_stale_fingerprints(Path(ctx.h.TARGETS) / "land-0")
    if not n:
        raise Failed("no stale fingerprint in land-0: the missing rlib has another cause (read last-gate.txt)")
    return f"dropped {n} stale fingerprints in land-0"


def slots_in_use(h, now: float, alive=None) -> set:
    used = {str(k) for k in h.load("claims.json", {})}
    # a queued row never builds in its slot (the train builds in its own target): its slot target is free to delete
    used |= {str(x.get("slot")) for e in h.load("dispatch.json", {}).values() for x in e.get("attempts", [])
             if live_attempt(x, now, alive)}
    for brief in open_lane_briefs(h):
        used |= set(re.findall(r"--slot (\d+)", brief.read_text(encoding="utf-8", errors="replace")))
    return used


LANE_CLOSED = ("done", "paused", "retired")  # DISPATCH.md status words whose brief no longer builds


def open_lane_briefs(h) -> list:
    disp = Path(h.HOME) / "notes" / "DISPATCH.md"
    out = []
    for line in disp.read_text(encoding="utf-8", errors="replace").splitlines() if disp.exists() else []:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4 and not parts[3].lower().startswith(LANE_CLOSED) and (Path(h.HOME) / parts[2]).is_file():
            out.append(Path(h.HOME) / parts[2])
    return out


def rm_tree(p: Path, deadline: float) -> tuple[float, bool]:
    """(GB freed, all gone): bottom-up until deadline."""
    before = shutil.disk_usage(str(p.anchor)).free
    done = True
    for root, dirs, files in os.walk(p, topdown=False):
        if time.time() > deadline:
            done = False
            break
        for f in files:
            try:
                os.unlink(os.path.join(root, f))
            except OSError:
                pass
        try:
            os.rmdir(root)
        except OSError:
            pass
    return max(0.0, (shutil.disk_usage(str(p.anchor)).free - before) / 1e9), done and not p.exists()


def free_disk(ctx, a) -> str:
    """Finished slot targets and qc-other; under EMERGENCY_GB the train's incremental cache (do_disk owns land-0 ageing)."""
    h, t = ctx.h, ctx.now
    used = slots_in_use(h, t, ctx.alive)
    free = shutil.disk_usage(str(Path(h.TARGETS).anchor)).free / 1e9
    todo = [d for d in sorted(Path(h.TARGETS).glob("slot-*")) + [Path(h.TARGETS) / "qc-other"] if d.is_dir()
            and (d.name[5:] if d.name.startswith("slot-") else "") not in used and t - newest_mtime(d) >= FINISHED_AGE]
    inc = Path(h.TARGETS) / "land-0" / "debug" / "incremental"
    if free < EMERGENCY_GB and inc.is_dir():
        todo.append(inc)
    if not todo:
        raise Failed(f"nothing finished to delete ({round(free)} GB free); in-use slots {sorted(used)[:8]}")
    gone, gb, cut = [], 0.0, False
    for d in todo:
        if ctx.dry:
            gone.append(d.name)
            continue
        freed, whole = rm_tree(d, ctx.deadline)
        gb += freed
        gone.append(d.name if whole else d.name + " (partly)")
        if not whole:
            cut = True
            break
    return f"deleted {', '.join(gone)[:160]} (+{round(gb)} GB)" + ("; time box spent, next run goes on" if cut else "")




def finished_briefs(h) -> list:
    """Done in DISPATCH.md, or answered with no open lane; never _runbooks or INDEX."""
    import notes_index
    home = Path(h.HOME)
    hbs = sorted(p.stem for p in (home / "notes" / "handbacks").glob("*.md"))
    status = {}
    disp = home / "notes" / "DISPATCH.md"
    for line in disp.read_text(encoding="utf-8", errors="replace").splitlines() if disp.exists() else []:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4 and parts[2].startswith("notes/briefs/"):
            status[Path(parts[2]).name] = parts[3]
    out = []
    for p in sorted((home / "notes" / "briefs").glob("*.md")):
        if p.name.startswith("_") or p.name == "INDEX.md" or status.get(p.name, "done") != "done":
            continue
        if status.get(p.name) == "done" or notes_index.answers(p.stem, hbs):
            out.append(p)
    return out


def archive_briefs(ctx, a) -> str:
    h = ctx.h
    home = Path(h.HOME)
    done = finished_briefs(h)
    if not done:
        raise Failed("no finished brief to archive: the live notes need a trim pass")
    if ctx.dry:
        return f"would archive {len(done)} briefs"
    dest = home / "notes" / "archive" / "briefs"
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for p in done:
        target = dest / p.name
        os.replace(p, target)
        paths += [p.relative_to(home).as_posix(), target.relative_to(home).as_posix()]
    git = lambda *x: subprocess.run(["git", *x], cwd=str(home), capture_output=True, text=True, creationflags=NOWIN)
    git("add", "-A", "--", *paths)
    r = git("commit", "-q", "-m", f"archive {len(done)} finished briefs (alarm dispatch)", "--", *paths)
    if r.returncode:
        raise Failed(f"moved {len(done)} briefs but the commit failed: {(r.stderr or r.stdout).strip()[-160:]}")
    return f"archived {len(done)} finished briefs ({', '.join(p.stem for p in done)[:120]})"


def manual_lane(h, a, handler) -> str:
    """An open DISPATCH.md lane, or a fresh brief with no hand-back, naming this alarm's subject or hint."""
    needle = Path(a.subject.split("::")[0]).stem if a.subject else getattr(handler, "hint", "")
    if not needle:
        return ""
    briefs = set(open_lane_briefs(h))
    home = Path(h.HOME)
    hbs = {p.stem for p in (home / "notes" / "handbacks").glob("*.md")}
    briefs |= {p for p in (home / "notes" / "briefs").glob("*.md") if time.time() - p.stat().st_mtime < FINISHED_AGE
               and p.name != "INDEX.md" and not p.name.startswith("_")
               and not any(x == p.stem or x.startswith(p.stem + "-") for x in hbs)}
    for p in sorted(briefs):
        if needle in p.name or needle in p.read_text(encoding="utf-8", errors="replace"):
            return p.stem
    return ""


def release_stale(ctx, a) -> str:
    """Release claims held STALE_CLAIM with nothing queued (harness.py release: the row goes back to the frontier)."""
    h = ctx.h
    queued = {q["row"] for q in h.load("queue.json", [])}
    stale = [(k, c["row"]) for k, c in h.load("claims.json", {}).items()
             if ctx.now - c.get("ts", ctx.now) > STALE_CLAIM and c["row"] not in queued]
    if ctx.dry or not stale:
        return f"{'would release' if ctx.dry else 'no stale claim left'}: {[r for _, r in stale][:6]}"
    py = sys.executable.replace("pythonw", "python")
    for k, row in stale:
        r = subprocess.run([py, str(Path(h.HOME) / "harness.py"), "release", "--slot", str(k), "--row", row, "--reason",
                            "stale claim: held 3+ h with nothing queued (alarm dispatch released it)"],
                           cwd=str(h.HOME), capture_output=True, text=True, timeout=ctx.left(120), creationflags=NOWIN)
        if r.returncode:
            raise Failed(f"release {row}: {(r.stderr or r.stdout).strip()[-160:]}")
    return f"released {[r for _, r in stale][:6]}"


def request_lane(h, rel: str) -> str:
    """A live lane (open DISPATCH row or fresh brief with no hand-back) that names this request file."""
    from types import SimpleNamespace
    return manual_lane(h, SimpleNamespace(subject=rel), SimpleNamespace(hint=""))


# ---------------------------------------------------------------- the table
LANDS = 90  # cooldown minutes for a fix landing through the train
RED = ("REPEAT-TEST", "KNOWN-RED", "MAIN-RED", "TRAIN-DOWN", "TRAIN-ERROR", "TRAIN-ENV", "DESK-DOWN")
HARNESS_FIX = Agent("harness-fix", cap_min=75, calls=80)
TABLE = {
    "ORIGIN-BEHIND": Auto(push_origin, patience=3600),
    "DISK": Auto(free_disk, patience=6 * 3600, budget_s=480),
    "CACHE-BROKEN": Auto(fix_cache, patience=1800, budget_s=300),
    "STALE-CLAIMS": Auto(release_stale, patience=3600),
    "BREVITY-NOTES": Auto(archive_briefs, patience=6 * 3600),
    "TRAIN-DOWN": Owned("fleet_beat.py (restarts the train detached)", patience=1800),
    "SUPPLY": Owned("the land train: builder supply returns as rows land", patience=6 * 3600),
    "OLD-QUEUE": Owned("the land train: the queue drains as rows land", patience=6 * 3600),
    "STACK-ORPHAN": Owned("the rebaser: rebases the held row onto main without the carried commits", patience=6 * 3600),
    "REPEAT-TEST": Agent("fix-test", slot=True, per_subject=True, cap_min=120, cooldown_min=LANDS),
    "KNOWN-RED": Agent("fix-test", slot=True, per_subject=True, cap_min=120, cooldown_min=LANDS),
    "MAIN-RED": Agent("main-red", slot=True, cap_min=120, cooldown_min=LANDS),
    "STALL": Agent("train-red", covered_by=RED + ("LONG-BATCH", "REPEAT-RED")),
    "LONG-BATCH": Agent("train-red", covered_by=RED + ("REPEAT-RED",)),
    "REPEAT-RED": Agent("train-red", covered_by=RED),
    "REJECTS": Agent("rejects", calls=80),
    "REWORK": Agent("ruling", calls=60, cap_min=60),
    "REQUEST": Agent("request", slot=True, per_subject=True, cap_min=120, cooldown_min=LANDS),
    "TRAIN-ENV": Agent("train-env", calls=80),
    "BREVITY": Agent("trim", model="sonnet", calls=60, cap_min=60),
    "TRAIN-ERROR": HARNESS_FIX,
    "DISPATCH-ERROR": HARNESS_FIX,
    "BREVITY-ERROR": HARNESS_FIX,
    "REQUEST-ERROR": HARNESS_FIX,
    "BEAT-TIMEOUT": HARNESS_FIX,
    "TRAIN-PID": Manager("stop this project's train once by its own pid; the next beat starts it with a pid file"),
    "DESK-DOWN": Manager("the desk gives no verdict (desk_error reasons): bring back its ssh and toolchain; batches wait"),
    "TRAIN-KILLABLE": Manager("restart the train with the quoted full path between batches (never mid-land)"),
}
