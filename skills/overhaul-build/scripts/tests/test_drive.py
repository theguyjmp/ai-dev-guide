"""drive.py against a toy git repo, a fake worker and a fake land.py (no real claude).

Run from the scripts dir:  py -3 -m unittest
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import drive  # noqa: E402

DRIVE = SCRIPTS / "drive.py"

FUNCTIONS = """# Functions

| id | phase | module | deps | entry | acceptance | status |
|----|-------|--------|------|-------|------------|--------|
| F1 | 1 | alpha | - | `toy add 1 2` | tests/acceptance/test_f1.py::test_add | planned |
| F2 | 2 | beta | F1 | `toy double 4` | tests/acceptance/test_f2.py::test_double | planned |
"""

THREE = """# Functions

| id | phase | module | deps | entry | acceptance | status |
|----|-------|--------|------|-------|------------|--------|
| F1 | 1 | alpha | - | `toy add 1 2` | tests/acceptance/test_f1.py::test_add | planned |
| F2 | 1 | beta | - | `toy double 4` | tests/acceptance/test_f2.py::test_double | planned |
| F3 | 1 | alpha | - | `toy triple 4` | tests/acceptance/test_f3.py::test_triple | planned |
"""

# One module, four functions with a `files` column: F1-F3 own disjoint files, F4 overlaps F1 (the whole module dir).
FILES4 = """# Functions

| id | phase | module | deps | entry | acceptance | files | status |
|----|-------|--------|------|-------|------------|-------|--------|
| F1 | 1 | alpha | - | `toy add 1 2` | tests/acceptance/test_f1.py::test_add | src/alpha/f1.py | planned |
| F2 | 1 | alpha | - | `toy double 4` | tests/acceptance/test_f2.py::test_double | src/alpha/f2.py, src/alpha/__init__.py | planned |
| F3 | 1 | alpha | - | `toy triple 4` | tests/acceptance/test_f3.py::test_triple | src/alpha/f3.py | planned |
| F4 | 1 | alpha | - | `toy quad 4` | tests/acceptance/test_f4.py::test_quad | {f4} | planned |
"""

CHAIN = """# Functions

| id | phase | module | deps | entry | acceptance | files | status |
|----|-------|--------|------|-------|------------|-------|--------|
| F1 | 1 | alpha | - | `toy add 1 2` | tests/acceptance/test_f1.py::test_add | src/alpha/f1.py | planned |
| F2 | 1 | alpha | F1 | `toy double 4` | tests/acceptance/test_f2.py::test_double | src/alpha/f2.py | planned |
| F3 | 1 | alpha | F2 | `toy triple 4` | tests/acceptance/test_f3.py::test_triple | src/alpha/f3.py | planned |
| F4 | 1 | alpha | F1 | `toy quad 4` | tests/acceptance/test_f4.py::test_quad | src/alpha/f4.py | planned |
"""

ARCH = """# Architecture

## alpha
paths: `src/alpha/`
shared: `src/alpha/__init__.py`
Adds numbers.

## beta
paths: `src/beta/`
Doubles numbers through alpha.
"""

ACCEPT = """import subprocess
import sys


def test_{name}():
    out = subprocess.run([sys.executable, "-m", "toy", "{name}"], capture_output=True, text=True)
    assert out.stdout.strip() == "ok"
