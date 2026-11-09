#!/usr/bin/env python3
"""supervise.py: the one launcher of an overhaul build. It runs drive.py and handles every exit itself.

    py -3 supervise.py --repo R [--max-rounds N]

It caps itself first (memcap, `[limits] proc_mem_gb`), so every child (drive.py, plan-fix agents, the integrator,
plan_validate, the tests they run) is capped too. Then, in a loop:
- drive.py in the foreground (output in .overhaul/drive.out);
- exit 0: when every non-deferred row is proven, one blind proof-check (references/proof-check.md), then stop;
- exit 3: one Exit-3 round. Every needs-plan function gets its own `claude -p` plan-fix agent at once (up to
  `[harness] plan_fix_parallel`, default 12; template plan-fix-brief.md), in its own worktree on plan/fix-<id>, with a
  reserved range of new ids so splits never collide. One integrator (plan-fix-merge.md) cherry-picks the branches
  into plan/fixes-r<round>, keeping the phase cap. supervise.py checks PLAN OK itself, fast-forwards main to it (the
  main guard lets plan commits through), runs `plan_validate --run` once and `drive.py --reset` for each fixed id
  that is still a row. A function that already had two plan fixes is never reset again: its agent must replace it
  with new rows. Then drive.py runs again; at most --max-rounds rounds (default 12);
- any other exit: drive.py once more, then stop with its error line.
Never runs the proof-check while functions remain. One line per step goes to .overhaul/supervise.log; everything it
writes is under .overhaul/. Every headless claude it starts gets CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0.

Exit: 0 every function proven and the proof-check found every one REAL; 4 the proof-check found some not REAL (its
line is logged); 3 rounds used up; 1 error (the line is logged).

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
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import drive  # noqa: E402
import memcap  # noqa: E402

PROOF_BRIEF = HERE.parent / "references" / "proof-check.md"
PLAN_SKILL = HERE.parent.parent / "overhaul-plan"
IDS_PER_FIX = 5
BG_WAIT = {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"}  # headless claude waits for its own background work
NUM_ID = re.compile(r"^(.*?)(\d+)$")
POLL_S = 0.5


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
                 validate_cmd: list[str] | None = None, claude_cmd: list[str] | None = None):
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
        self._claude = claude_cmd

    # ---- plumbing ----
    def log(self, msg: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
        self.ov.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def env(self) -> dict[str, str]:
        """The children's environment: never the harness flag (only drive.py and land.py move main's code)."""
        return {**{k: v for k, v in os.environ.items() if k != "OVERHAUL_HARNESS"}, **BG_WAIT}

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
                                   stderr=subprocess.STDOUT, env=self.env(), timeout=timeout)
        except FileNotFoundError as e:
            raise Stop(f"cannot start {cmd[0]}: {e}") from e
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout:.0f}s"
        return p.returncode, last_line(drive.read_text(out))

    def rows(self) -> list[drive.Fn]:
        return drive.parse_functions(self.git("show", f"{self.main}:{self.functions}"))

    def state(self) -> dict:
        try:
            data = json.loads(drive.read_text(self.ov / "state.json") or "{}")
        except ValueError:
            data = {}
        return data.get("functions", {}) if isinstance(data, dict) else {}

    # ---- the loop ----
    def supervise(self) -> int:
        try:
            return self.loop()
        except Stop as e:
            self.log(f"stop: error: {e}")
            return 1

    def loop(self) -> int:
        rounds, failed, n = 0, False, 0
        while True:
            n += 1
            self.log(f"drive start (run {n})")
            rc, tail = self.run([*self.drive_cmd, "--repo", str(self.repo)], self.ov / "drive.out")
            self.log(f"drive exit {rc}: {tail}")
            if rc == 0:
                return self.proof()
            if rc == 3:
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

    def needs_plan(self) -> list[str]:
        active = {f.id for f in self.rows() if f.status != drive.DEFERRED}
        return [fid for fid, rec in self.state().items() if rec.get("status") == "needs-plan" and fid in active]

    # ---- exit 3 ----
    def exit3(self, rnd: int) -> None:
        rows, recs = self.rows(), self.state()
        fixing = self.needs_plan()
        if not fixing:
            raise Stop("drive exit 3 with no needs-plan function (blocked on unproven deps; see drive.out)")
        if self.git("status", "--porcelain", "--untracked-files=no").strip():
            raise Stop(f"{self.repo} has uncommitted changes to tracked files; main cannot fast-forward")
        if (head := self.git("rev-parse", "--abbrev-ref", "HEAD").strip()) != self.main:
            raise Stop(f"{self.repo} is on {head}, not {self.main}; main cannot fast-forward")
        ids = reserve([f.id for f in rows], fixing)
        work = self.ov / "supervise" / f"{datetime.now():%Y%m%d-%H%M%S}-r{rnd}"  # a fresh folder: no stale reports
        work.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "prune", check=False)
        replace = {fid for fid in fixing if int(recs[fid].get("resets", 0)) >= drive.PLAN_FIXES}
        self.log(f"round {rnd}: plan-fix {' '.join(fixing)} ({min(len(fixing), self.parallel)} at once"
                 + (f"; replace {' '.join(sorted(replace))}" if replace else "") + ")")
        jobs = []
        for fid in fixing:
            wt, branch = work / "wt" / fid, f"plan/fix-{fid}"
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
            (fixed if ahead and "PLAN OK" in line1 else skipped).append(fid)
            self.log(f"plan-fix {fid}: {ahead} commit(s); {line1 or 'no report'}")
        if not fixed:
            self.drop_worktrees(work)
            raise Stop(f"round {rnd}: no plan fix succeeded ({' '.join(skipped)})")
        branch = f"plan/fixes-r{rnd}"
        merge_wt = work / "wt" / "_merge"
        self.git("worktree", "add", "-q", "-f", "-B", branch, str(merge_wt), self.main)
        if len(fixed) == 1:  # nothing to integrate: its own branch already passed PLAN OK
            self.git("merge", "-q", "--ff-only", f"plan/fix-{fixed[0]}", cwd=merge_wt)
        else:
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
        self.land_plan(branch, merge_wt, work, rnd)
        self.drop_worktrees(work)
        self.after_land(rows, fixing, fixed, replace)

    def agents(self, jobs: list[tuple[str, Path, Path, Path, str]]) -> None:
        """Run the agents, at most self.parallel at once; each one's output goes to its .json file."""
        base = self.claude()
        waiting, running = list(jobs), []
        while waiting or running:
            while waiting and len(running) < self.parallel:
                tag, cwd, brief, out, model = waiting.pop(0)
                cmd = [*base, "-p", "--dangerously-skip-permissions", "--output-format", "json", "--model", model,
                       f"Read and do {brief.as_posix()}"]
                try:
                    proc = drive.spawn(cmd, cwd, out, self.env())
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

    def land_plan(self, branch: str, merge_wt: Path, work: Path, rnd: int) -> None:
        """Check PLAN OK on the merged branch, fast-forward main to it, then plan_validate --run once on main."""
        if int(self.git("rev-list", "--count", f"{self.main}..{branch}").strip() or 0) == 0:
            raise Stop(f"round {rnd}: {branch} has no commit to land")
        code, line = self.run([*self.validate_cmd, "--repo", str(merge_wt)], work / "validate-static.txt")
        self.log(f"plan_validate {branch}: {line}")
        if code or not line.startswith("PLAN OK"):
            raise Stop(f"round {rnd}: {branch} is not PLAN OK: {line}")
        self.git("merge", "-q", "--ff-only", branch)
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
        cmd = [*self.claude(), "-p", "--dangerously-skip-permissions", "--output-format", "json", "--model",
               self.proof_model, f"Read and do {PROOF_BRIEF.as_posix()} for repo {self.repo.as_posix()}{only}."]
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
        sup = Supervisor(repo, args.max_rounds)
    except drive.DriveError as e:
        print(f"supervise: error: {e}", flush=True)
        return 1
    os.environ.update(BG_WAIT)  # drive.py's builders and reviewers are headless claudes too
    cap = memcap.cap_self(float(sup.cfg["limits"].get("proc_mem_gb") or 0))  # every child inherits it
    sup.log(f"start {repo.as_posix()}" + (f"; {cap}" if cap else ""))
    try:
        sup.claude()
    except Stop as e:
        sup.log(f"stop: error: {e}")
        return 1
    return sup.supervise()


if __name__ == "__main__":
    sys.exit(main())
