"""mainred.py: helpers drive.py uses to tell a red main and a systemic failure from one function's own failure.

Measured on the pilot project 2026-11-09: a plan commit left main itself failing dup_blocks, every build then failed the
pre-review dup gate, each failure was counted as that function's attempt and nothing landed for hours; earlier,
depfix passes kept ending "builder left no committed change". Nothing said "main is red" or "the same failure is
hitting every function". These helpers are pure (no Driver state) so drive.py's hooks stay small.
"""
from __future__ import annotations

import re
import subprocess
import time
from collections import deque
from pathlib import Path

import land  # beside this script (drive.py puts this folder on sys.path)

MAIN_RED = "main-red"  # blocked_on of a function waiting for main to go green again (its attempt not counted)
PLAN_SUBJECT = re.compile(r"^(plan|m0)")  # the guard's plan/m0 commit subjects (guard/reference-transaction)
DEAD_DEPFIX = "builder left no committed change"
SYSTEMIC_MIN = 3  # distinct functions failing the same normalized reason in the window
DEAD_DEPFIX_MIN = 2  # dead depfixes for one owner module in the window
_CHECK = re.compile(r"^(?:land: )?([A-Za-z0-9_.-]+) (?:failed \(exit -?\d+\)|timed out) "
                    r"(?:in the worktree|on merged tree)")
_PATH = re.compile(r"(?<![\w.-])((?:[A-Za-z]:)?[\w.-]*(?:[\\/][\w.-]+)+\.\w+)")
_FILE = re.compile(r"(?<![\w./\\-])((?:[\w.-]+[\\/])+[\w.-]+\.\w+)")


NODE_RE = re.compile(r"tests/[\w./-]+\.(?:rs|py|js|ts)::\w+")


def check_name(reason: str, names: set[str]) -> str:
    """The whole-tree check a pre-review or land failure names ('dup failed (exit 1) in the worktree ...',
    'land: test failed (exit 1) on merged tree: ...'), or '' when the reason is not one of `names`."""
    m = _CHECK.match(reason or "")
    return m.group(1) if m and m.group(1) in names else ""


def normalize(reason: str, ids: set[str] = frozenset()) -> str:
    """One key per kind of failure: ids, every path after the first and numbers stripped. Backticked commands are
    dropped first, so the first path kept is the first file the failure names, not the check's own script."""
    s = re.sub(r"`[^`]*`", "`cmd`", (reason or "")[:600])
    seen = [False]

    def path(m: re.Match) -> str:
        if seen[0]:
            return "<path>"
        seen[0] = True
        return m.group(1).replace("\\", "/")
    s = _PATH.sub(path, s)
    s = re.sub(r"\b(?:fn/|depfix-|probe-|polish-)[\w.-]+", "<id>", s)
    for fid in sorted(ids, key=len, reverse=True):
        s = re.sub(rf"(?<![\w-]){re.escape(fid)}(?![\w-])", "<id>", s)
    s = re.sub(r"\d+", "#", s)
    return re.sub(r"\s+", " ", s).strip()[:200]


class Window:
    """Events (key, who) in a sliding time window; `add` returns the distinct who-count for the key."""

    def __init__(self, minutes: float, clock=time.monotonic):
        self.span, self.clock, self.items = minutes * 60, clock, deque()

    def add(self, key: str, who: str) -> int:
        now = self.clock()
        self.items.append((now, key, who))
        while self.items and now - self.items[0][0] > self.span:
            self.items.popleft()
        return len({w for _, k, w in self.items if k == key})


def failing_files(output: str, tracked: set[str]) -> list[str]:
    """The tracked files a check's output names, in order of first mention (a path may carry :line)."""
    out: list[str] = []
    for m in _FILE.finditer(output or ""):
        p = m.group(1).replace("\\", "/")
        p = p[2:] if p.startswith("./") else p
        if p in tracked and p not in out:
            out.append(p)
    return out


