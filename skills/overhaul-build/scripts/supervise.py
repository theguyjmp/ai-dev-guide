#!/usr/bin/env python3
"""supervise.py: the one launcher of an overhaul build. It runs drive.py and handles every exit itself.

    py -3 supervise.py --repo R [--max-rounds N]

It caps itself first (memcap, `[limits] proc_mem_gb`), so every child (drive.py, plan-fix agents, the integrator,
plan_validate, the tests they run) is capped too. Then, in a loop:
- drive.py as a child (output in .overhaul/drive.out), its state.json read every LIVE_POLL_S (60 s). Live re-plan:
  while drive runs, needs-plan functions with no plan fix in flight get one live round (the same plan-fix agents and
  integrator as Exit 3, ids L1, L2, ...). Its branch is not run (static plan_validate only while drive runs); under
  .overhaul/lock (no land in between) it is rebased onto main, passes PLAN OK and fast-forwards main; then supervise
  writes .overhaul/requests/reset-<id> per fixed row still active (reload-<round> when none is), which drive applies at
  its next cycle boundary. Critical path first (live_step): a row with `[harness] critical_waiters` rows waiting gets
  its own round at once, the rest one batched round by waiters, ready 0 for 10 min a round for the top-ranked rows;
  no two rounds touch one function, and an Exit-3 round waits for every live round. .overhaul/supervise/round.json
  names the rounds in flight (verdict.py and watchdog.py read it);
- exit 0: when every non-deferred row is proven, one blind proof-check (references/proof-check.md), then stop;
- exit 3 (after a live round in flight finishes; a live round that landed reruns drive.py free): one Exit-3 round. Every needs-plan function gets its own `claude -p` plan-fix agent at once (up to
  `[harness] plan_fix_parallel`, default 12; template plan-fix-brief.md), in its own worktree on plan/fix-<id>, with a
  reserved range of new ids so splits never collide. One integrator (plan-fix-merge.md) cherry-picks the branches
  into plan/fixes-r<round>, keeping the phase cap. supervise.py checks PLAN OK itself, fast-forwards main to it (the
  main guard lets plan commits through), runs `plan_validate --run` once and `drive.py --reset` for each fixed id
  that is still a row. A function that already had two plan fixes is never reset again: its agent must replace it
  with new rows. Then drive.py runs again; at most --max-rounds rounds (default 12);
- any other exit: drive.py once more, then stop with its error line.
- while drive runs, router.py's route rounds: functions drive parked with a plan-class failure (files gap, missing dep,
  contract defect, oversized) get a plan-fix agent at once with the class and evidence in its brief; after an exit 3,
  the reset requests those rounds wrote are applied first (drive --reset), so no Exit-3 round redoes them.
Never runs the proof-check while functions remain. One line per step goes to .overhaul/supervise.log; everything it
writes is under .overhaul/. Every headless claude it starts gets CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0.

Exit: 0 every function proven and the proof-check found every one REAL; 4 the proof-check found some not REAL (its
line is logged); 3 rounds used up; 1 error (the line is logged).

Why live (measured 2026-11-09 15:07 CST on the pilot project): "rate 1.7/h of expected 1.8/h (slots 3, build 10.3m, 10
builds/land)": 7 functions sat needs-plan with most of 79 planned rows behind them, but an Exit-3 round ran only when
drive.py exited, and drive kept running while lands trickled, so those rows waited for hours.
Plan lands are gated: every plan branch (Exit-3 and live rounds) passes plan_land.py (the proven acceptance tests
its files can break, on the merged branch) after the static gates; red stops the round and main never moves.
One supervise per repo: it takes .overhaul/drive.lock (drivelock.py) before anything else, and a second start exits at
once naming the running pid; its drive.py runs under that lock. The memcap job also kills the whole tree (drive, its
builders, plan-fix agents) when supervise ends.

Why (measured 2026-11-09 on the pilot project vc5): 44 proven for 4+ hours while a bash launcher ran one serial headless
manager per exit 3 (25+ min each), planners ran plan_validate --run and 1M-fixture tests outside the memory cap
(30 GB processes, three low-memory reaps), `memcap.py 12 -- bash` picked WSL bash and `-- claude` was not found.
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
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import drivelock  # noqa: E402  one drive per repo
import drive  # noqa: E402
import memcap  # noqa: E402
import plan_land  # noqa: E402  fast_forward: the one way main moves
import router  # noqa: E402
import watchdog  # noqa: E402  5-minute invariants beside the loop
import worker  # noqa: E402

PROOF_BRIEF = HERE.parent / "references" / "proof-check.md"
PLAN_SKILL = HERE.parent.parent / "overhaul-plan"
IDS_PER_FIX = 5
LIVE_RETRIES = 2  # a live round that did not land is tried again this many times per row
BG_WAIT = {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}  # headless claude waits for its own background work
NUM_ID = re.compile(r"^(.*?)(\d+)$")
POLL_S = 0.5
LIVE_POLL_S = 60.0  # how often a running drive's state.json is read for needs-plan functions (no tight loop)
CRITICAL_WAITERS = 10  # [harness] critical_waiters: a needs-plan row with this many rows waiting gets its own round
READY_ZERO_S = 600.0  # ready rows 0 this long: a round for the top-ranked needs-plan rows, tried or not
PLANFIX_REQ = "planfix-"  # watchdog.py's .overhaul/requests/planfix-<id>: a dedicated round for that row
LIVE_LOCK_WAIT_S = 1800.0  # a live plan land waits this long for land.py to release .overhaul/lock


class Stop(Exception):
    """A step that cannot go on: its message is the error line."""


def reserve(ids: list[str], fixing: list[str], per: int = IDS_PER_FIX) -> dict[str, list[str]]:
    """{fid: per new ids} for each function being fixed: after the highest id of the most common prefix, in order,
    never overlapping each other or any existing id."""
    nums = [(m.group(1), m.group(2)) for m in map(NUM_ID.match, ids) if m]
    prefix = Counter(p for p, _ in nums).most_common(1)[0][0] if nums else "F"
    same = [d for p, d in nums if p == prefix]
    top, width = (max(int(d) for d in same), max(len(d) for d in same)) if same else (0, 2)
    taken, out, n = set(ids), {}, top
    for fid in fixing:
        got: list[str] = []
        while len(got) < per:
            n += 1
            if (new := f"{prefix}{str(n).zfill(width)}") not in taken:
                got.append(new)
        out[fid] = got
    return out


class Supervisor:
    def __init__(self, repo: Path, max_rounds: int = 12, drive_cmd: list[str] | None = None,
                 validate_cmd: list[str] | None = None, claude_cmd: list[str] | None = None,
                 plan_land_cmd: list[str] | None = None):
        self.repo = repo
        self.cfg = drive.load_config(repo)
        har = self.cfg["harness"]
        self.main = str(har.get("main_branch", "main"))
        self.functions = self.cfg["paths"]["functions"]
        self.parallel = max(1, int(har.get("plan_fix_parallel") or 12))
        self.fix_model = str(har.get("plan_fix_model") or "opus")
        self.merge_model = str(har.get("plan_merge_model") or "sonnet")  # a merge pass: Sonnet
        self.proof_model = str(har.get("proof_model") or "opus")
        self.agent_timeout_s = float(har.get("plan_fix_timeout_min") or 30) * 60
        self.validate_timeout = int(har.get("validate_timeout_s") or 600)
        self.max_phase = int((self.cfg.get("plan") or {}).get("max_phase_size") or 40)
        self.max_rounds = max_rounds
        self.ov = repo / ".overhaul"
        self.log_path = self.ov / "supervise.log"
        self.drive_cmd = drive_cmd or [sys.executable, str(HERE / "drive.py")]
        self.validate_cmd = validate_cmd or [sys.executable, str(HERE / "plan_validate.py")]
        self.plan_land_cmd = plan_land_cmd or [sys.executable, str(HERE / "plan_land.py")]
        self.test_timeout = float(har.get("plan_land_timeout_s") or 1800)
        self._claude = claude_cmd
        self.critical = max(1, int(har.get("critical_waiters") or CRITICAL_WAITERS))
        self.flight: dict[str, tuple[threading.Thread, list[str], str]] = {}  # live rounds: label -> (thread, ids, kind)
        self.rounds: dict[str, dict] = {}  # every round in flight (live or Exit 3): label -> its round.json entry
        self.reserved: dict[str, list[str]] = {}  # label -> new ids its agents may use (no two rounds share one)
        self.flight_lock = threading.Lock()
        self.live_starts: dict[str, tuple[int, int]] = {}  # fid -> (resets, live rounds started at that count)
        self.ready_zero_since: float | None = None
        self.live_n = 0
        self.live_landed = False  # a live round landed on main during this drive run
        self.live_tried: dict[str, int] = {}  # fid -> its resets count at its last live try
        self.live_fails: dict[str, int] = {}  # fid -> live rounds that did not land (live_retry)

    # ---- plumbing ----
    def log(self, msg: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
        self.ov.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def env(self) -> dict[str, str]:
        """The children's environment: never the harness flag (only drive.py and land.py move main's code)."""
        return {**{k: v for k, v in os.environ.items() if k not in ("OVERHAUL_HARNESS", drivelock.ENV)}, **BG_WAIT}

    def child_env(self, cmd: list[str]) -> dict[str, str]:
        """env(), plus the drive lock for drive.py only: it runs under supervise's lock, every other child does not."""
        env = self.env()
        if cmd[:len(self.drive_cmd)] == self.drive_cmd and os.environ.get(drivelock.ENV):
            env[drivelock.ENV] = os.environ[drivelock.ENV]
        return env

    def claude(self) -> list[str]:
        if self._claude is None:
            try:
                self._claude = memcap.resolve(["claude"])
            except memcap.NotFound as e:
                raise Stop(f"claude CLI: {e}") from e
        return self._claude

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        p = subprocess.run(["git", *args], check=False, cwd=cwd or self.repo, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL, env=self.env())
        if check and p.returncode:
            raise Stop(f"git {' '.join(args[:3])}: {last_line(p.stderr + p.stdout)}")
        return p.stdout

    def run(self, cmd: list[str], out: Path, cwd: Path | None = None, timeout: float | None = None) -> tuple[int, str]:
        """cmd in the foreground, output to out; (exit code, last line)."""
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(out, "wb") as fo:
                p = subprocess.run(cmd, check=False, cwd=cwd or self.repo, stdin=subprocess.DEVNULL, stdout=fo,
                                   stderr=subprocess.STDOUT, env=worker.env(cmd, self.child_env(cmd)), timeout=timeout)
        except FileNotFoundError as e:
            raise Stop(f"cannot start {cmd[0]}: {e}") from e
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout:.0f}s"
        return p.returncode, last_line(drive.read_text(out))

    def rows(self) -> list[drive.Fn]:
        return drive.parse_functions(self.git("show", f"{self.main}:{self.functions}"))

    def state_doc(self) -> dict:
        try:
            data = json.loads(drive.read_text(self.ov / "state.json") or "{}")
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def state(self) -> dict:
        return self.state_doc().get("functions", {})

    # ---- the loop ----
    def supervise(self) -> int:
        """The loop, with watchdog.py checking the build's invariants every 5 min beside it ([harness] watchdog)."""
        dog = watchdog.start(self.repo, self.log) if self.cfg["harness"].get("watchdog", True) else None
        try:
            return self.loop()
        except Stop as e:
            self.log(f"stop: error: {e}")
            return 1
        finally:
            if dog:
                dog.set()

    def loop(self) -> int:
        rounds, failed, n = 0, False, 0
        while True:
            n += 1
            self.log(f"drive start (run {n})")
            with router.watching(self):  # routed plan fixes (router.py) while drive runs; defers to live rounds
                rc, tail = self.run_drive()
            self.log(f"drive exit {rc}: {tail}")
            if self.flight:
                self.log(f"waiting for the live round(s) in flight: {' '.join(self.flight)}")
                for t, _, _ in list(self.flight.values()):
                    t.join()
                self.flight.clear()
            landed, self.live_landed = self.live_landed, False
            if rc == 0:
                return self.proof()
            if rc == 3 and landed:  # its reset requests wait in .overhaul/requests/: drive applies them at start
                self.log("drive exit 3 after a live plan land: drive again, no Exit-3 round used")
                failed = False
                continue
            if rc == 3:
                if reset := router.apply_requests(self):  # plan fixes landed while drive ran: no Exit-3 round
                    self.log(f"drive exit 3 after routed plan fixes ({' '.join(reset)}): drive again")
                    failed = False
                    continue
                if rounds >= self.max_rounds:
                    self.log(f"stop: {rounds} exit-3 rounds used (--max-rounds {self.max_rounds}); needs-plan: "
                             f"{' '.join(self.needs_plan()) or '-'}")
                    return 3
                rounds += 1
                self.exit3(rounds)
                failed = False
                continue
            if failed:
                raise Stop(f"drive exit {rc} twice: {tail}")
            failed = True

    def run_drive(self) -> tuple[int, str]:
        """drive.py as a child; every LIVE_POLL_S, a live round for needs-plan functions. (exit code, last line)."""
        out = self.ov / "drive.out"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as fo:
            try:
                proc = subprocess.Popen([*self.drive_cmd, "--repo", str(self.repo)], cwd=self.repo,
                                        stdin=subprocess.DEVNULL, stdout=fo, stderr=subprocess.STDOUT,
                                        env=self.child_env(self.drive_cmd))  # drive runs under our lock
            except OSError as e:
                raise Stop(f"cannot start {self.drive_cmd[0]}: {e}") from e
            try:
                while True:
                    try:
                        proc.wait(timeout=LIVE_POLL_S)
                        break
                    except subprocess.TimeoutExpired:
                        self.live_step()
            except BaseException:
                drive.kill_tree(proc)
                raise
        return proc.returncode, last_line(drive.read_text(out))

    def live_step(self) -> None:
        """Start live rounds while drive runs, critical path first (pilot 2026-11-10: Q09 blocked 80 rows and sat
        needs-plan for hours while live rounds fixed low-value rows, and ready rows were 0 most of the afternoon):
        - a needs-plan row with >= critical_waiters not-proven rows waiting on it (drive.waiters) gets its own round
          at once, beside any round in flight; so does a row the watchdog asks for (requests/planfix-<id>);
        - the rest not tried at their reset count go in one batched round, most waiters first;
        - ready rows 0 for READY_ZERO_S: one round for the top-ranked needs-plan rows up to plan_fix_parallel,
          whatever their live_tried state.
        A row is in one round at a time, never while its reset request waits for drive, and at most LIVE_RETRIES + 1
        live rounds start for it per reset count."""
        with self.flight_lock:
            self.flight = {k: v for k, v in self.flight.items() if v[0].is_alive()}
            busy = {fid for _, ids, _ in self.flight.values() for fid in ids}
            kinds = Counter(kind for _, _, kind in self.flight.values())
        try:
            rows, doc = self.rows(), self.state_doc()
        except Stop:
            return  # main unreadable for a moment: the next poll tries again
        recs = doc.get("functions") or {}
        active = [f for f in rows if f.status != drive.DEFERRED]
        fns = {f.id: f for f in active}
        req = self.ov / drive.REQUESTS_DIR
        pending = {p.name[len("reset-"):] for p in req.glob("reset-*")} if req.is_dir() else set()
        wait = drive.waiters(active, recs)
        needs = sorted((fid for fid, r in recs.items() if fid in fns and r.get("status") == "needs-plan"),
                       key=lambda fid: drive.rank_key(fid, fns, wait))
        resets = {fid: int(recs[fid].get("resets", 0)) for fid in needs}
        free = [fid for fid in needs if fid not in busy and fid not in pending and self.starts_left(fid, resets[fid])]
        untried = [fid for fid in free if self.live_tried.get(fid) != resets[fid]]
        asked = self.planfix_requests(needs, busy | pending, free)
        for fid in free:
            if fid in asked or (fid in untried and wait.get(fid, 0) >= self.critical
                                and kinds["critical"] < self.parallel):
                self.start_live([fid], resets, "critical",
                                f"{wait.get(fid, 0)} rows wait" + ("; watchdog request" if fid in asked else ""))
                kinds["critical"] += 1
                busy.add(fid)
        ready = (doc.get("width") or {}).get("ready")
        if ready is None or int(ready) > 0 or not needs:
            self.ready_zero_since = None
        elif self.ready_zero_since is None:
            self.ready_zero_since = time.time()
        zero = time.time() - self.ready_zero_since if self.ready_zero_since is not None else 0.0
        if zero >= READY_ZERO_S and not kinds["starved"]:
            if top := [fid for fid in free if fid not in busy][:self.parallel]:
                self.start_live(top, resets, "starved", f"ready 0 for {zero / 60:.0f} min")
                busy.update(top)
        if not kinds["batch"] and (rest := [fid for fid in untried if fid not in busy]):
            self.start_live(rest, resets, "batch", "")

    def starts_left(self, fid: str, resets: int) -> bool:
        """The retry cap: at most LIVE_RETRIES + 1 live rounds for fid per reset count."""
        at, n = self.live_starts.get(fid, (resets, 0))
        return at != resets or n <= LIVE_RETRIES

    def planfix_requests(self, needs: list[str], covered: set[str], free: list[str]) -> set[str]:
        """The rows of watchdog.py's requests/planfix-<id> that get a round now; every request file is consumed."""
        req = self.ov / drive.REQUESTS_DIR
        out: set[str] = set()
        for p in sorted(req.glob(PLANFIX_REQ + "*")) if req.is_dir() else []:
            fid = p.name[len(PLANFIX_REQ):]
            if fid in free:
                out.add(fid)
            else:
                self.log(f"request {p.name}: " + ("not needs-plan" if fid not in needs else "a round is in flight"
                                                  if fid in covered else "live retry cap used; left to Exit 3"))
            p.unlink(missing_ok=True)
        return out

    def start_live(self, fixing: list[str], resets: dict[str, int], kind: str, why: str) -> None:
        for fid in fixing:
            self.live_tried[fid] = resets[fid]
            at, n = self.live_starts.get(fid, (resets[fid], 0))
            self.live_starts[fid] = (resets[fid], n + 1 if at == resets[fid] else 1)
        self.live_n += 1
        label = f"L{self.live_n}"
        t = threading.Thread(target=self.live_round, args=(label, fixing), daemon=True)
        with self.flight_lock:
            self.flight[label] = (t, list(fixing), kind)
        self.log(f"live round {label} ({kind}{': ' + why if why else ''}): {' '.join(fixing)}")
        t.start()

    def live_retry(self, fixing: list[str]) -> list[str]:
        """A live round that did not land (main moved, a dirty checkout, a refused fast-forward) leaves its rows
        untried, so the next poll starts them again, up to LIVE_RETRIES times per row (pilot 2026-11-10 07:18: one
        failed land left 36 rows needs-plan with no round for the rest of the run)."""
        out = []
        for fid in fixing:
            n = self.live_fails.get(fid, 0) + 1
            self.live_fails[fid] = n
            if n <= LIVE_RETRIES:
                self.live_tried.pop(fid, None)
                out.append(fid)
        return out

    def live_round(self, label: str, fixing: list[str]) -> None:
        """One live round while drive runs; a failure is logged and the functions stay needs-plan for Exit 3."""
        work = self.ov / "supervise" / f"{datetime.now():%Y%m%d-%H%M%S}-{label}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            rows, recs = self.rows(), self.state()
            self.round_file(label, fixing)
            branch, merge_wt, fixed, _ = self.fix_round(label, fixing, rows, recs, work)
            self.land_live(branch, merge_wt, work, label)
            active = {f.id for f in self.rows() if f.status != drive.DEFERRED}
            new = [f.id for f in self.rows() if f.id not in {r.id for r in rows}]
            req = self.ov / drive.REQUESTS_DIR
            req.mkdir(parents=True, exist_ok=True)
            resets = [fid for fid in fixed if fid in active]
            for fid in resets:
                (req / f"reset-{fid}").write_text(f"{label}\n", encoding="utf-8")
            if not resets:  # every fixed row was replaced: drive must still reload the plan
                (req / f"reload-{label}").write_text(f"{label}\n", encoding="utf-8")
            self.live_landed = True
            self.log(f"live round {label}: landed; reset requests {' '.join(resets) or '-'}"
                     + (f"; new rows {' '.join(new)}" if new else "")
                     + "".join(f"; {fid}: replaced" for fid in fixed if fid not in active))
        except Stop as e:
            retry = self.live_retry(fixing)
            self.log(f"live round {label}: {e}; " + (f"retried next poll: {' '.join(retry)}" if retry
                                                     else "still needs-plan for an Exit-3 round"))
        except Exception as e:  # never kill the supervisor from the side thread
            retry = self.live_retry(fixing)  # a crash (WinError 8, the pilot project 2026-11-10 13:43) retries like a failed land
            self.log(f"live round {label}: error: {type(e).__name__}: {e}"
                     + (f"; retried next poll: {' '.join(retry)}" if retry else ""))
        finally:
            self.drop_worktrees(work)
            self.round_file(label, done=True)

    def round_file(self, label: str, ids: list[str] | None = None, done: bool = False) -> None:
        """.overhaul/supervise/round.json names the plan-fix rounds in flight (verdict.py and watchdog.py read it):
        round = their labels joined by '+', ids = every row they fix; removed when none is. done: label finished,
        its reserved ids freed."""
        path = self.ov / "supervise" / "round.json"
        with self.flight_lock:
            if done:
                self.rounds.pop(label, None)
                self.reserved.pop(label, None)
            else:
                self.rounds[label] = {"ids": list(ids or []), "started": datetime.now().isoformat(timespec="seconds")}
            if not self.rounds:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"round": "+".join(self.rounds),
                                        "ids": [i for r in self.rounds.values() for i in r["ids"]],
                                        "started": min(r["started"] for r in self.rounds.values()),
                                        "rounds": self.rounds}), encoding="utf-8")

    def land_live(self, branch: str, merge_wt: Path, work: Path, label: str) -> None:
        """Rebased onto main: dup/literals and the proven tests it touches (plan_land.py) outside the lock; then under
        .overhaul/lock: rebase again, static PLAN OK (no --run while drive runs), the proven tests again only when a
        land moved main meanwhile, fast-forward main."""
        if int(self.git("rev-list", "--count", f"{self.main}..{branch}").strip() or 0) == 0:
            raise Stop(f"{branch} has no commit to land")
        self.rebase(branch, merge_wt)  # the slow checks run before the lock, so lands keep landing meanwhile
        self.static_gates(branch, merge_wt, work, label)
        self.plan_tests(branch, merge_wt, work, label)
        checked = self.git("rev-parse", self.main).strip()
        if not drive.acquire_lock(self.repo, LIVE_LOCK_WAIT_S, "supervise.py live plan land"):
            raise Stop(f".overhaul/lock held by {drive.lock_holder(self.repo) or '?'} for {LIVE_LOCK_WAIT_S:.0f}s")
        try:  # no land moves main while the plan is rebased, checked and fast-forwarded
            self.check_main()
            self.rebase(branch, merge_wt)
            code, line = self.run([*self.validate_cmd, "--repo", str(merge_wt)], work / "validate-static.txt")
            if code or not line.startswith("PLAN OK"):
                raise Stop(f"{branch} on {self.main} is not PLAN OK: {line}")
            if self.git("rev-parse", self.main).strip() != checked:
                self.plan_tests(branch, merge_wt, work, label)
            self.fast_forward(branch)
        finally:
            drive.release_lock(self.repo)
        self.log(f"main -> {branch} {self.git('rev-parse', '--short=12', self.main).strip()} (live)")

    def fast_forward(self, branch: str) -> None:
        """main -> branch via plan_land.fast_forward: a refusal (the guard, local changes) leaves main and the
        checkout as they were and stops the round."""
        ok, why = plan_land.fast_forward(self.repo, branch, self.main, harness=True)
        if not ok:
            raise Stop(f"{branch}: fast-forward {why}")

    def rebase(self, branch: str, merge_wt: Path) -> None:
        try:
            self.git("rebase", "-q", self.main, cwd=merge_wt)
        except Stop as e:
            if self.index_only_conflicts(merge_wt):
                return
            self.git("rebase", "--abort", cwd=merge_wt, check=False)
            raise Stop(f"{branch} does not rebase onto {self.main} cleanly: {e}") from e

    def index_only_conflicts(self, merge_wt: Path) -> bool:
        """A rebase stopped only on the generated INDEX files (plan_land.py's regenerate commit vs a land's): take
        main's copy and go on; the plan_tests rerun after main moved regenerates them. True when the rebase finished."""
        for _ in range(20):
            bad = [f for f in self.git("diff", "--name-only", "--diff-filter=U", cwd=merge_wt,
                                       check=False).splitlines() if f]
            if not bad or not all(f.startswith("spec/index/") or f == "spec/INDEX.md" for f in bad):
                return False
            self.git("checkout", "--ours", "--", *bad, cwd=merge_wt, check=False)
            self.git("add", "--", *bad, cwd=merge_wt, check=False)
            try:  # main's copy alone left nothing to commit: skip the regenerate commit
                if not self.git("diff", "--cached", "--name-only", cwd=merge_wt, check=False).strip():
                    self.git("rebase", "--skip", cwd=merge_wt)
                else:
                    self.git("-c", "core.editor=true", "rebase", "--continue", cwd=merge_wt)
                return True
            except Stop:
                continue
        return False

    def plan_tests(self, branch: str, merge_wt: Path, work: Path, rnd: int | str) -> None:
        """plan_land.py --tests-only on the merged branch: the proven acceptance tests the plan commit can break.
        Red: Stop, main never moves (pilot 2026-11-10: phase-10 stubs failed proven tests, main red 03:30-05:20)."""
        code, line = self.run([*self.plan_land_cmd, "--repo", str(self.repo), "--branch", branch, "--main", self.main,
                               "--wt", str(merge_wt), "--tests-only", "--timeout", f"{self.test_timeout:.0f}"],
                              work / "plan-land.txt")
        self.log(f"plan_land {branch}: {line}")
        if code:
            raise Stop(f"round {rnd}: {branch} fails proven tests (main would go red): {line}")

    def check_main(self) -> None:
        if self.git("status", "--porcelain", "--untracked-files=no").strip():
            raise Stop(f"{self.repo} has uncommitted changes to tracked files; main cannot fast-forward")
        if (head := self.git("rev-parse", "--abbrev-ref", "HEAD").strip()) != self.main:
            raise Stop(f"{self.repo} is on {head}, not {self.main}; main cannot fast-forward")

    def needs_plan(self) -> list[str]:
        active = {f.id for f in self.rows() if f.status != drive.DEFERRED}
        return [fid for fid, rec in self.state().items() if rec.get("status") == "needs-plan" and fid in active]

    # ---- exit 3 ----
    def exit3(self, rnd: int) -> None:
        rows, recs = self.rows(), self.state()
        fixing = self.needs_plan()
        if not fixing:
            try:
                said = [ln.strip() for ln in (self.ov / "drive.out").read_text(encoding="utf-8", errors="replace")
                        .splitlines() if "needs-plan:" in ln]
            except OSError:
                said = []
            # main red / systemic stops (mainred.py) name no function: hand the manager drive's own line
            raise Stop(f"drive exit 3 with no needs-plan function: {said[-1]}" if said else
                       "drive exit 3 with no needs-plan function (blocked on unproven deps; see drive.out)")
        self.check_main()
        work = self.ov / "supervise" / f"{datetime.now():%Y%m%d-%H%M%S}-r{rnd}"  # a fresh folder: no stale reports
        work.mkdir(parents=True, exist_ok=True)
        self.round_file(str(rnd), fixing)
        try:
            branch, merge_wt, fixed, replace = self.fix_round(str(rnd), fixing, rows, recs, work)
            self.land_plan(branch, merge_wt, work, rnd)
            self.drop_worktrees(work)
        finally:
            self.round_file(str(rnd), done=True)
        self.after_land(rows, fixing, fixed, replace)

    def fix_round(self, rnd: str, fixing: list[str], rows: list[drive.Fn], recs: dict,
                  work: Path) -> tuple[str, Path, list[str], set[str]]:
        """The plan-fix agents (in parallel) and the integrator: (plan branch, its worktree, fixed ids, replace)."""
        with self.flight_lock:  # rounds run side by side: never reserve another round's new ids
            ids = reserve([f.id for f in rows] + [i for v in self.reserved.values() for i in v], fixing)
            self.reserved[rnd] = [i for v in ids.values() for i in v]
        self.git("worktree", "prune", check=False)
        replace = {fid for fid in fixing if int(recs[fid].get("resets", 0)) >= drive.PLAN_FIXES}
        self.log(f"round {rnd}: plan-fix {' '.join(fixing)} ({min(len(fixing), self.parallel)} at once"
                 + (f"; replace {' '.join(sorted(replace))}" if replace else "") + ")")
        jobs = []
        for fid in fixing:
            wt, branch = work / "wt" / fid, f"plan/fix-{fid}"
            self.free_branch(branch)
            self.git("worktree", "add", "-q", "-f", "-B", branch, str(wt), self.main)
            report = work / f"plan-fix-{fid}.md"
            mode = ("This function already had two plan fixes and is never reset again: replace it. Split it into new "
                    "rows with the reserved ids (or one new row with a new approach), and turn its own row into "
                    "`deferred` naming them; a third tweak of the same test or row is not allowed."
                    if fid in replace else "")
            brief = work / f"brief-{fid}.md"
            brief.write_text(drive.render("plan-fix-brief.md", {
                "id": fid, "round": str(rnd), "skill": HERE.parent.as_posix(), "plan_skill": PLAN_SKILL.as_posix(),
                "wt": wt.as_posix(), "branch": branch, "main": self.main, "repo": self.repo.as_posix(),
                "ids": ", ".join(ids[fid]), "resets": str(recs[fid].get("resets", 0)),
                "reason": (recs[fid].get("last_reason") or "-").replace("\n", " ")[:400],
                "report": report.as_posix(), "functions": f"{wt.as_posix()}/{self.functions}",
                "validate": (HERE / "plan_validate.py").as_posix(), "mode": mode}), encoding="utf-8")
            jobs.append((fid, wt, brief, work / f"plan-fix-{fid}.json", self.fix_model))
        self.agents(jobs)
        fixed, skipped = [], []
        for fid in fixing:
            ahead = int(self.git("rev-list", "--count", f"{self.main}..plan/fix-{fid}").strip() or 0)
            line1 = first_line(drive.read_text(work / f"plan-fix-{fid}.md"))
            split = router.split_ok(router.oversized_reason(recs[fid].get("last_reason") or ""), line1)
            (fixed if ahead and "PLAN OK" in line1 and split else skipped).append(fid)
            self.log(f"plan-fix {fid}: {ahead} commit(s); {line1 or 'no report'}")
        if not fixed:
            self.drop_worktrees(work)
            raise Stop(f"round {rnd}: no plan fix succeeded ({' '.join(skipped)})")
        branch = f"plan/fixes-r{rnd}"
        merge_wt = work / "wt" / "_merge"
        # one fix: nothing to integrate, its own branch already passed PLAN OK (main may have moved since: a live
        # round's lands; land_live rebases it)
        self.free_branch(branch)
        self.git("worktree", "add", "-q", "-f", "-B", branch, str(merge_wt),
                 f"plan/fix-{fixed[0]}" if len(fixed) == 1 else self.main)
        if len(fixed) > 1:
            report = work / "plan-fix-merge.md"
            listing = "\n".join(f"- `plan/fix-{fid}`: reserved {', '.join(ids[fid])}; report "
                                f"`{(work / f'plan-fix-{fid}.md').as_posix()}`" for fid in fixed)
            brief = work / "brief-merge.md"
            brief.write_text(drive.render("plan-fix-merge.md", {
                "round": str(rnd), "wt": merge_wt.as_posix(), "branch": branch, "main": self.main,
                "repo": self.repo.as_posix(), "max_phase": str(self.max_phase), "report": report.as_posix(),
                "branches": listing, "validate": (HERE / "plan_validate.py").as_posix()}), encoding="utf-8")
            self.agents([("merge", merge_wt, brief, work / "plan-fix-merge.json", self.merge_model)])
            self.log(f"integrator: {first_line(drive.read_text(report)) or 'no report'}")
        return branch, merge_wt, fixed, replace

    def agents(self, jobs: list[tuple[str, Path, Path, Path, str]]) -> None:
        """Run the agents, at most self.parallel at once; each one's output goes to its .json file."""
        base = self.claude()
        waiting, running = list(jobs), []
        while waiting or running:
            while waiting and len(running) < self.parallel:
                tag, cwd, brief, out, model = waiting.pop(0)
                cmd = [*base, "-p", "--dangerously-skip-permissions", "--output-format", "json",
                       *worker.lean_flags(base[0], "build"), "--model", model, f"Read and do {brief.as_posix()}"]
                try:
                    proc = drive.spawn(cmd, cwd, out, worker.env(cmd, self.env()))
                except OSError as e:
                    raise Stop(f"cannot start claude for {tag}: {e}") from e
                running.append((tag, proc, time.monotonic() + self.agent_timeout_s))
            time.sleep(POLL_S)
            for job in list(running):
                tag, proc, deadline = job
                if proc.poll() is None and time.monotonic() < deadline:
                    continue
                if proc.poll() is None:
                    drive.kill_tree(proc)
                    self.log(f"agent {tag}: killed after {self.agent_timeout_s / 60:.0f} min")
                elif proc.returncode:
                    self.log(f"agent {tag}: exit {proc.returncode}")
                running.remove(job)

    def free_branch(self, branch: str) -> None:
        """Remove any worktree that still has branch checked out: one supervise runs per repo, so it is a killed
        round's leftover, and `worktree add -B` refuses a branch checked out elsewhere (pilot 2026-11-10: every
        restart left plan/fix-<id> worktrees that failed the next live round for that row)."""
        listing = self.git("worktree", "list", "--porcelain", check=False) or ""
        path = ""
        for line in listing.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):]
            elif line == f"branch refs/heads/{branch}" and path:
                self.git("worktree", "remove", "--force", path, check=False)
                self.log(f"removed stale worktree {path} holding {branch}")
        self.git("worktree", "prune", check=False)

    def land_plan(self, branch: str, merge_wt: Path, work: Path, rnd: int) -> None:
        """land_plan_unlocked under plan_land.py's per-repo lock, so a live round, the manager's plan_land and an
        agent's plan_land never race to fast-forward main (pilot 2026-11-10 22:33: 3 plan lands at once)."""
        repo = str(self.repo)
        if not plan_land.land.acquire_lock(repo, wait=3600.0, owner=f"supervise round {rnd} {branch}",
                                           name=plan_land.LOCK_NAME,
                                           on_wait=lambda who: self.log(f"round {rnd}: waiting for plan_land of {who}")):
            raise Stop(f"round {rnd}: plan_land lock busy for an hour")
        try:
            self.land_plan_unlocked(branch, merge_wt, work, rnd)
        finally:
            plan_land.land.release_lock(repo, name=plan_land.LOCK_NAME)

    def land_plan_unlocked(self, branch: str, merge_wt: Path, work: Path, rnd: int) -> None:
        """Check PLAN OK on the merged branch, fast-forward main to it, then plan_validate --run once on main."""
        if int(self.git("rev-list", "--count", f"{self.main}..{branch}").strip() or 0) == 0:
            raise Stop(f"round {rnd}: {branch} has no commit to land")
        code, line = self.run([*self.validate_cmd, "--repo", str(merge_wt)], work / "validate-static.txt")
        self.log(f"plan_validate {branch}: {line}")
        if code or not line.startswith("PLAN OK"):
            raise Stop(f"round {rnd}: {branch} is not PLAN OK: {line}")
        self.static_gates(branch, merge_wt, work, rnd)
        self.plan_tests(branch, merge_wt, work, rnd)
        self.fast_forward(branch)
        self.log(f"main -> {branch} {self.git('rev-parse', '--short=12', 'HEAD').strip()}")
        out = work / "validate-run.txt"
        code, line = self.run([*self.validate_cmd, "--repo", str(self.repo), "--run",
                               "--timeout", str(self.validate_timeout)], out)
        flaky = [ln for ln in drive.read_text(out).splitlines() if "flaky under load" in ln]
        self.log(f"plan_validate --run: {line}" + (f"; flaky under load: {len(flaky)}" if flaky else ""))
        for ln in flaky:
            self.log(f"  {ln.strip()}")
        if code or not line.startswith("PLAN OK"):
            problems = [ln for ln in drive.read_text(out).splitlines() if ln.strip() and not ln.startswith("PLAN")]
            raise Stop(f"round {rnd}: plan_validate --run on main: {line}"
                       + (f"; first: {problems[0][:300]}" if problems else ""))

    def static_gates(self, branch: str, merge_wt: Path, work: Path, rnd: int | str) -> None:
        """The whole-tree checks that need no build (dup, literals) on the merged plan branch before main moves:
        a plan stub that repeats landed code turned main red and held every land (pilot 2026-11-09 23:08)."""
        cmds = self.cfg.get("commands") or {}
        for name in ("dup", "literals"):
            cmd = cmds.get(name)
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            out = work / f"gate-{name}.txt"
            with open(out, "wb") as fo:
                p = subprocess.run(cmd, shell=True, check=False, cwd=merge_wt, stdin=subprocess.DEVNULL, stdout=fo,
                                   stderr=subprocess.STDOUT)
            text = drive.read_text(out)
            self.log(f"{name} {branch}: exit {p.returncode}")
            if p.returncode:
                first = next((ln for ln in text.splitlines() if ln.strip()), "")
                raise Stop(f"round {rnd}: {branch} fails {name} (main would go red): {first[:300]}")

    def after_land(self, old_rows: list[drive.Fn], fixing: list[str], fixed: list[str], replace: set[str]) -> None:
        rows = self.rows()
        active = {f.id for f in rows if f.status != drive.DEFERRED}
        new = [f.id for f in rows if f.id not in {r.id for r in old_rows}]
        if new:
            self.log(f"new rows: {' '.join(new)}")
        for fid in fixed:
            if fid not in active:
                self.log(f"{fid}: replaced (no longer an active row)")
                continue
            if fid in replace:
                raise Stop(f"{fid} had two plan fixes; its third reset is refused and its fix kept the row instead "
                           "of replacing it with new ids")
            out = self.ov / "supervise" / f"reset-{fid}.txt"
            code, line = self.run([*self.drive_cmd, "--repo", str(self.repo), "--reset", fid], out)
            if code:
                raise Stop(f"drive --reset {fid}: {line}")
            self.log(f"reset {fid}")
        for fid in fixing:
            if fid not in fixed:
                self.log(f"{fid}: no plan fix this round; still needs-plan")

    def drop_worktrees(self, work: Path) -> None:
        wts = work / "wt"
        for wt in sorted(wts.iterdir()) if wts.is_dir() else []:
            self.git("worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wts, ignore_errors=True)
        self.git("worktree", "prune", check=False)

    # ---- exit 0 ----
    def proof(self) -> int:
        left = [f.id for f in self.rows() if f.status != drive.DEFERRED
                and self.state().get(f.id, {}).get("status") != "proven"]
        if left:
            raise Stop(f"drive exit 0 but not proven: {' '.join(left)}; no proof-check while functions remain")
        out = self.ov / "proof-check.txt"
        first = self.proof_pass("", out)
        unchecked = [ln.split()[0] for ln in drive.read_text(out).splitlines()[1:] if " UNCHECKED" in ln]
        if unchecked:
            shutil.copy(out, self.ov / "proof-check-1.txt")
            self.proof_pass(f", only {' '.join(unchecked)}", out)
        lines = drive.read_text(out).splitlines()
        bad = [ln for ln in lines[1:] if ln.strip() and " REAL " not in f"{ln} "]
        if unchecked:  # the second pass judged only the unchecked ones
            bad = [ln for ln in drive.read_text(self.ov / "proof-check-1.txt").splitlines()[1:]
                   if ln.strip() and " REAL " not in f"{ln} " and " UNCHECKED" not in ln] + bad
        self.log(f"proof: {first}" + (f"; recheck {first_line(chr(10).join(lines))}" if unchecked else ""))
        for ln in bad:
            self.log(f"  not REAL: {ln.strip()[:300]}")
        if bad:
            self.log(f"stop: {len(bad)} function(s) not REAL; each goes back through Exit 3 option 2")
            return 4
        self.log("done: every function proven and REAL")
        return 0

    def proof_pass(self, only: str, out: Path) -> str:
        out.unlink(missing_ok=True)
        base = self.claude()
        cmd = [*base, "-p", "--dangerously-skip-permissions", "--output-format", "json",
               *worker.lean_flags(base[0], "proof"), "--model", self.proof_model,
               f"Read and do {PROOF_BRIEF.as_posix()} for repo {self.repo.as_posix()}{only}."]
        self.log("proof-check start" + (f" (only {only[7:]})" if only else ""))
        code, _ = self.run(cmd, self.ov / "supervise" / "proof-check.json", timeout=self.agent_timeout_s * 4)
        line = first_line(drive.read_text(out))
        if not line.startswith("PROOF"):
            raise Stop(f"proof-check wrote no PROOF line (claude exit {code})")
        return line


def first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def last_line(text: str) -> str:
    return next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Run an overhaul build: drive.py, exit-3 plan fixes, proof-check.")
    ap.add_argument("--repo", default=".", help="rebuild repo root (default: cwd)")
    ap.add_argument("--max-rounds", type=int, default=12, help="exit-3 rounds before it stops (default 12)")
    args = ap.parse_args(argv)
    repo = Path(os.path.abspath(args.repo))
    try:
        drivelock.acquire(repo, "supervise.py")  # one drive per repo, first; drive.py runs under this lock
    except drivelock.Held as e:
        print(f"supervise: error: {e}; this start exits, the running one keeps going", flush=True)
        return 1
    try:
        try:
            sup = Supervisor(repo, args.max_rounds)
        except drive.DriveError as e:
            print(f"supervise: error: {e}", flush=True)
            return 1
        os.environ.update(BG_WAIT)  # drive.py's builders and reviewers are headless claudes too
        # every child inherits the cap and dies with supervise (one job, both limits)
        cap = memcap.cap_self(float(sup.cfg["limits"].get("proc_mem_gb") or 0), kill_on_close=True)
        sup.log(f"start {repo.as_posix()}" + (f"; {cap}" if cap else ""))
        try:
            sup.claude()
        except Stop as e:
            sup.log(f"stop: error: {e}")
            return 1
        return sup.supervise()
    finally:
        drivelock.release(repo)


if __name__ == "__main__":
    sys.exit(main())
