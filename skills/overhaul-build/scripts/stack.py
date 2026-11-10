"""Stacked starts ([harness] stacked_starts, default false): a planned row may start while a dep's fn/<dep> branch has
passed review but not landed yet (status `landing`). Its worktree merges main and then each such dep branch, it
never lands before its deps, and it goes back to planned (no attempt counted) when a dep's branch changes.

rate_sim.py on the pilot project's dependency graph: only-proven deps hold the rate at ~7.5/h whatever the slots; stacking on
reviewed deps gives 14.5-15.8/h at 40 slots.

The record of a stacked row keeps `stacked_on` {dep: sha of fn/<dep> it merged}. Reviews, the pre-review checks and
the land footprint diff the row's branch against main + those shas (diff_range), so nobody judges a dep's code.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

RESET = "stack_reset"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL)


def stackable(drv, dep: str) -> bool:
    """dep's branch passed review and waits to land (queued, landing, or held on a main-flake fix)."""
    rec = drv.st["functions"].get(dep)
    return bool(drv.stacked and rec and rec.get("status") == "landing")


def deps_ok(drv, f, proven: set[str]) -> bool:
    """Every blocking dep proven, or (stacked starts on, this row not barred by a stack conflict) stackable."""
    on = drv.stacked and not drv.st["functions"][f.id].get("no_stack")
    return all(d in proven or (on and stackable(drv, d)) for d in f.blocking)


def to_stack(drv, f) -> list[str]:
    """The not-yet-proven deps f would build on, in plan order."""
    recs = drv.st["functions"]
    deps = [d for d in dict.fromkeys(f.blocking) if recs.get(d, {}).get("status") != "proven" and stackable(drv, d)]
    return sorted(deps, key=lambda d: drv.fns[d].order if d in drv.fns else 1 << 30)


def brief_note(deps) -> str:
    if not deps:
        return ""
    names = ", ".join(deps)
    return (f"\nBuilt on unlanded deps: {names}. Their reviewed branches are merged into this worktree; "
            f"do not edit them (their files are theirs: a bug there is BLOCKED-BY, not a fix here).")


def merge_deps(drv, fid: str, wt: Path, deps: list[str]) -> dict[str, str] | None:
    """Merge each dep's fn/<dep> into fid's fresh worktree; {dep: sha}. None on a conflict: the worktree goes back
    to main and the row waits for its deps to land (no_stack), as without stacked starts."""
    shas: dict[str, str] = {}
    for d in deps:
        sha = _git(drv.repo, "rev-parse", "--verify", f"refs/heads/fn/{d}^{{commit}}").stdout.strip()
        p = _git(wt, "merge", "-q", "--no-edit", "-m", f"{fid}: stack on fn/{d}", sha) if sha else None
        if p is None or p.returncode != 0:
            _git(wt, "merge", "--abort")
            _git(wt, "checkout", "-q", "-f", "-B", f"fn/{fid}", drv.main)
            _git(wt, "clean", "-fdq")
            lines = [] if p is None else (p.stdout + p.stderr).strip().splitlines()
            why = "no branch" if p is None else (lines[-1] if lines else "merge conflict")
            drv.st["functions"][fid]["no_stack"] = True
            drv.log(fid, "stack_conflict", f"cannot merge fn/{d}: {why}; waits for its deps to land")
            return None
        shas[d] = sha
    return shas


def base(drv, fid: str) -> str:
    """A commit equal to main (as fn/<fid> last merged it) + every dep sha it stacked on; '' if not stacked or
    the merge cannot be rebuilt (then callers fall back to main...branch)."""
    on = drv.st["functions"].get(fid, {}).get("stacked_on") or {}
    if not on:
        return ""
    branch = f"fn/{fid}"
    mb = _git(drv.repo, "merge-base", drv.main, branch)
    if mb.returncode != 0:
        return ""
    cur = mb.stdout.strip()
    for d, sha in on.items():
        if _git(drv.repo, "merge-base", "--is-ancestor", sha, cur).returncode == 0:
            continue
        tree = _git(drv.repo, "merge-tree", "--write-tree", cur, sha)
        if tree.returncode != 0:
            return ""
        c = _git(drv.repo, "commit-tree", tree.stdout.split()[0], "-p", cur, "-p", sha, "-m", f"stack base {d}")
        if c.returncode != 0:
            return ""
        cur = c.stdout.strip()
    return cur


