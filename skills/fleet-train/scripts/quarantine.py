"""Main-red acceptance tests: an unowned failing test red alone on main is quarantined with a fix request (Healed);
past CAP, MainBroken pauses the train. Functions take the harness module `h` (tests pass a fake).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

CAP = 3
RECHECK_SECS = 2 * 3600  # a red test can cost 900 s a run
KNOWN = "accept-known-red.json"  # {test: reason str | entry dict}
BASELINE = "accept-main-baseline.json"  # {"sha", "red": {test: tail}, "green": [test]}
CHECKS = "quarantine-checks.json"  # {test: ts}
BROKEN = "main-broken.json"
TAIL = 1500


class MainBroken(Exception):
    """More than CAP tests red on main: a human fix of main is needed."""


def stem(test: str) -> str:
    return Path(test.split("::")[0]).stem


def request_path(h, test: str) -> Path:
    return h.HOME / "notes" / "requests" / f"fix-test-{stem(test)}.md"


REQUEST_MIN_AGE = 600  # its writer may be starting it
REQUEST_OWNED = ("split-",)  # prefixes another alarm owns


def open_requests(h, now: float, min_age: float = REQUEST_MIN_AGE) -> list:
    """notes/requests/*.md older than min_age that no other alarm owns (done ones move to notes/archive/)."""
    out = []
    for p in sorted((h.HOME / "notes" / "requests").glob("*.md")):
        if p.name.startswith(REQUEST_OWNED + ("_", "README")) or now - p.stat().st_mtime < min_age:
            continue
        out.append(p)
    return out


def unowned(fails, rows: dict, ids) -> list:
    """Failing tests no batch row owns."""
    own = {t for i in ids for t in rows.get(i, {}).get("tests", [])}
    return sorted({t for t in fails if "::" in t and t not in own})


def on_main(h, test: str) -> bool:
    """A test file only the batch adds is its own test, never quarantined."""
    return h.git("cat-file", "-e", f"{h.BASE_REF}:{test.split('::')[0]}", check=False).returncode == 0


def main_red(h, tests) -> dict:
    """{test: failure tail} of the tests red alone on main, cached per main sha."""
    proven, unbuilt = main_baseline(h, tests)
    return {**proven, **unbuilt}


def main_baseline(h, tests) -> tuple:
    """({test: tail} red alone on main, unbuilt there: never cached)."""
    sha = h.git("rev-parse", h.BASE_REF).stdout.strip()
    cache = h.load(BASELINE, {})
    if cache.get("sha") != sha:
        cache = {"sha": sha, "red": {}, "green": []}
    todo = [t for t in tests if t not in cache["red"] and t not in cache["green"]]
    if todo:
        with h.at_main():
            red = h.run_alone(todo)
        # a build failure is no verdict: never cached
        unbuilt = {t: red[t][-TAIL:] for t in todo if t in red and red[t].startswith(h.BUILD_FAILED)}
        cache["red"].update({t: red[t][-TAIL:] for t in todo if t in red and t not in unbuilt})
        cache["green"] = sorted(set(cache["green"]) | {t for t in todo if t not in red})
        h.save(BASELINE, cache)
        h.event("main_baseline", sha=sha, tests=todo[:10], red=sorted(t for t in todo if t in red)[:10], unbuilt=len(unbuilt))
    else:
        unbuilt = {}
    return {t: cache["red"][t] for t in tests if t in cache["red"]}, unbuilt


@dataclass(frozen=True)
class Healed:
    """What one heal quarantined; falsy: nothing."""
    proven: frozenset = frozenset()
    unbuilt: frozenset = frozenset()

    def __bool__(self) -> bool:
        return bool(self.proven or self.unbuilt)

    def covers(self, fails) -> bool:
        """All failures proven main-red."""
        return bool(fails) and set(fails) <= self.proven


def heal(h, merged: list, rows: dict, fails, ids) -> Healed:
    """Quarantine unowned failing tests (`ids`: landing rows) red alone on main. MainBroken past CAP."""
    cand = [t for t in unowned(fails, rows, ids) if on_main(h, t)]
    known = h.known_red()
    proven, unbuilt = main_baseline(h, [t for t in cand if t not in known])
    red = {**proven, **unbuilt}
    if not red:
        return Healed()
    if len(known) + len(red) > CAP:
        sha = h.git("rev-parse", h.BASE_REF).stdout.strip()
        h.save(BROKEN, {"t": h.iso(), "sha": sha, "quarantined": sorted(known), "red": sorted(red)})
        h.event("quarantine_cap", sha=sha, quarantined=sorted(known), red=sorted(red)[:10])
        raise MainBroken(f"{len(known)} quarantined + {len(red)} more red on main {sha[:10]}: a human fix of main is needed")
    sha = h.git("rev-parse", h.BASE_REF).stdout.strip()
    with h.lock("state"):
        kr = h.load(KNOWN, {})
        for t, tail in red.items():
            req = request_path(h, t)
            kr[t] = {"by": "train", "since": h.iso(), "main": sha, "request": req.relative_to(h.HOME).as_posix(),
                     "reason": f"red alone on main {sha[:10]} (batch {[q['row'] for q in merged][:6]})", "evidence": tail}
        h.save(KNOWN, kr)
    for t, tail in red.items():
        write_request(h, t, tail, sha)
        h.event("quarantine", test=t, sha=sha, request=request_path(h, t).name)
    return Healed(frozenset(proven), frozenset(unbuilt))


def slow(h, tests: dict, sha: str) -> None:
    """Quarantine tests whose alone re-run hit its time cap ({test: tail}): no verdict, never red (slow-tests class)."""
    if not tests:
        return
    with h.lock("state"):
        kr = h.load(KNOWN, {})
        for t, tail in tests.items():
            kr.setdefault(t, {"by": "time-cap", "since": h.iso(), "main": sha, "evidence": tail,
                              "request": request_path(h, t).relative_to(h.HOME).as_posix(),
                              "reason": f"alone re-run hit its time cap at {sha[:10]}: no verdict (class slow tests)"})
        h.save(KNOWN, kr)
    for t, tail in tests.items():
        write_request(h, t, tail, sha)
        h.event("quarantine_slow", test=t, sha=sha)


REQUEST = """# Fix request: {test}
> RULE ZERO: valuable, well made, clean, extensible code; never loosen a check (no timeout raise, no test weakening).

Written by the land train {t} (quarantine.py). This acceptance test fails run ALONE on main {sha} (not any row's
fault), so the gate skips it (state/accept-known-red.json) until it passes on main again. Make the CODE pass inside the
gate's budget (900 s a test run; aim well under 120 s) and keep every assertion. Clean slot: `py -3 harness.py
clean-start --slot <N> --name fix-{stem}`; time `{name}` of `{file}` run alone; quality-check OK;
clean-submit. The train returns the test to the gate by itself once it passes alone on main
(event `unquarantine`, this file becomes .done): never edit state/accept-known-red.json by hand.

Failure on main (tail):
```
{tail}
```
"""


def write_request(h, test: str, tail: str, sha: str) -> None:
    p = request_path(h, test)
    p.parent.mkdir(parents=True, exist_ok=True)
    file, name = test.split("::", 1)
    text = REQUEST.format(test=test, t=h.iso(), sha=sha[:10], stem=stem(test), file=file, name=name, tail=tail[-TAIL:])
    if p.exists():
        if test in p.read_text(encoding="utf-8"):
            return
        text = p.read_text(encoding="utf-8") + "\n" + text
    p.write_text(text, encoding="utf-8")


def release(h) -> list:
    """Release the quarantined tests now green alone on main; returns them."""
    known = h.known_red()
    checks = h.load(CHECKS, {})
    due = [t for t in sorted(known) if h.now() - checks.get(t, 0) >= RECHECK_SECS]
    if not due:
        return []
    red = main_red(h, due)
    checks.update({t: h.now() for t in due})
    green = [t for t in due if t not in red]
    with h.lock("state"):
        kr = h.load(KNOWN, {})
        for t in green:
            kr.pop(t, None)
            checks.pop(t, None)
        h.save(KNOWN, kr)
    h.save(CHECKS, {t: ts for t, ts in checks.items() if t in kr})
    for t in green:
        p = request_path(h, t)
        if p.exists() and not any(stem(k) == stem(t) for k in kr):
            p.replace(p.with_name(p.name + ".done"))
        h.event("unquarantine", test=t, sha=h.git("rev-parse", h.BASE_REF).stdout.strip())
    return green


def paused(h) -> bool:
    """True while a quarantine_cap pause holds: main unchanged and its tests not recovered."""
    b = h.load(BROKEN, None)
    if b is None:
        return False
    sha = h.git("rev-parse", h.BASE_REF).stdout.strip()
    if sha == b.get("sha") and not recovered(h, b):
        return True
    (h.ST / BROKEN).unlink(missing_ok=True)
    h.event("main_unpaused", was=b.get("sha"), sha=sha)
    return False


def recovered(h, b: dict) -> bool:
    """Every test behind the pause passes alone on main (uncached, at most every RECHECK_SECS)."""
    if h.now() - b.get("checked", 0) < RECHECK_SECS:
        return False
    tests = sorted(set(b.get("quarantined", [])) | set(b.get("red", [])))
    if not tests:
        return True
    with h.at_main():
        red = h.run_alone(tests)
    b["checked"] = h.now()
    h.save(BROKEN, b)
    return not red


FIX_PREFIX = "CLEAN-"  # fix rows (clean-submit): the only rows that can repair a red main


def train_queue(h, queue: list) -> list:
    """Rows the train may pick: only fix rows while a pause holds."""
    if not paused(h):
        return queue
    return [q for q in queue if q["row"].startswith(FIX_PREFIX)]


DISPATCH_RE = re.compile(r"^\s*([\w.-]+)\s*\|\s*\w+\s*\|\s*(\S+)\s*\|\s*(\w+)")


def fix_status(h, test: str) -> str:
    """Alarm text: the fix request or brief for `test`, and whether a DISPATCH.md lane works it."""
    rel = request_path(h, test).relative_to(h.HOME).as_posix()
    briefs = {rel} | set(re.findall(r"notes/(?:briefs|requests)/[\w.-]+\.md", str(h.known_red().get(test, ""))))
    disp = h.HOME / "notes" / "DISPATCH.md"
    lanes = []
    for line in (disp.read_text(encoding="utf-8", errors="replace").splitlines() if disp.exists() else []):
        m = DISPATCH_RE.match(line)
        brief = h.HOME / m.group(2) if m else None
        if m and (m.group(2) in briefs or (brief.is_file() and stem(test) in brief.read_text(encoding="utf-8", errors="replace"))):
            lanes.append((m.group(1), m.group(3)))
    if any(st != "done" for _, st in lanes):
        return f"fix worked by {[ln for ln, st in lanes if st != 'done'][0]}"
    if lanes:
        return f"fix lane {lanes[-1][0]} done but the test is still red on main: brief a new approach"
    have = sorted(b for b in briefs if (h.HOME / b).is_file())
    return f"fix brief {have[0]} NOT dispatched: launch one fix agent from it (add its DISPATCH.md line)" if have else \
        f"NO fix request: write {rel} and launch one fix agent"