"""

# Behaviour comes from scenario.json: {"F1": {"build": [...], "review": [...], "land": [...]}}.
# The Nth attempt (build) or Nth review uses entry N, the last entry repeats.
FAKE_WORKER = '''
import json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
brief = Path(sys.argv[-1].split("Read and do ", 1)[1]).read_text(encoding="utf-8")
first = brief.splitlines()[0]
review = first.startswith("# Blind review of ")
conflict = first.startswith("# Resolve conflict ")
fid = first.split()[-1] if review else first.split()[3 if conflict else 2]
scenario = json.loads((ROOT / "scenario.json").read_text(encoding="utf-8")).get(fid, {})
seen = ROOT / "seen"
seen.mkdir(exist_ok=True)


def pick(kind, n):
    seq = scenario.get(kind) or ["PASS" if kind == "review" else "ok"]
    return seq[min(n, len(seq)) - 1]


def field(prefix):
    return next(ln[len(prefix):].strip().strip("`") for ln in brief.splitlines() if ln.startswith(prefix))


def finish(text, code=0):
    print(json.dumps({"type": "result", "result": text, "num_turns": 3,
                      "usage": {"input_tokens": 100, "output_tokens": 20}}))
    sys.exit(code)


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)


if first.startswith("# Polish audit round "):  # scenario "audit": one list of findings per round, the last repeats
    rnd = int(first.split()[-1])
    audits = json.loads((ROOT / "scenario.json").read_text(encoding="utf-8")).get("audit") or [[]]
    (seen / f"polish-audit-{rnd}.md").write_text(brief, encoding="utf-8")
    found = audits[min(rnd, len(audits)) - 1]
    Path(field("- Findings file to write:")).write_text(json.dumps(found), encoding="utf-8")
    finish(str(len(found)))
if first.startswith("# Polish fix "):
    (seen / f"{first.split()[3]}-fix.md").write_text(brief, encoding="utf-8")
    p = Path("src") / field("- Module:") / "tidy.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# tidied by {first.split()[3]}\\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", first)
    finish("DONE")

if review:
    n = len(list(seen.glob(fid + "-review-*.md"))) + 1
    (seen / f"{fid}-review-{n}.md").write_text(brief, encoding="utf-8")
    verdict, _, why = pick("review", n).partition(" ")
    Path(field("- Review file to write:")).write_text(f"{verdict}\\n{why or 'checked'}\\n", encoding="utf-8")
    finish(verdict)

n = int(first.rstrip(")").split()[-1])
if conflict:  # a conflict-fix pass: same worktree and branch, merge main, commit a visible fix
    (seen / f"{fid}-conflict-{n}.md").write_text(brief, encoding="utf-8")
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    (seen / f"{fid}-conflict-{n}.json").write_text(
        json.dumps({"cwd": os.getcwd(), "branch": head.stdout.strip()}), encoding="utf-8")
    if pick("conflict", n) == "crash":
        finish("boom: conflict", 7)
    git("merge", "--no-edit", "main")
    p = Path("src") / field("- Module:") / (fid.lower() + ".py")
    p.write_text(p.read_text(encoding="utf-8") + f"# conflict fix {n}\\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", f"{fid}: conflict fix {n}")
    finish("DONE")
(seen / f"{fid}-build-{n}.md").write_text(brief, encoding="utf-8")
mode = pick("build", n)
if mode == "crash":
    finish("boom: disk on fire", 7)
if mode == "hang":
    (ROOT / "pids").mkdir(exist_ok=True)
    (ROOT / "pids" / (fid + ".worker")).write_text(str(os.getpid()))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    (ROOT / "pids" / (fid + ".child")).write_text(str(child.pid))
    time.sleep(300)
if mode == "slow":  # a build that takes long enough for parallel builds to overlap in time
    time.sleep(2.5)
    mode = "ok"
if mode == "outside":  # edits a protected file and leaves it uncommitted
    p = Path("spec/FUNCTIONS.md")
    p.write_text(p.read_text(encoding="utf-8") + "\\n<!-- edited by the builder -->\\n", encoding="utf-8")
if mode == "stray":  # also writes into another module's paths
    p = Path("src") / "beta" / (fid.lower() + "_stray.py")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("STRAY = 1" + chr(10), encoding="utf-8")
if mode == "cycle":  # a change the cycles check fails, as F09 of trial 3 did
    p = Path("src") / field("- Module:") / (fid.lower() + ".py")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# CYCLE" + chr(10), encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", f"{fid}: build {n}")
if mode in ("ok", "stray"):
    p = Path("src") / field("- Module:") / (fid.lower() + ".py")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"def run():\\n    return '{fid} real work'\\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", f"{fid}: build {n}")
finish("DONE")
'''

def put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL)
    if r.returncode:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_dead(pid: int, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while pid_alive(pid) and time.monotonic() < end:
        time.sleep(0.2)
    return not pid_alive(pid)


class Toy(unittest.TestCase):
    """A fresh toy repo per test: F1 (phase 1) and F2 (phase 2, deps F1)."""

    def make_repo(self, scenario: dict | None = None, retries: int = 1, timeout_min: float = 30,
                  brief_max: int = 40000, functions: str = FUNCTIONS, max_parallel: int = 2,
                  conflict_retries: int | None = None, worker_args: str = "", harness_extra: str = "",
                  commands_extra: str = "") -> Path:
        self.root = Path(tempfile.mkdtemp(prefix="drive-test-")).resolve()
        self.addCleanup(self.cleanup_root)
        repo, scripts = self.root / "repo", self.root / "scripts"
        put(self.root / "fake_worker.py", FAKE_WORKER)
        put(self.root / "scenario.json", json.dumps(scenario or {}))
        scripts.mkdir(parents=True, exist_ok=True)
        shutil.copy(Path(__file__).with_name("fake_land.py"), scripts / "land.py")
        put(scripts / "verdict.py", "print('VERDICT GOOD fake')\n")
        put(scripts / "dash.py", "pass\n")
        put(repo / ".gitignore", ".overhaul/\n__pycache__/\n")
        put(repo / "spec" / "FUNCTIONS.md", functions)
        put(repo / "spec" / "ARCHITECTURE.md", ARCH)
        put(repo / "tests" / "acceptance" / "test_f1.py", ACCEPT.format(name="add"))
        put(repo / "tests" / "acceptance" / "test_f2.py", ACCEPT.format(name="double"))
        put(repo / "tests" / "acceptance" / "test_f3.py", ACCEPT.format(name="triple"))
        put(repo / "tests" / "acceptance" / "test_f4.py", ACCEPT.format(name="quad"))
        py, worker = Path(sys.executable).as_posix(), (self.root / "fake_worker.py").as_posix()
        limits_extra = "" if conflict_retries is None else f"conflict_retries = {conflict_retries}"
        put(repo / "overhaul.toml", f"""[commands]
test = "py -3 -m pytest -q tests/unit"
acceptance = "py -3 -m pytest -q tests/acceptance -k {{test}}"
{commands_extra}

[limits]
max_parallel = {max_parallel}
builder_max_calls = 12
brief_max_tokens = {brief_max}
retries_per_function = {retries}
builder_timeout_min = {timeout_min}
{limits_extra}