def plan_only(repo: Path, rev: str, path: str) -> bool:
    """True when every commit on rev that touched path is a plan or m0 commit (a pure plan stub)."""
    r = subprocess.run(["git", "log", "--format=%s", rev, "--", path], cwd=repo, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    subjects = [s for s in r.stdout.splitlines() if s.strip()]
    return r.returncode == 0 and bool(subjects) and all(PLAN_SUBJECT.match(s) for s in subjects)


def first_lines(text: str, n: int = 3) -> str:
    return " / ".join([ln.strip() for ln in (text or "").splitlines() if ln.strip()][:n])


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class MainRed:
    """Driver mixin: main-red detection, the systemic-failure detector and the dead-depfix detector. drive.py calls
    red_init, red_fail (top of fail), red_hold (launch and land gate), red_step (each loop), red_depfix (top of
    depfix_settled), red_dead_block (blocked_by) and red_done / red_stop_reason (exit)."""

    def red_init(self, har: dict) -> None:
        win = float(har.get("systemic_window_min", 30) or 0)
        self.red_win_min = win
        self.red_window = Window(win) if win > 0 else None
        self.red_dead = Window(win) if win > 0 else None
        self.red_dead_modules: set[str] = set()
        self.red_pending: tuple[str, str, str] | None = None  # (systemic key, its check, one raw reason)
        self.red_cache: dict[tuple[str, str], tuple[bool, str]] = {}  # (main sha, check) -> (ok, output)
        self.red_review: set[str] = set()  # waiting before review: on green they go to review, not a rebuild
        self.red_stop_reason = ""

    # ---- main red ----
    def red_names(self) -> set[str]:
        return {n for n, _ in self.gate_list(land.WHOLE_TREE)} | set(land.WHOLE_TREE)

    def main_sha(self) -> str:
        r = subprocess.run(["git", "rev-parse", self.main], cwd=self.repo, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        return r.stdout.strip()

    def main_check(self, name: str) -> tuple[bool, str, str]:
        """One whole-tree check on clean main, cached per main sha: (ok, output, sha). A check that is not configured
        or times out counts as green: only a measured failure makes main red."""
        sha = self.main_sha()
        if (sha, name) not in self.red_cache:
            wt = self.prepare_worktree("main-check")
            subprocess.run(["git", "checkout", "-q", "-f", "--detach", sha], cwd=wt, capture_output=True,
                           stdin=subprocess.DEVNULL)
            if name == "cycles":
                problem = self.cycles_problem(wt)
                res = (not problem, problem)
            else:
                cmd = dict(self.gate_list([name])).get(name)
                p = self.sh(land.fill(cmd), wt, "main-check") if cmd else None
                res = (True, "") if p is None else (land.passes(cmd, p.returncode), p.stdout + p.stderr)
            self.red_cache[(sha, name)] = res
        ok, out = self.red_cache[(sha, name)]
        return ok, out, sha

    def red_fail(self, fid: str, reason: str) -> bool:
        """Top of fail(): a whole-tree check that fails on clean main too is not this function's attempt. It waits
        (before review, or at landing) for main to go green, and main gets one fix. Else feeds the systemic window."""
        if fid not in self.fns:
            return False
        check = check_name(reason, self.red_names())
        if check:
            ok, out, sha = self.main_check(check)
            if not ok:
                landing = reason.startswith("land:")
                if not landing:
                    self.red_review.add(fid)
                self.set(fid, status="landing" if landing else "planned", blocked_on=MAIN_RED,
                         last_reason=f"waiting for main to go green: {check} fails on clean {self.main} {sha[:12]} "
                                     f"too; attempt not counted: {reason}"[:1500])
                self.log(fid, "main_red", f"{check} fails on clean {self.main} {sha[:12]} too; attempt not "
                                          f"counted: {first_lines(out)}"[:1500])
                self.say(f"{fid}: {check} red on clean {self.main} too; attempt not counted")
                self.red_set(check, sha, out)
                return True
        self.red_systemic(fid, reason, check)
        return False

    def red_set(self, check: str, sha: str, out: str) -> None:
        """Main just went red on check: one fix. Pure plan stubs stop the build for a plan fix; else one
        depfix-main-<check> goes to the module owning the first failing file."""
        if self.st.get("main_red") or self.red_stop_reason:
            return
        tracked = set(subprocess.run(["git", "ls-tree", "-r", "--name-only", sha], cwd=self.repo, capture_output=True,
                                     text=True, encoding="utf-8", stdin=subprocess.DEVNULL).stdout.splitlines())
        files = failing_files(out, tracked)
        head = first_lines(out) or "(no output)"
        mr = {"check": check, "sha": sha, "files": files[:10], "reason": head[:600], "fix": "", "t": _now()}
        self.st["main_red"] = mr
        if files and all(plan_only(self.repo, sha, f) for f in files):
            mr["action"] = (f"plan fix: make the plan stubs pass {check} (plan_validate PLAN OK), commit, then "
                            "drive.py --repo R")
            self.log(None, "main_red", f"{check} at {sha[:12]} in plan stubs {' '.join(files[:5])}: {head}"[:1500])
            self.red_stop(f"main red in plan stubs: {check} {' '.join(files[:5])}")
            return
        owned = next(((f, m) for f in files if (m := self.owner_module(f))), None)
        if not owned:
            mr["action"] = f"fix {check} on main in a plan commit; no failing file names a module"
            self.log(None, "main_red", f"{check} at {sha[:12]}, no owned file: {head}"[:1500])
            self.red_stop(f"main red: {check} names no file a module owns: {head}"[:600])
            return
        path, module = owned
        dfid = f"depfix-main-{check}"
        mr.update(fix=dfid, action=f"{dfid} fixes it in {module}; new builds and lands wait for a green main")
        self.log(None, "main_red", f"{check} at {sha[:12]}: {dfid} to {module} for {path}: {head}"[:1500])
        self.save()
        self.start_polish_fix(module, [{"file": path, "line": "?", "rule": f"main red: {check}",
                                        "evidence": f"`{check}` fails on clean {self.main} {sha[:12]} and every "
                                                    f"build fails it: {head}"}], fid=dfid)

    def red_hold(self) -> bool:
        """No new build (and no plain land) while main is red or the build is stopping for the manager."""
        return bool(self.st.get("main_red") or self.red_stop_reason)

    def red_fix_running(self, dfid: str) -> bool:
        return bool(dfid) and (dfid in self.jobs or dfid in self.polish_lands
                               or any(j.fn == dfid for j in self.land_jobs()))

    def red_step(self) -> bool:
        """Each loop: act on a systemic flag, and recheck a red main once it moved (cached per sha)."""
        if self.red_pending and not self.red_stop_reason:
            key, check, reason = self.red_pending
            self.red_pending = None
            if check:
                ok, out, sha = self.main_check(check)
                if not ok:
                    self.red_set(check, sha, out)
                    return True
            node = None if check else NODE_RE.search(reason)  # a whole-tree check green on main still stops
            if node and self.main_test_ok(node.group(0)):
                return self.red_clear_systemic(key, f"{node.group(0)} is green on clean {self.main}")
            self.red_stop(f"systemic {key}")
            return True
        mr = self.st.get("main_red")
        if not mr or self.red_stop_reason or self.red_fix_running(mr.get("fix", "")):
            return False
        sha = self.main_sha()
        if sha == mr["sha"]:
            return False
        ok, out, sha = self.main_check(mr["check"])
        if ok:
            self.red_clear(sha)
        else:
            mr["sha"] = sha
            self.save()
            self.red_stop(f"main red: {mr['check']} still fails at {sha[:12]}: {first_lines(out)}"[:600])
        return True

    def red_clear(self, sha: str) -> None:
        mr = self.st.pop("main_red", None) or {}
        self.log(None, "main_green", f"{mr.get('check', '?')} green on {self.main} {sha[:12]}")
        self.say(f"main green again ({mr.get('check', '?')}); builds and lands resume")
        self.save()
        for fid, rec in list(self.st["functions"].items()):
            if rec.get("blocked_on") != MAIN_RED:
                continue
            self.set(fid, blocked_on=None)
            if rec["status"] == "landing":
                self.land_queue.append(fid)
            elif fid in self.red_review:  # its reviewed-ready branch goes to review, not a rebuild
                self.red_review.discard(fid)
                self.set(fid, status="building")
                self.to_review(_Resume(fid, rec["attempts"] + 1), self.sync_problem(fid))

    def red_stop(self, reason: str) -> None:
        """Stop starting work; drive exits 3 with `needs-plan: <reason>` once what runs has finished."""
        if self.red_stop_reason:
            return
        self.red_stop_reason = reason
        self.log(None, "stop", f"needs-plan: {reason}"[:1500])
        self.say(f"stopping: needs-plan: {reason}")

    def red_done(self) -> bool:
        return bool(self.red_stop_reason) and not self.jobs and not self.land_jobs() and not self.periodic.busy()

    # ---- systemic ----
    def red_clear_systemic(self, key: str, why: str) -> bool:
        """A systemic reason whose check or test is green on clean main is per-branch (or flaky under load): each
        function keeps its own retry and the build goes on (pilot2026-11-10 03:04: a proven test red in 3
        worktrees, green on main 3 of 3, stopped every launch for 57 min)."""
        self.log(None, "systemic_clear", f"{why}; not stopping: {key}"[:1500])
        return True

    def main_test_ok(self, node: str) -> bool:
        """One acceptance test on clean main (the main-check worktree), cached per main sha; True when it passes or
        cannot be run (only a measured failure on main stops the build)."""
        tmpl = self.cfg.get("acceptance") or self.cfg["commands"].get("acceptance")
        if not tmpl:
            return False
        sha = self.main_sha()
        if (sha, node) not in self.red_cache:
            wt = self.prepare_worktree("main-check")
            subprocess.run(["git", "checkout", "-q", "-f", "--detach", sha], cwd=wt, capture_output=True,
                           stdin=subprocess.DEVNULL)
            cmd = land.acceptance_cmds(tmpl, [node])[0][1]
            p = self.sh(cmd, wt, "main-check")
            self.red_cache[(sha, node)] = (p is None or p.returncode == 0, "" if p is None else p.stdout[-2000:])
        return self.red_cache[(sha, node)][0]

    def red_flag(self, key: str, count: int, check: str = "") -> bool:
        """Record a systemic reason in state.json (verdict.py shows it); True the first time."""
        rows = self.st.setdefault("systemic", [])
        row = next((r for r in rows if r.get("reason") == key), None)
        if row:
            row.update(count=max(count, int(row.get("count", 0))), t=_now())
            self.save()
            return False
        rows.append({"reason": key, "count": count, "t": _now(), "check": check})
        self.log(None, "systemic", f"{count} functions in {self.red_win_min:g} min: {key}"[:1500])
        self.say(f"systemic: {count} functions fail the same way: {key}")
        self.save()
        return True

    def red_systemic(self, fid: str, reason: str, check: str) -> None:
        if not self.red_window:
            return
        key = normalize(reason, set(self.fns))
        n = self.red_window.add(key, fid)
        if n >= SYSTEMIC_MIN and self.red_flag(key, n, check) and not self.red_pending:
            self.red_pending = (key, check, reason)

    def red_depfix(self, dfid: str, ok: bool, reason: str) -> bool:
        """Top of depfix_settled: the main fix settling, and a dead depfix ("builder left no committed change") a
        second time for one owner module: its BLOCKED-BY claim is a plan gap, the waiting function parks at once."""
        mr = self.st.get("main_red")
        if mr and dfid == mr.get("fix"):
            if ok:
                mr["sha"] = ""  # red_step rechecks main now
                self.save()
            else:
                self.red_stop(f"main red: {mr['check']}: its fix {dfid} failed: {reason}"[:600])
            return True
        if ok or DEAD_DEPFIX not in reason or not self.red_dead or dfid not in self.polish_meta:
            return False
        module = self.polish_meta[dfid][0]
        if self.red_dead.add(module, dfid) < DEAD_DEPFIX_MIN:
            return False
        self.red_dead_modules.add(module)
        self.red_flag(f"dead depfix in {module}: {DEAD_DEPFIX}", DEAD_DEPFIX_MIN)
        for fid, rec in list(self.st["functions"].items()):
            if rec.get("blocked_on") == dfid:
                self.set(fid, blocked_on=None)
                self.park(fid, f"plan gap: {rec['last_reason'].partition(': ')[2] or rec['last_reason']}")
        return True

    def red_dead_block(self, fid: str, module: str, text: str) -> bool:
        """blocked_by: a BLOCKED-BY on a module whose depfixes died twice this window parks without a depfix."""
        if module not in self.red_dead_modules:
            return False
        self.log(fid, "blocked_by", f"{text} in {module}: plan gap (dead depfixes in {module})"[:1500])
        self.park(fid, f"plan gap: {text}")
        return True


class _Resume:
    """What to_review reads from a job: fn and attempt."""

    def __init__(self, fn: str, attempt: int):
        self.fn, self.attempt = fn, attempt