def diff_range(drv, branch: str) -> list[str]:
    """`git diff` arguments for what branch itself changes: main...branch, or base..branch for a stacked row."""
    fid = branch[3:] if branch.startswith("fn/") else ""
    b = base(drv, fid) if fid else ""
    return [b, branch] if b else [f"{drv.main}...{branch}"]


def reset(drv, fid: str, dep: str, why: str) -> None:
    """fid was built on dep's branch, which changed: stop its work and requeue it, no attempt counted."""
    rec = drv.st["functions"][fid]
    job = drv.jobs.pop(fid, None)
    if job is not None:
        drv.kill(job)
    drv.verify_meta.pop(fid, None)
    if fid in drv.land_queue:
        drv.land_queue.remove(fid)
    was = rec.get("status")
    rec.pop("stacked_on", None)
    drv.set(fid, status="planned", host=None)
    drv.log(fid, RESET, f"dep {dep} {why}; was {was}, back to planned, attempt not counted")
    drv.say(f"{fid} stack reset: dep {dep} {why}")


def check(drv) -> bool:
    """Reset every in-flight row whose stacked-on dep left `landing` other than by landing (land failed, a conflict
    pass, a rebuild, parked). Repeats so a reset reaches rows stacked on the reset row. True if any was reset."""
    if not drv.stacked:
        return False
    recs, any_reset = drv.st["functions"], False
    while True:
        hit = None
        for fid, rec in recs.items():
            on = rec.get("stacked_on")
            if not on or rec.get("status") in ("planned", "proven", "needs-plan"):
                continue
            for d in on:
                st = recs.get(d, {}).get("status")
                if st not in ("landing", "proven"):
                    hit = (fid, d, f"is {st}" if st else "is gone")
                    break
            if hit:
                break
        if not hit:
            return any_reset
        reset(drv, *hit)
        any_reset = True


def landable(drv, fid: str) -> bool:
    """A queued row lands only after every dep it stacked on is proven with the sha it merged. A dep that landed a
    different branch resets the row. Once checked, stacked_on is kept only as `stacked` (the deps' names)."""
    rec = drv.st["functions"].get(fid, {})
    on = rec.get("stacked_on")
    if not on:
        return True
    recs = drv.st["functions"]
    if any(recs.get(d, {}).get("status") != "proven" for d in on):
        return False
    for d, sha in on.items():
        if _git(drv.repo, "merge-base", "--is-ancestor", sha, drv.main).returncode != 0:
            reset(drv, fid, d, f"landed a branch other than {sha[:12]}")
            return False
    rec.pop("stacked_on", None)
    drv.set(fid, stacked=list(on))
    return True


def landed(drv, dep: str) -> None:
    """dep landed: each queued row stacked on it merges the new main (no rebuild), so its branch is main + its own
    work again. A conflict is left for land.py to meet (a conflict-fix pass)."""
    for fid, rec in drv.st["functions"].items():
        if dep not in (rec.get("stacked_on") or {}) or rec.get("status") != "landing" or fid in drv.jobs:
            continue
        wt = drv.ov / "wt" / fid
        if not (wt / ".git").exists():
            continue
        p = _git(wt, "merge", "-q", "--no-edit", "-m", f"{fid}: merge {drv.main} after {dep} landed", drv.main)
        if p.returncode != 0:
            _git(wt, "merge", "--abort")
            drv.log(fid, "stack_merge_conflict", f"merging {drv.main} after {dep} landed; land.py will meet it")