[harness]
worker_cmd = ["{py}", "{worker}"{worker_args}]
scripts_dir = "{scripts.as_posix()}"
{harness_extra}
""")
        git(repo, "init", "-q", "-b", "main")
        for key, value in (("user.name", "toy"), ("user.email", "toy@example.com"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            git(repo, "config", key, value)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "toy plan")
        return repo

    def cleanup_root(self) -> None:
        try:
            shutil.rmtree(self.root, onexc=drive.force_remove)
        except OSError:
            pass

    def drive_cli(self, repo: Path, *args: str) -> tuple[int, str]:
        p = subprocess.run([sys.executable, str(DRIVE), "--repo", str(repo), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=180)
        return p.returncode, p.stdout + p.stderr

    @staticmethod
    def state(repo: Path) -> dict:
        return json.loads((repo / ".overhaul" / "state.json").read_text(encoding="utf-8"))

    @staticmethod
    def events(repo: Path) -> list[dict]:
        lines = (repo / ".overhaul" / "log.jsonl").read_text(encoding="utf-8").splitlines()
        return [json.loads(ln) for ln in lines if ln.strip()]

    def seen(self, name: str) -> str:
        return (self.root / "seen" / name).read_text(encoding="utf-8")


class TestTemplates(unittest.TestCase):
    def test_every_placeholder_is_one_drive_fills(self):
        build = "id attempt worktree branch row entry module module_section allowed_paths " \
                "acceptance_source policies commands max_calls previous standard_path standard".split()
        review = "id branch row allowed_paths acceptance_source review_file policies diffstat diff max_calls "                  "standard_path standard".split()
        conflict = "id pass worktree branch main module reason row allowed_paths commands max_calls".split()
        audit = "round main audit_file max_calls scratch modules policies commands standard audit_brief".split()
        fix = "id round module branch max_calls issues allowed_paths policies commands".split()
        for name, keys in (("build-brief.md", build), ("review-brief.md", review),
                           ("conflict-brief.md", conflict), ("polish-audit.md", audit),
                           ("polish-fix-brief.md", fix)):
            used = set(re.findall(r"\{\{(\w+)\}\}", (drive.TEMPLATES / name).read_text(encoding="utf-8")))
            self.assertEqual(used, set(keys), name)
            self.assertNotIn("{{", drive.render(name, {k: f"<{k}>" for k in keys}))


class TestReadiness(Toy):
    def test_ready_waits_for_deps_and_retries(self):
        repo = self.make_repo()
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        recs = drv.st["functions"]
        self.assertEqual([f.id for f in drv.ready()], ["F1"])  # F2 waits for F1
        recs["F1"]["attempts"] = 2  # retries_per_function = 1: both attempts used
        self.assertEqual(drv.ready(), [])
        recs["F1"].update(attempts=0, status="proven")
        self.assertEqual([f.id for f in drv.ready()], ["F2"])

    def test_build_review_land_in_dependency_order_exit_0(self):
        repo = self.make_repo()
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertIn("DRIVE EXIT 0 proven 2/2", out)
        self.assertIn("VERDICT GOOD fake", out)
        order = [(e["fn"], e["event"]) for e in self.events(repo) if e["event"] in ("build_start", "land_ok")]
        self.assertEqual(order, [("F1", "build_start"), ("F1", "land_ok"), ("F2", "build_start"), ("F2", "land_ok")])
        recs = self.state(repo)["functions"]
        self.assertEqual({k: v["status"] for k, v in recs.items()}, {"F1": "proven", "F2": "proven"})
        files = git(repo, "ls-tree", "-r", "--name-only", "main")
        self.assertIn("src/alpha/f1.py", files)
        self.assertIn("src/beta/f2.py", files)
        build, review = (repo / ".overhaul" / "briefs" / "F1.md"), (repo / ".overhaul" / "briefs" / "review-F1.md")
        brief = build.read_text(encoding="utf-8")
        for needle in ("fn/F1", "`src/alpha/`", "tests/acceptance/test_f1.py", "def test_add", "-k test_add",
                       "None. This is the first attempt.", "land.py also refuses"):
            self.assertIn(needle, brief)
        diff = review.read_text(encoding="utf-8")
        self.assertIn("diff --git a/src/alpha/f1.py", diff)
        self.assertNotIn("{{", brief + diff)


class TestRetries(Toy):
    def test_retry_carries_the_reason_of_the_failure(self):
        repo = self.make_repo({"F1": {"build": ["crash", "ok"], "review": ["FAIL stub left in f1.py", "PASS"]}},
                              retries=2)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertIn("None. This is the first attempt.", self.seen("F1-build-1.md"))
        self.assertIn("Attempt 1 failed: builder exited rc=7: boom: disk on fire", self.seen("F1-build-2.md"))
        self.assertIn("Attempt 2 failed: review FAIL: stub left in f1.py", self.seen("F1-build-3.md"))
        self.assertIn("The worktree was reset to main", self.seen("F1-build-3.md"))
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("proven", 2))

    def test_needs_plan_after_the_retries_are_used(self):
        repo = self.make_repo({"F1": {"build": ["outside", "nochange"]}}, retries=1)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        self.assertIn("DRIVE EXIT 3", out)
        self.assertIn("needs-plan F1", out)
        self.assertIn("blocked on unproven deps: F2", out)
        recs = self.state(repo)["functions"]
        self.assertEqual((recs["F1"]["status"], recs["F1"]["attempts"]), ("needs-plan", 2))
        self.assertIn("no committed change", recs["F1"]["last_reason"])
        self.assertEqual((recs["F2"]["status"], recs["F2"]["attempts"]), ("planned", 0))
        self.assertIn("changed protected files (spec/FUNCTIONS.md)", self.seen("F1-build-2.md"))
        self.assertFalse((self.root / "seen" / "F2-build-1.md").exists())  # F2 never started

    def test_acceptance_already_green_skips_the_retry(self):
        repo = self.make_repo({"F1": {"land": ["green_before"]}}, retries=1)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("needs-plan", 0))
        self.assertTrue(rec["last_reason"].startswith("land: "), rec["last_reason"])
        self.assertIn("acceptance already green before change", rec["last_reason"])
        builds = [e for e in self.events(repo) if e["event"] == "build_start" and e["fn"] == "F1"]
        self.assertEqual(len(builds), 1)


CYCLES = """import pathlib
import sys

