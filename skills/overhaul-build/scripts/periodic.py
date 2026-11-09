"""periodic.py: gates that are too slow for every land, run on main in the background, with bisect on red.

overhaul.toml (all optional; without [gates.periodic] nothing changes):
  [gates.periodic]
  every_lands = 10          # run after this many lands since the last run ...
  every_min = 120           # ... or this many minutes, if anything landed; and once more when the build is done
  timeout_min = 60          # per gate command
  bisect = true             # red: bisect the main commits since the last green, culprit's module gets one depfix
  budgets = "spec/BUDGETS.toml"
  [gates.periodic.commands]  # name = command, run in .overhaul/periodic (a detached worktree at {sha})
  parity = "py -3 {scripts}/periodic.py ci --workflow parity.yml --sha {sha}"   # X2: Windows/macOS/Linux matrix
  scale = "cargo test --release --test scale -- --test-threads=1 --nocapture"   # the full scale tier
  gpu = "ssh remote-host \"cd app && git fetch -q && git checkout -q {sha} && cargo bench --bench gpu\""
Placeholders: {sha}, {sha12}, {repo}, {scripts}. A gate is red when its command exits non-zero or a line it prints,
`BENCH <name> <os> <value>`, is over budget. Every BENCH line goes to .overhaul/bench.jsonl.
BUDGETS.toml (D9): `ci_factor = 2.0`, `reference = "desk"` (the os label whose numbers are the budget itself),
`record_only = ["macos"]` (recorded, never gated) and `[budgets] <name> = <value>`: lower is better; a value
over budget x (1 on the reference, else ci_factor) fails. Unknown names are recorded only.
State: state.json "periodic" {last_green, lands, last_run, red: {gate: {sha, reason, culprit}}, bisect, fixed}.
  py -3 periodic.py ci --workflow W --sha S [--timeout-min 90]   push S to overhaul-periodic/<sha12>, run W there, wait,
                                                                print its BENCH lines; exit 0 only on success
Python standard library only.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

BENCH = re.compile(r"^\s*BENCH\s+(\S+)\s+(\S+)\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\b", re.M)
LAND_MSG = re.compile(r"^(?:land|polish)\s+([A-Za-z0-9][A-Za-z0-9._-]*):")
DEFAULTS = {"every_lands": 10, "every_min": 120, "timeout_min": 60, "bisect": True, "budgets": "", "commands": {}}


def config(raw: dict) -> dict:
    """[gates.periodic] with defaults, or {} when there is none or it has no commands."""
    g = ((raw.get("gates") or {}).get("periodic")) if isinstance(raw.get("gates"), dict) else None
    if not isinstance(g, dict):
        return {}
    cfg = {**DEFAULTS, **g}
    cmds = cfg["commands"] if isinstance(cfg["commands"], dict) else {}
    cfg["commands"] = {str(k): str(v) for k, v in cmds.items() if str(v).strip()}
    return cfg if cfg["commands"] else {}


def benches(text: str) -> list[tuple[str, str, float]]:
    return [(m.group(1), m.group(2).lower(), float(m.group(3))) for m in BENCH.finditer(text or "")]


def load_budgets(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def judge_bench(name: str, os_: str, value: float, b: dict) -> tuple[float | None, bool]:
    """(the limit this value is gated at or None, ok): x1 on the reference os, x ci_factor elsewhere."""
    budget = (b.get("budgets") or {}).get(name)
    if not isinstance(budget, (int, float)) or os_ in [str(x).lower() for x in b.get("record_only") or []]:
        return None, True
    factor = 1.0 if os_ == str(b.get("reference") or "").lower() else float(b.get("ci_factor") or 2.0)
    limit = float(budget) * factor
    return limit, value <= limit


def next_mid(lo: int, hi: int) -> int | None:
    """Bisect over candidates: lo is the last known good index (-1 = the last green), hi the first known bad one."""
    return None if hi - lo <= 1 else (lo + hi) // 2


def culprit_fid(message: str) -> str:
    """The function or fix id of a land.py commit subject (`land F012: proven`), else ''."""
    m = LAND_MSG.match(message or "")
    return m.group(1) if m else ""


class Periodic:
    """Owned by a drive.Driver: schedules the gates, runs one command at a time in the background, bisects reds."""

    def __init__(self, drv, cfg: dict):
        self.drv, self.cfg = drv, cfg
        self.job = None  # (proc, gate, sha, out path, deadline, mode) mode = "run" | "bisect"
        self.queue: list[str] = []  # gates left in the current full run
        self.run_sha = ""
        self.run_red: list[str] = []

    @property
    def st(self) -> dict:
        st = self.drv.st.setdefault("periodic", {})
        for k, v in (("last_green", ""), ("lands", 0), ("last_run", 0.0), ("red", {}), ("bisect", None),
                     ("fixed", [])):
            st.setdefault(k, v)
        return st

    def landed(self) -> None:
        if self.cfg:
            self.st["lands"] += 1

    def busy(self) -> bool:
        return bool(self.cfg) and bool(self.job or self.queue or self.st["bisect"])

    def due(self, idle: bool) -> bool:
        st = self.st
        if not st["lands"]:
            return False
        mins = (time.time() - float(st["last_run"] or 0)) / 60
        return idle or st["lands"] >= int(self.cfg["every_lands"]) or mins >= float(self.cfg["every_min"])

    def step(self, idle: bool = False) -> bool:
        """One poll; idle = nothing else is in flight (the build is done but main has untested lands)."""
        if not self.cfg:
            return False
        if self.job:
            return self.poll()
        if self.queue:
            return self.start(self.queue.pop(0), self.run_sha, "run")
        if self.run_sha:
            return self.end_run()
        b = self.st["bisect"]
        if b:
            mid = next_mid(b["lo"], b["hi"])
            if mid is None:
                return self.blame(b["gate"], b["cands"][b["hi"]])
            return self.start(b["gate"], b["cands"][mid], "bisect")
        if self.due(idle):
            sha = self.drv_git("rev-parse", self.drv.main)
            if not sha:
                return False
            self.st.update(lands=0, last_run=time.time())
            self.run_sha, self.run_red, self.queue = sha, [], list(self.cfg["commands"])
            return self.start(self.queue.pop(0), sha, "run")
        return False

    def drv_git(self, *args: str) -> str:
        r = subprocess.run(["git", *args], cwd=self.drv.repo, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        return r.stdout.strip() if r.returncode == 0 else ""

    def worktree(self, sha: str) -> Path | None:
        wt = self.drv.ov / "periodic"
        if (wt / ".git").exists():
            ok = subprocess.run(["git", "checkout", "-q", "--detach", "--force", sha], cwd=wt,
                                stdin=subprocess.DEVNULL, capture_output=True).returncode == 0
        else:
            self.drv_git("worktree", "prune")
            ok = bool(subprocess.run(["git", "worktree", "add", "-q", "--detach", str(wt), sha], cwd=self.drv.repo,
                                     stdin=subprocess.DEVNULL, capture_output=True).returncode == 0)
        return wt if ok else None

    def start(self, gate: str, sha: str, mode: str) -> bool:
        wt = self.worktree(sha)
        out = self.drv.ov / "runs" / f"periodic-{gate}-{sha[:12]}.out"
        out.parent.mkdir(parents=True, exist_ok=True)
        if wt is None:
            out.write_text(f"cannot check out {sha} in .overhaul/periodic", encoding="utf-8")
            self.finish(gate, sha, mode, 1, out.read_text(encoding="utf-8"))
            return True
        cmd = self.cfg["commands"][gate]
        for k, v in (("{sha12}", sha[:12]), ("{sha}", sha), ("{repo}", self.drv.repo.as_posix()),
                     ("{scripts}", self.drv.scripts.as_posix())):
            cmd = cmd.replace(k, v)
        with out.open("wb") as fo:
            proc = subprocess.Popen(cmd, shell=True, cwd=wt, stdin=subprocess.DEVNULL, stdout=fo, stderr=subprocess.STDOUT)
        self.job = (proc, gate, sha, out, time.monotonic() + float(self.cfg["timeout_min"]) * 60, mode)
        self.drv.log(None, "periodic_start", f"{gate} {mode} at {sha[:12]}")
        return True

    def poll(self) -> bool:
        import drive
        proc, gate, sha, out, deadline, mode = self.job
        if proc.poll() is None:
            if time.monotonic() < deadline:
                return False
            drive.kill_tree(proc)
            proc.wait()
            code, text = 124, f"timed out after {self.cfg['timeout_min']} min"
        else:
            code, text = proc.returncode, ""
        self.job = None
        text = (out.read_text(encoding="utf-8", errors="replace") if out.is_file() else "") + text
        self.finish(gate, sha, mode, code, text)
        return True

    def finish(self, gate: str, sha: str, mode: str, code: int, text: str) -> None:
        """Record the BENCH lines, then judge: exit 0 and every bench within budget is green."""
        b = load_budgets(self.drv.repo / self.cfg["budgets"]) if self.cfg.get("budgets") else {}
        over = []
        with (self.drv.ov / "bench.jsonl").open("a", encoding="utf-8") as fh:
            for name, os_, value in benches(text):
                limit, ok = judge_bench(name, os_, value, b)
                fh.write(json.dumps({"t": time.time(), "gate": gate, "sha": sha, "name": name, "os": os_,
                                     "value": value, "limit": limit, "ok": ok}) + "\n")
                if not ok:
                    over.append(f"{name} {os_} {value:g} > {limit:g}")
        red = code != 0 or bool(over)
        tail = [x for x in text.splitlines() if x.strip()][-1:] or [""]
        reason = (f"over budget: {'; '.join(over)}" if over else f"exit {code}: {tail[0][:160]}") if red else ""
        self.drv.log(None, "periodic_red" if red else "periodic_green", f"{gate} {mode} at {sha[:12]} {reason}"[:1500])
        if mode == "run":
            if red:
                self.run_red.append(gate)
                self.st["red"][gate] = {"sha": sha, "reason": reason, "culprit": ""}
            else:
                self.st["red"].pop(gate, None)
            return
        bis = self.st["bisect"]
        idx = bis["cands"].index(sha)
        bis["hi" if red else "lo"] = idx

    def end_run(self) -> bool:
        """A full run is done: green moves last_green; a newly red gate is bisected over the lands since then."""
        sha, red, self.run_sha = self.run_sha, self.run_red, ""
        st = self.st
        if not red:
            st["last_green"] = sha
            self.drv.say(f"periodic gates green at {sha[:12]}")
            return True
        gate = next((g for g in red if not st["red"][g].get("culprit")), "")
        if not gate or not self.cfg.get("bisect", True):
            return True
        cands = self.drv_git("rev-list", "--first-parent", "--reverse", f"{st['last_green']}..{sha}").split() \
            if st["last_green"] else []
        if not cands:  # no green yet: nothing to bisect against; the red stands for the owner
            st["red"][gate]["culprit"] = f"{sha[:12]} (no earlier green)"
            self.drv.say(f"periodic gate {gate} red at {sha[:12]} with no earlier green: {st['red'][gate]['reason']}")
            return True
        st["bisect"] = {"gate": gate, "cands": cands, "lo": -1, "hi": len(cands) - 1}
        return True

    def blame(self, gate: str, sha: str) -> bool:
        """The first red commit: its function's module gets one depfix per gate; otherwise the red is the owner's."""
        st, self.st["bisect"] = self.st, None
        import drive
        subject = self.drv_git("log", "-1", "--format=%s", sha)
        fid = culprit_fid(subject)
        f = self.drv.fns.get(fid)
        rec = st["red"].setdefault(gate, {"sha": sha, "reason": "", "culprit": ""})
        rec["culprit"] = fid or f"{sha[:12]} {subject[:60]}"
        self.drv.log(fid or None, "periodic_culprit", f"{gate}: {sha[:12]} {subject[:80]}")
        dfid = f"{drive.DEPFIX_PREFIX}periodic-{gate}"
        if f is None or dfid in st["fixed"]:
            self.drv.say(f"periodic gate {gate} red since {rec['culprit']}: {rec['reason']}; the owner decides")
            return True
        st["fixed"].append(dfid)
        self.drv.say(f"periodic gate {gate} red since {fid}: depfix to module {f.module}")
        self.drv.start_polish_fix(f.module, [{"file": (f.files or ["?"])[0], "line": "?",
                                              "rule": f"periodic gate {gate} red since {fid} ({sha[:12]})",
                                              "evidence": f"`{self.cfg['commands'][gate]}`: {rec['reason']}"}],
                                  fid=dfid)
        return True

    def stop(self) -> None:
        if self.job:
            import drive
            drive.kill_tree(self.job[0])
            self.job = None


def gh(*args: str, timeout: float = 120) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def ci(workflow: str, sha: str, timeout_min: float, run=gh, git=None, sleep=time.sleep) -> int:
    """X2 parity: push sha to a branch, dispatch the matrix workflow there, wait for it, print its BENCH lines."""
    ref = f"overhaul-periodic/{sha[:12]}"
    push = git or (lambda *a: subprocess.run(["git", *a], stdin=subprocess.DEVNULL, capture_output=True).returncode)
    if push("push", "-q", "-f", "origin", f"{sha}:refs/heads/{ref}"):
        print(f"ci: cannot push {sha[:12]} to {ref}")
        return 1
    code, out = run("workflow", "run", workflow, "--ref", ref)
    if code:
        print(f"ci: gh workflow run failed: {out.strip()[:200]}")
        return 1
    end = time.monotonic() + timeout_min * 60
    while time.monotonic() < end:
        sleep(30)
        code, out = run("run", "list", "--workflow", workflow, "--branch", ref, "--limit", "1",
                        "--json", "databaseId,status,conclusion,headSha")
        try:
            runs = json.loads(out) if code == 0 else []
        except ValueError:
            runs = []
        if not runs or runs[0].get("headSha", sha) != sha or runs[0].get("status") != "completed":
            continue
        _, log = run("run", "view", str(runs[0]["databaseId"]), "--log", timeout=600)
        for name, os_, value in benches(log):
            print(f"BENCH {name} {os_} {value:g}")
        print(f"ci: {workflow} at {sha[:12]}: {runs[0].get('conclusion')}")
        return 0 if runs[0].get("conclusion") == "success" else 1
    print(f"ci: {workflow} at {sha[:12]} not done after {timeout_min} min")
    return 124


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="periodic gates")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("ci", help="run a CI workflow at a sha and wait for it")
    c.add_argument("--workflow", required=True)
    c.add_argument("--sha", required=True)
    c.add_argument("--timeout-min", type=float, default=90)
    a = ap.parse_args(argv)
    return ci(a.workflow, a.sha, a.timeout_min)


if __name__ == "__main__":
    sys.exit(main())
