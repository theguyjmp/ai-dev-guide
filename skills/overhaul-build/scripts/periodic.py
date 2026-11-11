"""periodic.py: gates that are too slow for every land, run on main in the background, with bisect on red.

overhaul.toml (all optional; without [gates.periodic] nothing changes):
  [gates.periodic]
  every_lands = 10          # run after this many lands since the last run ...
  every_min = 120           # ... or this many minutes, if anything landed; and once more when the build is done
  # Per gate: make every_lands / every_min a table instead; its gates run on their own cadence, the rest on
  # `default` (else 10 / 120), and at the end such a gate runs once only when every function is proven:
  #   [gates.periodic.every_min]
  #   default = 120
  #   parity = 10080          # weekly (a paid CI minute budget), plus once at the finish
  # A gate named in only one of the two tables runs on that one alone.
  timeout_min = 60          # per gate command
  bisect = true             # red: bisect the main commits since that gate's last green; culprit's module, one depfix
  depfix_max = 3            # depfixes per gate while it stays red (one per red run); then the red is the owner's
  budgets = "spec/BUDGETS.toml"
  [gates.periodic.commands]  # name = command, run in .overhaul/periodic (a detached worktree at {sha})
  parity = "py -3 {scripts}/periodic.py ci --workflow parity.yml --sha {sha}"   # X2: Windows/macOS/Linux matrix
  scale = "cargo test --release --test scale -- --test-threads=1 --nocapture"   # the full scale tier
  gpu = "ssh remote-host \"cd repo && git fetch -q && git checkout -q {sha} && cargo bench --bench gpu\""
Placeholders: {sha}, {sha12}, {repo}, {scripts}. A gate is red when its command exits non-zero or a line it prints,
`BENCH <name> <os> <value>`, is over budget. Every BENCH line goes to .overhaul/bench.jsonl.
BUDGETS.toml (D9): `ci_factor = 2.0`, `reference = "desk"` (the os label whose numbers are the budget itself),
`record_only = ["macos"]` (recorded, never gated) and `[budgets] <name> = <value>`: lower is better; a value
over budget x (1 on the reference, else ci_factor) fails. Unknown names are recorded only.
Each gate keeps its own green: a red gate bisects over the lands since ITS last green, so one gate's red never
blocks another's bisect. A gate whose command exits 75 after printing `ci: unavailable: <reason>` (CI that cannot
run: billing, spending limit, unsupported runner) is UNAVAILABLE: never bisected, no depfix, not green; the verdict
shows it as an owner line. A gate that fails as a command, not as code (exit 126/127, `command not found`, a Windows
path in bash) is a CONFIG red: never bisected, no depfix, not green; the verdict shows a `gate_config` owner line.
Every red run acts: a red gate is bisected again each run (its culprit stands only while its depfix is in flight or it
has no green to bisect against) and the culprit's module gets one depfix per red run, up to depfix_max. The culprit is
the function of a `land F:` / `F: ...` commit, else the module that owns the commit's first changed file.
State: state.json "periodic" {green: {gate: sha}, last_green (every gate green; older states seed green from it),
  lands, last_run (the last run of any gate), since: {gate: lands}, ran: {gate: time}, finish: {gate: functions},
  red: {gate: {sha, reason, culprit, depfix, depfixes}}, unavailable: {gate: {sha, reason}},
  config: {gate: {sha, reason}}, bisect, fixed (every periodic depfix id started)}.
  py -3 periodic.py ci --workflow W --sha S [--timeout-min 90]   push S to overhaul-periodic/<sha12>, run W there, wait,
                                                                print its BENCH lines; exit 0 only on success, 75 when
                                                                GitHub never started the jobs (unavailable)
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

import memcap  # beside this script: names a process memory cap hit in a red's reason
import priority  # beside this script: periodic gates run at NORMAL, above the builders

BENCH = re.compile(r"^\s*BENCH\s+(\S+)\s+(\S+)\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\b", re.M)
LAND_MSG = re.compile(r"^(?:land|polish)\s+([A-Za-z0-9][A-Za-z0-9._-]*):")
UNAVAILABLE = 75  # EX_TEMPFAIL: the gate could not run at all; not a code red
UNAVAIL_MSG = re.compile(r"^ci: unavailable: (.+)$", re.M)
# GitHub annotations on jobs that never started: billing, spending limit, a runner label it does not offer
NOT_STARTED = re.compile(r"The job was not started because [^\n]*?(?:\.(?=\s|$)|$)|"
                         r"The configuration '[^'\n]+' is not supported", re.I | re.M)
# a gate that cannot start as a command: the owner's config, not a code red (exit 126/127 are matched by code)
CONFIG_RED = re.compile(r"command not found|is not recognized as an internal or external command|"
                        r"^(?:/usr)?/bin/(?:ba|da)?sh: (?:line \d+: )?[A-Za-z]:[\\/]", re.I | re.M)
CONFIG_EXIT = (126, 127)
NO_GREEN = "(no earlier green)"
DEFAULTS = {"every_lands": 10, "every_min": 120, "timeout_min": 60, "bisect": True, "budgets": "", "commands": {},
            "depfix_max": 3}


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


def config_red(code: int, text: str) -> bool:
    """The gate failed as a command (cannot run, not found, a Windows path in bash), not on the code under test."""
    return code in CONFIG_EXIT or (code != 0 and bool(CONFIG_RED.search(text or "")))


class Periodic:
    """Owned by a drive.Driver: schedules the gates, runs one command at a time in the background, bisects reds."""

    def __init__(self, drv, cfg: dict):
        self.drv, self.cfg = drv, cfg
        self.job = None  # (proc, gate, sha, out path, deadline, mode) mode = "run" | "bisect"
        self.queue: list[str] = []  # gates left in the current full run
        self.run_sha = ""
        self.run_red: list[str] = []
        self.run_all = False  # this run has every gate: all green moves last_green

    @property
    def st(self) -> dict:
        st = self.drv.st.setdefault("periodic", {})
        for k, v in (("last_green", ""), ("lands", 0), ("last_run", 0.0), ("red", {}), ("bisect", None),
                     ("fixed", []), ("unavailable", {}), ("config", {})):
            st.setdefault(k, v)
        if "green" not in st:  # an older state: one last_green for all gates, plus each gate's greens in the log
            st["green"] = self.seed_green(st["last_green"])
        return st

    def seed_green(self, last_green: str) -> dict:
        """{gate: sha} from the old last_green, then each gate's latest green full run in log.jsonl."""
        gates = self.cfg.get("commands", {})
        green = {g: last_green for g in gates} if last_green else {}
        try:
            lines = (self.drv.ov / "log.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            if '"periodic_green"' not in line:
                continue
            try:
                m = re.match(r"(\S+) run at ([0-9a-f]{7,40})\b", json.loads(line).get("detail") or "")
            except (ValueError, AttributeError):
                continue
            if m and m.group(1) in gates:
                sha = self.drv_git("rev-parse", "--verify", "-q", f"{m.group(2)}^{{commit}}")
                if sha:
                    green[m.group(1)] = sha
        return green

    def landed(self) -> None:
        if self.cfg:
            st = self.st
            st["lands"] += 1
            for g in st.setdefault("since", {}):
                st["since"][g] += 1

    def busy(self) -> bool:
        return bool(self.cfg) and bool(self.job or self.queue or self.st["bisect"])

    def every(self, key: str, gate: str) -> float:
        """every_lands / every_min for one gate: a number for all, or a table {gate: n, default: n}."""
        v = self.cfg.get(key, DEFAULTS[key])
        if self.own_cadence(gate) and not (isinstance(v, dict) and gate in v):
            return float("inf")  # a gate with its own cadence runs only on the keys it names
        if isinstance(v, dict):
            v = v.get(gate, v.get("default", DEFAULTS[key]))
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(DEFAULTS[key])

    def own_cadence(self, gate: str) -> bool:
        return any(isinstance(self.cfg.get(k), dict) and gate in self.cfg[k] for k in ("every_lands", "every_min"))

    def all_proven(self) -> bool:
        recs = self.drv.st.get("functions") or {}
        return bool(self.drv.fns) and all((recs.get(fid) or {}).get("status") == "proven" for fid in self.drv.fns)

    def due_gates(self, idle: bool) -> list[str]:
        """The gates due now, each with lands since its own last run: past its every_lands or every_min, or the
        build is idle (a gate with its own cadence: only once all functions are proven, once per plan size)."""
        st, now, out = self.st, time.time(), []
        since, ran, fin = st.get("since") or {}, st.get("ran") or {}, st.get("finish") or {}
        for g in self.cfg["commands"]:
            n = since.get(g, st["lands"])
            if not n:
                continue
            mins = (now - float(ran.get(g, st["last_run"]) or 0)) / 60
            if n >= self.every("every_lands", g) or mins >= self.every("every_min", g):
                out.append(g)
            elif idle and (not self.own_cadence(g) or (self.all_proven() and fin.get(g) != len(self.drv.fns))):
                out.append(g)
        return out

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
        b = self.st["bisect"] or self.next_bisect()
        if b:
            mid = next_mid(b["lo"], b["hi"])
            if mid is None:
                return self.blame(b["gate"], b["cands"][b["hi"]])
            return self.start(b["gate"], b["cands"][mid], "bisect")
        gates = self.due_gates(idle)
        if gates:
            sha = self.drv_git("rev-parse", self.drv.main)
            if not sha:
                return False
            st, now = self.st, time.time()
            since, ran, fin = st.setdefault("since", {}), st.setdefault("ran", {}), st.setdefault("finish", {})
            done = idle and self.all_proven()
            for g in self.cfg["commands"]:
                since.setdefault(g, st["lands"])
                ran.setdefault(g, st["last_run"])
                if g in gates:
                    since[g], ran[g] = 0, now
                    if done:
                        fin[g] = len(self.drv.fns)
            st.update(lands=0, last_run=now)
            self.run_sha, self.run_red, self.queue = sha, [], list(gates)
            self.run_all = len(gates) == len(self.cfg["commands"])
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
            proc = subprocess.Popen(cmd, shell=True, cwd=wt, stdin=subprocess.DEVNULL, stdout=fo, stderr=subprocess.STDOUT,
                                    **priority.popen_kwargs(priority.NORMAL))  # above the BELOW_NORMAL builders
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
        st = self.st
        un = UNAVAIL_MSG.search(text) if code == UNAVAILABLE else None
        if un:  # the gate could not run (CI billing, runner): not red, not green, never bisected
            reason = un.group(1).strip()[:200]
            self.drv.log(None, "periodic_unavailable", f"{gate} {mode} at {sha[:12]} {reason}"[:1500])
            if gate not in st["unavailable"]:
                self.drv.say(f"periodic gate {gate} unavailable: {reason}; the owner decides")
            st["unavailable"][gate] = {"sha": sha, "reason": reason}
            st["red"].pop(gate, None)
            if mode == "run":
                self.run_red.append(gate)  # the run is not all green
            elif (st["bisect"] or {}).get("gate") == gate:
                st["bisect"] = None
            return
        tail = [x for x in text.splitlines() if x.strip()][-1:] or [""]
        if not over and config_red(code, text):
            return self.config_red(gate, sha, mode, f"exit {code}: {tail[0][:160]}")
        red = code != 0 or bool(over)
        reason = (f"over budget: {'; '.join(over)}" if over else f"exit {code}: {tail[0][:160]}") if red else ""
        reason = memcap.note(text, reason) if red and not over else reason
        self.drv.log(None, "periodic_red" if red else "periodic_green", f"{gate} {mode} at {sha[:12]} {reason}"[:1500])
        if mode == "run":
            st["unavailable"].pop(gate, None)
            st["config"].pop(gate, None)
            if red:
                self.run_red.append(gate)
                old = st["red"].get(gate) or {}  # every red run is bisected again, unless its depfix is in flight
                keep = self.in_flight(old.get("depfix", "")) or NO_GREEN in str(old.get("culprit", ""))
                st["red"][gate] = {"sha": sha, "reason": reason, "culprit": old.get("culprit", "") if keep else "",
                                   "depfix": old.get("depfix", ""), "depfixes": old.get("depfixes", 0)}
            else:
                st["red"].pop(gate, None)
                st["green"][gate] = sha
            return
        bis = st["bisect"]
        if not bis or bis.get("gate") != gate or sha not in bis["cands"]:
            return
        idx = bis["cands"].index(sha)
        bis["hi" if red else "lo"] = idx

    def config_red(self, gate: str, sha: str, mode: str, reason: str) -> None:
        """A gate that cannot run as written: an owner line (gate_config), never bisected, no depfix, not green."""
        st = self.st
        self.drv.log(None, "periodic_config", f"{gate} {mode} at {sha[:12]} {reason}"[:1500])
        if mode == "run":
            if gate not in st["config"]:
                self.drv.say(f"periodic gate {gate} gate_config: {reason}; the owner fixes its command")
            st["config"][gate] = {"sha": sha, "reason": reason}
            st["red"].pop(gate, None)
            self.run_red.append(gate)  # the run is not all green
            return
        bis = st["bisect"] or {}
        if bis.get("gate") == gate:  # an older commit cannot run the gate: stop, the red stands for the owner
            st["bisect"] = None
            rec = st["red"].setdefault(gate, {"sha": sha, "reason": reason, "culprit": ""})
            rec["culprit"] = f"{sha[:12]} cannot run the gate (gate_config) {NO_GREEN}"
            self.drv.say(f"periodic gate {gate}: bisect stopped, {sha[:12]} cannot run it: {reason}")

    def in_flight(self, dfid: str) -> bool:
        d = self.drv
        return bool(dfid) and (dfid in d.jobs or dfid in d.polish_lands or any(j.fn == dfid for j in d.land_jobs()))

    def end_run(self) -> bool:
        """A full run is done: all green moves last_green too. Red gates are bisected next, each on its own."""
        sha, red, self.run_sha = self.run_sha, self.run_red, ""
        if not red and self.run_all:
            self.st["last_green"] = sha
            self.drv.say(f"periodic gates green at {sha[:12]}")
        return True

    def next_bisect(self) -> dict | None:
        """The next red gate without a culprit, bisected over the lands since ITS own last green. A gate with no
        earlier green gets that as its culprit (the red is the owner's) and the next gate is tried."""
        st = self.st
        if not self.cfg.get("bisect", True):
            return None
        for gate in [g for g in self.cfg["commands"] if g in st["red"]]:
            rec = st["red"][gate]
            if rec.get("culprit") or gate in st["unavailable"] or gate in st["config"]:
                continue
            green, sha = st["green"].get(gate) or st["last_green"], rec.get("sha", "")  # all green is green too
            cands = self.drv_git("rev-list", "--first-parent", "--reverse", f"{green}..{sha}").split() \
                if green and sha else []
            if not cands:  # no green yet: nothing to bisect against
                rec["culprit"] = f"{sha[:12]} {NO_GREEN}"
                self.drv.say(f"periodic gate {gate} red at {sha[:12]} with no earlier green: {rec['reason']}")
                continue
            st["bisect"] = {"gate": gate, "cands": cands, "lo": -1, "hi": len(cands) - 1}
            return st["bisect"]
        return None

    def blame(self, gate: str, sha: str) -> bool:
        """The first red commit: its function's module (else the module owning its first changed file) gets one
        depfix for this red run, up to depfix_max while the gate stays red; otherwise the red is the owner's."""
        st, self.st["bisect"] = self.st, None
        import drive
        subject = self.drv_git("log", "-1", "--format=%s", sha)
        fid = culprit_fid(subject)
        lead = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*):", subject)
        if fid not in self.drv.fns and lead and lead.group(1) in self.drv.fns:  # `S09: ...`: a commit for S09
            fid = lead.group(1)
        f = self.drv.fns.get(fid)
        files = [x for x in self.drv_git("show", "--name-only", "--format=", sha).splitlines() if x.strip()]
        module, path = (f.module, (f.files or files or ["?"])[0]) if f else ("", "?")
        for x in files if not module else []:
            module, path = self.drv.owner_module(x), x
            if module:
                break
        rec = st["red"].setdefault(gate, {"sha": sha, "reason": "", "culprit": ""})
        rec["culprit"] = fid if f else (f"{sha[:12]} {subject[:60]}" + (f" ({module})" if module else ""))
        self.drv.log(fid or None, "periodic_culprit", f"{gate}: {sha[:12]} {subject[:80]}")
        n, cap = int(rec.get("depfixes", 0)), int(self.cfg.get("depfix_max", DEFAULTS["depfix_max"]))
        if not module or n >= cap:
            why = "no module owns it" if not module else f"{n} depfixes did not turn it green"
            self.drv.say(f"periodic gate {gate} red since {rec['culprit']}: {rec['reason']} ({why}); the owner decides")
            return True
        base = f"{drive.DEPFIX_PREFIX}periodic-{gate}"
        k = sum(1 for x in st["fixed"] if x == base or x.startswith(base + "-"))
        dfid = base if not k else f"{base}-{k + 1}"
        st["fixed"].append(dfid)
        rec.update(depfix=dfid, depfixes=n + 1)
        self.drv.say(f"periodic gate {gate} red since {rec['culprit']}: {dfid} to module {module}")
        self.drv.start_polish_fix(module, [{"file": path, "line": "?",
                                            "rule": f"periodic gate {gate} red since {rec['culprit']} ({sha[:12]})",
                                            "evidence": f"`{self.cfg['commands'][gate]}`: {rec['reason']}"}],
                                  fid=dfid)
        return True

    def rearm(self, gate: str) -> str:
        """watchdog periodic-<gate>: a red gate whose culprit is set (and depfix spent) bisects again and gets one more
        depfix, at most drive.PERIODIC_REARMS times; '' when re-armed, else why not."""
        import drive
        st = self.st
        rec = st["red"].get(gate)
        if not rec:
            return f"{gate} is not red"
        if (st["bisect"] or {}).get("gate") == gate or (self.job and self.job[1] == gate):
            return f"{gate} is already running or bisecting"
        n = int(st.setdefault("rearmed", {}).get(gate, 0))
        if n >= drive.PERIODIC_REARMS:
            return f"{gate} re-armed {n} times already; the owner decides ({rec.get('reason', '')[:160]})"
        st["rearmed"][gate] = n + 1
        rec["culprit"] = ""
        dfid = f"{drive.DEPFIX_PREFIX}periodic-{gate}"
        st["fixed"] = [x for x in st["fixed"] if x != dfid]
        return ""

    def stop(self) -> None:
        if self.job:
            import drive
            drive.kill_tree(self.job[0])
            self.job = None


def gh(*args: str, timeout: float = 120) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def not_started(run, rid: str) -> str:
    """Why GitHub never ran this run, from its job annotations, or '' when any job ran (a real red)."""
    code, out = run("run", "view", rid, "--json", "jobs")
    try:
        jobs = (json.loads(out) if code == 0 else {}).get("jobs") or []
    except (ValueError, AttributeError):
        jobs = []
    if not jobs or any(j.get("steps") for j in jobs):  # a job that ran steps: its failure is the code's
        return ""
    _, text = run("run", "view", rid)
    found = []
    for m in NOT_STARTED.finditer(text or ""):
        why = m.group(0).strip()
        if why not in found:
            found.append(why)
    return "; ".join(found)[:300]


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
        rid = str(runs[0]["databaseId"])
        _, log = run("run", "view", rid, "--log", timeout=600)
        for name, os_, value in benches(log):
            print(f"BENCH {name} {os_} {value:g}")
        if runs[0].get("conclusion") != "success":
            why = not_started(run, rid)
            if why:
                print(f"ci: unavailable: {why}")
                return UNAVAILABLE
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