bad = [p for p in pathlib.Path("src").rglob("*.py") if "CYCLE" in p.read_text(encoding="utf-8")]
for p in bad:
    print(f"spec/ARCHITECTURE.md:9: Public entry toy.alpha.add_line of module alpha is no module or name it owns")
print(f"CYCLES FAIL {len(bad)} problems" if bad else "CYCLES OK")
sys.exit(1 if bad else 0)
"""


class TestTrial3Waste(Toy):
    """Trial 3 lost builder passes to review fixes outside the allowed paths (F10, F15) and to a cycles failure
    first seen at land (F09)."""

    def builds(self, repo: Path, fid: str, kind: str = "build_start") -> int:
        return sum(1 for e in self.events(repo) if e["event"] == kind and e["fn"] == fid)

    def test_review_fix_outside_allowed_paths_goes_to_needs_plan(self):  # F10
        why = "FAIL whole is not exported by inventory/__init__.py (outside allowed paths); reuse whole(text)"
        repo = self.make_repo({"F1": {"review": [why, "PASS"]}}, retries=2)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("needs-plan", 0))
        self.assertIn("plan gap", rec["last_reason"])
        self.assertEqual(self.builds(repo, "F1"), 1)

    def test_plan_gap_line_goes_to_needs_plan(self):  # F15
        repo = self.make_repo({"F1": {"review": ["FAIL PLAN-GAP: export a total from inventory", "PASS"]}}, retries=2)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.state(repo)["functions"]["F1"]["status"], "needs-plan")
        self.assertEqual(self.builds(repo, "F1"), 1)

    def test_outside_allowed_paths_on_a_stray_diff_is_a_builder_retry(self):
        why = "FAIL src/beta/f1_stray.py is outside the allowed paths; remove it"
        repo = self.make_repo({"F1": {"build": ["stray", "ok"], "review": [why, "PASS"]}}, retries=2)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertIn("Attempt 1 failed: review FAIL: src/beta/f1_stray.py", self.seen("F1-build-2.md"))

    def test_cycles_run_before_review_and_reach_the_retry(self):  # F09
        py = Path(sys.executable).as_posix()
        repo = self.make_repo({"F1": {"build": ["cycle", "ok"]}}, retries=2,
                              commands_extra=f'cycles = "{py} tools/cyc.py"')
        put(repo / "tools" / "cyc.py", CYCLES)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "cycles tool")
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        brief = self.seen("F1-build-2.md")
        self.assertIn("Attempt 1 failed: cycles failed (exit 1) in the worktree before review", brief)
        self.assertIn("Public entry toy.alpha.add_line of module alpha is no module or name it owns", brief)
        self.assertEqual(self.builds(repo, "F1", "review_pass") + self.builds(repo, "F1", "review_fail"), 1)
        self.assertFalse(any(e["event"] == "land_fail" for e in self.events(repo)))


class TestBudgets(Toy):
    def test_over_budget_brief_is_refused_into_needs_plan(self):
        repo = self.make_repo(brief_max=50)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("needs-plan", 0))
        self.assertIn("over brief_max_tokens 50", rec["last_reason"])
        self.assertFalse((self.root / "seen").exists())  # no worker was ever started

    def test_timeout_kills_the_worker_tree_and_nothing_else(self):
        repo = self.make_repo({"F1": {"build": ["hang"]}}, retries=0, timeout_min=0.1)
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        self.assertIn("timed out", self.state(repo)["functions"]["F1"]["last_reason"])
        worker = int((self.root / "pids" / "F1.worker").read_text())
        child = int((self.root / "pids" / "F1.child").read_text())
        self.assertTrue(wait_dead(worker), "the timed-out worker is still running")
        self.assertTrue(wait_dead(child), "the worker's child is still running")
        self.assertIsNone(bystander.poll(), "an unrelated process of the same program was killed")


class TestModules(Toy):
    def driver(self, repo: Path) -> drive.Driver:
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        return drv

    def test_ready_offers_one_function_per_module_and_none_of_an_in_flight_module(self):
        drv = self.driver(self.make_repo(functions=THREE))
        self.assertEqual([f.id for f in drv.ready()], ["F1", "F2"])  # F3 shares alpha with F1
        drv.land_queue.append("F1")  # F1 is in flight (queued to land)
        self.assertEqual([f.id for f in drv.ready()], ["F2"])
        drv.land_queue.clear()
        drv.jobs["F2"] = None  # F2 is in flight (building); ready() reads only the ids
        self.assertEqual([f.id for f in drv.ready()], ["F1"])

    def test_same_module_functions_never_run_in_parallel_other_modules_do(self):
        repo = self.make_repo(functions=THREE, max_parallel=3)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        events = [(e["fn"], e["event"]) for e in self.events(repo)]
        self.assertLess(events.index(("F1", "land_ok")), events.index(("F3", "build_start")))  # F3 waited
        self.assertLess(events.index(("F2", "build_start")), events.index(("F1", "land_ok")))  # F2 did not


class TestFileLock(Toy):
    """The build lock is the function's files (FUNCTIONS.md `files`), not its module."""

    SLOW = {f"F{i}": {"build": ["slow"]} for i in range(1, 5)}

    def order(self, repo: Path) -> list[tuple[str, str]]:
        return [(e["fn"], e["event"]) for e in self.events(repo)]

    def test_four_functions_of_one_module_with_disjoint_files_build_at_once(self):
        repo = self.make_repo(self.SLOW, functions=FILES4.format(f4="src/alpha/f4.py"), max_parallel=6)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.order(repo)
        first_end = min(ev.index((f"F{i}", "build_end")) for i in range(1, 5))
        for i in range(1, 5):
            self.assertLess(ev.index((f"F{i}", "build_start")), first_end, f"F{i} waited for another build")
        rec = self.state(repo)["functions"]
        self.assertEqual({k: (v["status"], v["attempts"]) for k, v in rec.items()},
                         {f"F{i}": ("proven", 0) for i in range(1, 5)})  # no failed attempt: no rework
        self.assertIn("`src/alpha/f2.py`", self.seen("F2-build-1.md"))
        self.assertIn("`src/alpha/__init__.py`", self.seen("F1-build-1.md"))  # the shared file is allowed to all

    def test_overlapping_files_serialise(self):
        repo = self.make_repo(self.SLOW, functions=FILES4.format(f4="src/alpha/"), max_parallel=6)
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        self.assertEqual([f.id for f in drv.ready()], ["F1", "F2", "F3"])  # F4 claims the whole module dir
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.order(repo)
        last_land = max(ev.index((f"F{i}", "land_ok")) for i in range(1, 4))
        self.assertLess(last_land, ev.index(("F4", "build_start")))

    def test_shared_file_is_no_lock_and_globs_overlap(self):
        self.assertFalse(drive.paths_overlap(["src/a/f1.py"], ["src/a/f2.py"]))
        self.assertTrue(drive.paths_overlap(["src/a/"], ["src/a/f2.py"]))
        self.assertTrue(drive.paths_overlap(["src/a/*.py"], ["src/a/f2.py"]))
        self.assertTrue(drive.paths_overlap(["src/a/f*"], ["src/a/**"]))
        self.assertFalse(drive.paths_overlap(["src/a/*.py"], ["src/b/x.py"]))

    def test_a_dependency_chain_is_respected(self):
        repo = self.make_repo(functions=CHAIN, max_parallel=6)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.order(repo)
        for fn, dep in (("F2", "F1"), ("F3", "F2"), ("F4", "F1")):
            self.assertLess(ev.index((dep, "land_ok")), ev.index((fn, "build_start")), f"{fn} before {dep}")

    def test_max_parallel_defaults_to_6_and_harness_wins(self):
        repo = self.make_repo(functions=CHAIN)
        cfg = drive.load_config(repo)
        cfg["limits"].pop("max_parallel", None)
        self.assertEqual(drive.Driver(repo, cfg).max_parallel, 6)
        cfg["limits"]["max_parallel"], cfg["harness"]["max_parallel"] = 2, 5
        self.assertEqual(drive.Driver(repo, cfg).max_parallel, 5)


class TestConflicts(Toy):
    def of(self, repo: Path, event: str) -> list[dict]:
        return [e for e in self.events(repo) if e["fn"] == "F1" and e["event"] == event]

    def test_a_merge_conflict_runs_a_conflict_pass_and_is_not_an_attempt(self):
        repo = self.make_repo({"F1": {"land": ["conflict", "ok"]}}, retries=1)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"], rec["conflicts"]), ("proven", 0, 1))
        self.assertEqual((len(self.of(repo, "build_start")), len(self.of(repo, "conflict_start"))), (1, 1))
        self.assertEqual(len(self.of(repo, "review_pass")), 2)  # reviewed again after the fix, then landed
        self.assertFalse((self.root / "seen" / "F1-build-2.md").exists())  # never rebuilt
        brief = self.seen("F1-conflict-1.md")
        for needle in ("Resolve conflict F1 (pass 1)", "`git merge main`", "merge conflict merging main into fn/F1",
                       "src/alpha/", "Never edit `tests/acceptance/`"):
            self.assertIn(needle, brief)
        self.assertNotIn("{{", brief)
        where = json.loads(self.seen("F1-conflict-1.json"))
        self.assertEqual(where["branch"], "fn/F1")
        same = os.path.normcase(os.path.realpath(where["cwd"]))
        self.assertEqual(same, os.path.normcase(os.path.realpath(repo / ".overhaul" / "wt" / "F1")))
        self.assertIn("# conflict fix 1", git(repo, "show", "main:src/alpha/f1.py"))  # the fix reached main

    def test_the_conflict_budget_is_spent_then_the_failure_counts_as_an_attempt(self):
        repo = self.make_repo({"F1": {"land": ["conflict", "conflict", "conflict", "ok"]}}, retries=1)
        rc, out = self.drive_cli(repo)  # conflict_retries defaults to 2
        self.assertEqual(rc, 0, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"], rec["conflicts"]), ("proven", 1, 0))
        self.assertEqual((len(self.of(repo, "conflict_start")), len(self.of(repo, "build_start"))), (2, 2))
        self.assertIn("Attempt 1 failed: land: merge conflict merging main into fn/F1", self.seen("F1-build-2.md"))

    def test_conflict_retries_zero_makes_it_an_ordinary_failed_attempt(self):
        repo = self.make_repo({"F1": {"land": ["conflict"]}}, retries=0, conflict_retries=0)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("needs-plan", 1))
        self.assertEqual(self.of(repo, "conflict_start"), [])

    def test_a_failed_conflict_pass_is_an_attempt(self):
        repo = self.make_repo({"F1": {"land": ["conflict", "ok"], "conflict": ["crash"]}}, retries=1)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("proven", 1))
        self.assertIn("Attempt 1 failed: conflict pass 1: builder exited rc=7: boom: conflict",
                      self.seen("F1-build-2.md"))

    def test_the_branch_must_contain_main_as_it_was_when_the_pass_began(self):
        repo = self.make_repo()
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        wt = drv.prepare_worktree("F1")
        put(wt / "src" / "alpha" / "f1.py", "def run():\n    return 1\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "F1 work")
        put(repo / "NEWS.md", "main moved\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves")
        job = SimpleNamespace(fn="F1", attempt=1, kind="conflict", timed_out=False, out=repo / "none.json",
                              proc=SimpleNamespace(returncode=0), base=git(repo, "rev-parse", "main").strip())
        self.assertIn("does not contain main", drv.conflict_problem(job, ""))
        git(wt, "merge", "--no-edit", "main")
        self.assertEqual(drv.conflict_problem(job, ""), "")
        put(repo / "NEWS.md", "main moved again\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves on")  # a landing during the pass must not fail it
        self.assertEqual(drv.conflict_problem(job, ""), "")

    def test_the_conflict_wording_is_the_one_land_py_prints(self):
        self.assertIn(f"{drive.CONFLICT} merging", (SCRIPTS / "land.py").read_text(encoding="utf-8"))


class TestModels(Toy):
    def claude_cmds(self, repo: Path) -> dict[str, list[str]]:
        """What drive.py runs per worker kind when no worker_cmd is set and `claude` is on PATH."""
        toml = repo / "overhaul.toml"
        toml.write_text(re.sub(r"^worker_cmd = .*\n", "", toml.read_text(encoding="utf-8"), flags=re.M),
                        encoding="utf-8")
        drv = drive.Driver(repo, drive.load_config(repo))
        with mock.patch.object(drive.shutil, "which", return_value="claude"), \
                mock.patch.object(drive, "supports_max_turns", return_value=True):
            return {kind: drv.worker_cmd(kind) for kind in ("build", "conflict", "review")}

    @staticmethod
    def flag(cmd: list[str], name: str) -> str | None:
        return cmd[cmd.index(name) + 1] if name in cmd else None

    def test_defaults_and_the_model_flag_of_each_worker_kind(self):
        repo = self.make_repo()
        cfg = drive.load_config(repo)
        self.assertEqual(cfg["limits"]["conflict_retries"], 2)
        self.assertEqual((cfg["harness"]["builder_model"], cfg["harness"]["review_model"]), ("opus", "sonnet"))
        cmds = self.claude_cmds(repo)
        for kind, model, turns in (("build", "opus", "12"), ("conflict", "opus", "12"), ("review", "sonnet", "20")):
            self.assertEqual(cmds[kind][1], "-p", kind)
            self.assertEqual(self.flag(cmds[kind], "--model"), model, kind)
            self.assertEqual(self.flag(cmds[kind], "--max-turns"), turns, kind)

    def test_models_come_from_overhaul_toml_and_an_empty_one_means_the_claude_default(self):
        repo = self.make_repo(harness_extra='builder_model = "haiku"\nreview_model = ""')
        cmds = self.claude_cmds(repo)
        self.assertEqual([self.flag(cmds[k], "--model") for k in ("build", "conflict", "review")],
                         ["haiku", "haiku", None])

    def test_the_model_is_logged_on_build_conflict_and_review_events(self):
        repo = self.make_repo({"F1": {"land": ["conflict", "ok"]}}, worker_args=', "--model", "fake-m"')
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        for event in ("build_start", "build_end", "conflict_start", "conflict_end", "review_pass"):
            details = [e["detail"] for e in self.events(repo) if e["fn"] == "F1" and e["event"] == event]
            self.assertTrue(details and all("model=fake-m" in d for d in details), (event, details))


class TestManagerCommands(Toy):
    def test_reset_and_mark_proven_and_exit_codes(self):
        repo = self.make_repo(brief_max=50)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.state(repo)["functions"]["F1"]["status"], "needs-plan")

        rc, out = self.drive_cli(repo, "--reset", "F1")
        self.assertEqual(rc, 0, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"], rec["last_reason"]), ("planned", 0, ""))

        rc, out = self.drive_cli(repo, "--reset", "NOPE")
        self.assertEqual(rc, 1, out)
        rc, out = self.drive_cli(repo, "--mark-proven", "F1")
        self.assertEqual((rc, "needs --evidence" in out), (1, True), out)
        empty = self.root / "empty.txt"
        put(empty, "")
        rc, out = self.drive_cli(repo, "--mark-proven", "F1", "--evidence", str(empty))
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.state(repo)["functions"]["F1"]["status"], "planned")

        evidence = self.root / "review-F1.txt"
        put(evidence, "PASS\nblind review found it real\n")
        rc, out = self.drive_cli(repo, "--mark-proven", "F1", "--evidence", str(evidence))
        self.assertEqual(rc, 0, out)
        st = self.state(repo)
        self.assertEqual(st["functions"]["F1"]["status"], "proven")
        self.assertEqual(st["proven_history"][-1][1], 1)

        rc, out = self.drive_cli(repo)  # F2 is ready now, but its brief is still over budget
        self.assertEqual(rc, 3, out)
        self.assertIn("proven 1/2", out)
        self.assertEqual(self.state(repo)["functions"]["F2"]["status"], "needs-plan")

    def test_errors_exit_1(self):
        repo = self.make_repo()
        (repo / "overhaul.toml").unlink()
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 1, out)
        self.assertIn("no overhaul.toml", out)

        repo = self.make_repo()
        (self.root / "scripts" / "land.py").unlink()
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 1, out)
        self.assertIn("land.py not found", out)


class TestLock(Toy):
    def test_land_runs_while_drive_holds_no_lock(self):
        repo = self.make_repo({"F1": {"land": ["slow"]}})
        rc, out = self.drive_cli(repo)  # the fake land fails with "lock held" if drive kept the lock
        self.assertEqual(rc, 0, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("proven", 0))
        self.assertFalse(drive.lock_path(repo).exists())
        landed = [e for e in self.events(repo) if e["fn"] == "F1" and e["event"] == "land_ok"]
        self.assertEqual(len(landed), 1)  # land.py logs its own land_ok; drive.py must not add a second
        argv = json.loads((repo / ".overhaul" / "land-argv.json").read_text(encoding="utf-8"))
        self.assertEqual(argv[argv.index("--main") + 1], "main")  # land.py supports --main, so drive.py passes it

    def test_land_fail_is_logged_once(self):
        repo = self.make_repo({"F1": {"land": ["green_before"]}})  # the fake logs its own land_fail
        self.drive_cli(repo)
        fails = [e for e in self.events(repo) if e["fn"] == "F1" and e["event"] == "land_fail"]
        self.assertEqual(len(fails), 1)

    def test_land_that_dies_without_logging_gets_one_land_fail_from_drive(self):
        repo = self.make_repo({"F1": {"land": ["die"]}}, retries=0)
        self.drive_cli(repo)
        fails = [e for e in self.events(repo) if e["fn"] == "F1" and e["event"] == "land_fail"]
        self.assertEqual(len(fails), 1)
        self.assertIn("land died", fails[0]["detail"])

    def test_state_write_is_deferred_while_land_holds_the_lock(self):
        repo = self.make_repo()
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        self.assertTrue(drive.acquire_lock(repo, 0, "land.py"))
        t0 = time.monotonic()
        drv.set("F1", status="building")
        self.assertLess(time.monotonic() - t0, 2)  # no waiting on land.py
        self.assertTrue(drv.dirty)
        self.assertEqual(self.state(repo)["functions"]["F1"]["status"], "planned")
        drive.release_lock(repo)
        self.assertTrue(drv.save())
        self.assertEqual((drv.dirty, self.state(repo)["functions"]["F1"]["status"]), (False, "building"))

    def test_absorbs_what_land_wrote_and_breaks_a_dead_holders_lock(self):
        repo = self.make_repo()
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        st = self.state(repo)
        st["functions"]["F1"]["status"] = "proven"  # what land.py writes
        put(repo / ".overhaul" / "state.json", json.dumps(st))
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        put(drive.lock_path(repo), f"land.py pid {dead.pid} since long ago\n")  # land.py killed hard
        self.assertTrue(drv.save())
        self.assertEqual(drv.st["functions"]["F1"]["status"], "proven")
        put(drive.lock_path(repo), f"land.py pid {os.getpid()} since now\n")  # a live holder is respected
        self.assertFalse(drive.acquire_lock(repo, 0))
        drive.release_lock(repo)

    def test_land_py_breaks_a_dead_holders_lock_and_respects_live_and_fresh_ones(self):
        import land
        repo = self.make_repo()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        put(Path(land.lock_path(str(repo))), f"land.py pid {dead.pid} since long ago\n")
        self.assertTrue(land.acquire_lock(str(repo), 0))  # broken, then ours
        self.assertFalse(drive.acquire_lock(repo, 0))  # drive.py and land.py share one lock
        land.release_lock(str(repo))
        put(Path(land.lock_path(str(repo))), f"land.py pid {os.getpid()} since now\n")
        self.assertFalse(land.acquire_lock(str(repo), 0))  # a live holder keeps it
        put(Path(land.lock_path(str(repo))), "")
        self.assertFalse(land.acquire_lock(str(repo), 0))  # created a moment ago, not written yet
        land.release_lock(str(repo))
        self.assertTrue(drive.acquire_lock(repo, 0))
        drive.release_lock(repo)
        self.assertEqual(drive.lock_path(repo), Path(land.lock_path(str(repo))))


def finding(module: str, n: int) -> dict:
    return {"module": module, "file": f"src/{module}/x.py", "line": n, "rule": "duplication", "evidence": f"dup {n}"}


class TestPolish(Toy):
    def verdict(self, repo: Path) -> str:
        p = subprocess.run([sys.executable, str(SCRIPTS / "verdict.py"), "--repo", str(repo), "--no-checks"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        return p.stdout.strip().splitlines()[-1]

    def polish_events(self, repo: Path) -> list[tuple[str, str]]:
        return [(e["event"], e["detail"]) for e in self.events(repo) if e["event"].startswith("polish_")]

    def test_audit_fix_land_then_a_clean_reaudit_gives_verdict_good(self):
        repo = self.make_repo({"audit": [[finding("alpha", 3), finding("beta", 7)], []]})
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.polish_events(repo)
        self.assertEqual([d.split(" model=")[0] for e, d in ev if e == "polish_audit"],
                         ["round 1: 2 issues", "round 2: 0 issues"])
        landed = sorted(d for e, d in ev if e == "polish_fix" and d.startswith("landed"))
        self.assertEqual(landed, ["landed alpha", "landed beta"])
        self.assertEqual([d for e, d in ev if e == "polish_land_ok"], ["landed", "landed"])
        self.assertEqual(ev[-1], ("polish_done", "0 issues after 1 fix rounds"))
        files = git(repo, "ls-tree", "-r", "--name-only", "main")
        self.assertIn("src/alpha/tidy.py", files)
        self.assertIn("src/beta/tidy.py", files)
        self.assertIn("`src/alpha/x.py:3` [duplication] dup 3", self.seen("polish-r1-alpha-fix.md"))
        self.assertNotIn("beta/x.py", self.seen("polish-r1-alpha-fix.md"))  # one module per fix pass
        pol = self.state(repo)["polish"]
        self.assertEqual((pol["status"], pol["issues"], pol["history"]), ("done", 0, [[1, 2], [2, 0]]))
        self.assertEqual(set(self.state(repo)["functions"]), {"F1", "F2"})  # no state entry for a polish fix
        line = self.verdict(repo)
        self.assertTrue(line.startswith("VERDICT GOOD proven 2/2 rate"), line)
        self.assertIn(" issues 0 ", line)

    def test_issues_left_after_polish_rounds_give_a_verdict_that_is_not_good(self):
        repo = self.make_repo({"audit": [[finding("alpha", 3), finding("beta", 7)]]})
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.polish_events(repo)
        self.assertEqual(len([1 for e, _ in ev if e == "polish_audit"]), 4)  # polish_rounds 3, then the re-audit
        self.assertEqual(len([1 for e, d in ev if e == "polish_fix" and d.startswith("landed")]), 6)
        self.assertEqual(ev[-1], ("polish_done", "2 issues after 3 fix rounds"))
        line = self.verdict(repo)
        self.assertTrue(line.startswith("VERDICT BAD proven 2/2"), line)
        self.assertIn(" issues 2 ", line)
        self.assertIn("polish done: 2 issues", line)

    def test_a_fix_round_that_raises_the_issue_count_is_reverted(self):
        repo = self.make_repo({"audit": [[finding("alpha", 3), finding("beta", 7)],
                                         [finding("alpha", 3), finding("beta", 7), finding("alpha", 9)]]})
        before = git(repo, "rev-parse", "main").strip()
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.polish_events(repo)
        self.assertEqual(len([1 for e, _ in ev if e == "polish_revert"]), 3, ev)
        self.assertEqual(ev[-1], ("polish_done", "2 issues after 3 fix rounds"))
        files = git(repo, "ls-tree", "-r", "--name-only", "main")
        self.assertNotIn("src/alpha/tidy.py", files)  # every round made it worse: main keeps the round 1 tree
        self.assertNotIn("src/beta/tidy.py", files)
        self.assertNotEqual(git(repo, "rev-parse", "main").strip(), before)  # reverted forward, history kept
        self.assertIn("dup 7", self.seen("polish-r2-beta-fix.md"))  # round 2 works from the best audit's issues
        self.assertNotIn("x.py:9", self.seen("polish-r2-alpha-fix.md"))
        pol = self.state(repo)["polish"]
        self.assertEqual((pol["issues"], pol["best"][:2]), (2, [1, 2]))
        audit = self.seen("polish-audit-1.md")
        self.assertIn("quality-standard.md", audit)
        self.assertIn("audit-brief.md", audit)

    def test_polish_audit_defaults_to_the_judge_model(self):
        repo = self.make_repo()
        drv = drive.Driver(repo, drive.load_config(repo))
        self.assertEqual((drv.audit_model, drv.polish_rounds), ("opus", 3))

    def test_polish_rounds_zero_skips_the_phase(self):
        repo = self.make_repo({"audit": [[finding("alpha", 3)]]})
        toml = repo / "overhaul.toml"
        toml.write_text(toml.read_text(encoding="utf-8").replace("[limits]\n", "[limits]\npolish_rounds = 0\n"),
                        encoding="utf-8")
        git(repo, "commit", "-qam", "no polish")
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.polish_events(repo), [])


if __name__ == "__main__":
    unittest.main()
