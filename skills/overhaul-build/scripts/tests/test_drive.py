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
verify = first.startswith("# Verify findings ")
conflict = first.startswith("# Resolve conflict ")
fid = first.split()[-1] if review or verify else first.split()[3 if conflict else 2]
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
    garble = json.loads((ROOT / "scenario.json").read_text(encoding="utf-8")).get("audit_garble")
    if garble and not (seen / "audit-garbled").exists():  # the first audit writes an unreadable file
        (seen / "audit-garbled").write_text("", encoding="utf-8")
        Path(field("- Findings file to write:")).write_text("[{", encoding="utf-8")
        finish("0")
    Path(field("- Findings file to write:")).write_text(json.dumps(found), encoding="utf-8")
    finish(str(len(found)))
if first.startswith("# Polish fix "):
    k = len(list(seen.glob(first.split()[3] + "-fix*.md")))  # the 2nd pass on a fid writes -fix-2.md
    (seen / (first.split()[3] + ("-fix.md" if not k else f"-fix-{k + 1}.md"))).write_text(brief, encoding="utf-8")
    p = Path("src") / field("- Module:").split(",")[0] / "tidy.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    p.write_text(old + f"# tidied by {first.split()[3]}\\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", first)
    finish("DONE")

if verify:  # scenario "verify": per verify pass "all" (default), "none" or the list of n it reproduces
    n = len(list(seen.glob(fid + "-verify-*.md"))) + 1
    (seen / f"{fid}-verify-{n}.md").write_text(brief, encoding="utf-8")
    listed = [int(ln.split(".", 1)[0]) for ln in brief.splitlines() if ln[:1].isdigit() and ". [" in ln]
    seq = scenario.get("verify") or ["all"]
    got = seq[min(n, len(seq)) - 1]
    hits = listed if got == "all" else [] if got == "none" else got
    Path(field("- Verify file to write:")).write_text(json.dumps(
        [{"n": k, "reproduced": k in hits, "how": f"scratch test {k} fails"} for k in listed]), encoding="utf-8")
    finish(str(len(hits)))

if review:  # scenario "review": "PASS", "FAIL why" (one K2 NO), "FAIL PLAN-GAP: x", or the JSON result itself
    n = len(list(seen.glob(fid + "-review-*.md"))) + 1
    (seen / f"{fid}-review-{n}.md").write_text(brief, encoding="utf-8")
    got = pick("review", n)
    if not isinstance(got, str):
        result = got
    elif got == "PASS":
        result = {"checklist": "v1", "items": []}
    elif got.startswith("FAIL PLAN-GAP: "):
        result = {"checklist": "v1", "items": [], "plan_gap": got[len("FAIL PLAN-GAP: "):]}
    else:
        why = got.partition(" ")[2] or "checked"
        mod = next((ln.split("`")[1] for ln in brief.splitlines() if ln.startswith("- Module:")), "alpha")
        result = {"checklist": "v1", "items": [{"id": "K2", "answer": "no", "file": f"src/{mod}/{fid.lower()}.py",
                                                "line": 1, "evidence": why, "fix": "make it right"}]}
    Path(field("- Review file to write:")).write_text(json.dumps(result), encoding="utf-8")
    finish("FAIL" if result.get("items") or result.get("plan_gap") else "PASS")

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
if mode == "blockedtest":  # the contract is wrong: name the planned test
    finish("BLOCKED " + fid + chr(10) + "the test is wrong" + chr(10)
           + "BLOCKED-BY: tests/acceptance/test_f1.py:3 the pattern holds a backspace byte")
if mode == "blockedby" and (seen / (fid + "-blocked")).exists():  # the rebuild after its dependency fix
    mode = "ok"
if mode == "blockedby":  # a landed dependency's bug outside the allowed paths: commit nothing, name it
    (seen / (fid + "-blocked")).write_text("1", encoding="utf-8")
    finish("BLOCKED " + fid + chr(10) + "alpha add is wrong" + chr(10)
           + "BLOCKED-BY: src/alpha/f1.py:1 add 1 2 prints 0, want 3")
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
                  commands_extra: str = "", pre_review: str = '["cycles"]',
                  pre_review_acceptance: str = "false", phase_polish: bool = False,
                  phase_audit: bool = False) -> Path:
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
pre_review = {pre_review}
pre_review_acceptance = {pre_review_acceptance}
phase_polish = {str(phase_polish).lower()}
phase_audit = {str(phase_audit).lower()}
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
                "acceptance_source policies commands max_calls previous standard_path standard project_rules footprint dup_check".split()
        review = "id branch row allowed_paths acceptance_source review_file policies diffstat diff max_calls " \
                 "standard_path standard project_rules checklist_path checklist_version checklist round_rule".split()
        conflict = "id pass worktree branch main module reason row allowed_paths commands max_calls".split()
        audit = "round main audit_file max_calls scratch modules policies commands standard " \
                "checklist_path checklist_version checklist scope".split()
        verify = "id branch verify_file scratch findings commands max_calls".split()
        fix = "id round module branch max_calls issues allowed_paths policies commands".split()
        for name, keys in (("build-brief.md", build), ("review-brief.md", review),
                           ("conflict-brief.md", conflict), ("polish-audit.md", audit),
                           ("polish-fix-brief.md", fix), ("verify-brief.md", verify)):
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
        self.assertIn("Attempt 2 failed: review FAIL: 1 verified blocking NOs", self.seen("F1-build-3.md"))
        self.assertIn("] `src/alpha/f1.py:1` stub left in f1.py", self.seen("F1-build-3.md"))
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

    def test_red_acceptance_fails_before_review_with_its_output_saved(self):  # trial 7 F02
        repo = self.make_repo({"F1": {"review": ["PASS"]}}, retries=0, pre_review_acceptance="true")
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertTrue(rec["last_reason"].startswith("acceptance red in the worktree"), rec["last_reason"])
        self.assertTrue((repo / ".overhaul" / "runs" / "F1-acceptance.out").is_file())
        self.assertFalse(any(e["event"].startswith("review") for e in self.events(repo)))

    def test_blocked_by_sends_the_defect_to_the_owner_and_rebuilds_free(self):  # trials 7-9: F02, F08, F09
        repo = self.make_repo({"F2": {"build": ["blockedby"]}}, retries=0)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state(repo)["functions"]["F2"]["status"], "proven")
        ev = [(e["event"], e["fn"], e["detail"]) for e in self.events(repo)]
        self.assertTrue(any(e == "blocked_by" and f == "F2" and "src/alpha/f1.py:1 in alpha" in d for e, f, d in ev), ev)
        self.assertTrue(any(e == "dep_fix" and f == "depfix-F2" and d == "landed alpha" for e, f, d in ev), ev)
        self.assertIn("add 1 2 prints 0, want 3", self.seen("depfix-F2-fix.md"))
        self.assertFalse(any(e == "needs_plan" for e, _, _ in ev))

    def test_blocked_by_on_the_contract_parks_at_once(self):  # trial 10 F09: backspace bytes in its test
        repo = self.make_repo({"F1": {"build": ["blockedtest"]}}, retries=3)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual(rec["status"], "needs-plan")
        self.assertIn("contract defect reported by the builder in tests/acceptance/test_f1.py:3", rec["last_reason"])
        self.assertEqual(len([e for e in self.events(repo) if e["event"] == "build_start"]), 1)

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

    def test_optional_plan_gap_with_no_blocking_no_passes_and_is_recorded(self):  # trial 6 F06
        gap = "Optional: beta could export the tax rate text"
        repo = self.make_repo({"F1": {"review": ["FAIL PLAN-GAP: " + gap, "PASS"]}}, retries=2)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state(repo)["functions"]["F1"]["status"], "proven")
        self.assertEqual(self.builds(repo, "F1"), 1)
        gaps = [json.loads(ln) for ln in (repo / ".overhaul" / "plan-gaps.jsonl").read_text(
            encoding="utf-8").splitlines()]
        self.assertEqual([(g["fn"], g["module"], g["gap"]) for g in gaps], [("F1", "alpha", gap)])
        self.assertFalse(any(e["event"] == "needs_plan" for e in self.events(repo)))

    def test_verified_dedup_no_with_plan_gap_passes_and_records_the_gap(self):
        review = {"checklist": "v1", "plan_gap": "C07: beta must export LIMIT; beta is outside the allowed paths",
                  "items": [{"id": "C07", "answer": "no", "file": "src/alpha/f1.py", "line": 1,
                             "evidence": "LIMIT = 9 repeats beta's literal", "fix": "import it from beta"}]}
        repo = self.make_repo({"F1": {"review": [review]}}, retries=2)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state(repo)["functions"]["F1"]["status"], "proven")
        gaps = (repo / ".overhaul" / "plan-gaps.jsonl").read_text(encoding="utf-8")
        self.assertIn("beta must export LIMIT", gaps)

    def test_verified_dedup_no_on_the_last_attempt_lands_and_records_a_gap(self):  # trial 7 F02
        review = {"checklist": "v1",
                  "items": [{"id": "C03", "answer": "no", "file": "src/alpha/f1.py", "line": 1,
                             "evidence": "help text repeats the limit", "fix": "build it from LIMIT"}]}
        repo = self.make_repo({"F1": {"review": [review]}}, retries=0)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state(repo)["functions"]["F1"]["status"], "proven")
        gaps = (repo / ".overhaul" / "plan-gaps.jsonl").read_text(encoding="utf-8")
        self.assertIn("help text repeats the limit", gaps)

    def test_plan_gap_with_a_verified_behaviour_no_goes_to_needs_plan(self):  # F15, trial 6 F05
        review = {"checklist": "v1", "plan_gap": "C13: beta must export LIMIT; beta is outside the allowed paths",
                  "items": [{"id": "C13", "answer": "no", "file": "src/alpha/f1.py", "line": 1,
                             "evidence": "LIMIT = 9 repeats beta's literal", "fix": "import it from beta"}]}
        repo = self.make_repo({"F1": {"review": [review, "PASS"]}}, retries=2)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual(rec["status"], "needs-plan")
        self.assertIn("plan gap", rec["last_reason"])
        self.assertEqual(self.builds(repo, "F1"), 1)
        self.assertFalse((repo / ".overhaul" / "plan-gaps.jsonl").exists())

    def test_plan_gap_with_an_unverified_no_passes_and_is_recorded(self):
        review = {"checklist": "v1", "plan_gap": "C07: beta should export LIMIT",
                  "items": [{"id": "C07", "answer": "no", "file": "src/alpha/f1.py", "line": 1,
                             "evidence": "LIMIT = 9 repeats beta's literal", "fix": "import it from beta"}]}
        repo = self.make_repo({"F1": {"review": [review, "PASS"], "verify": ["none"]}}, retries=2)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertTrue((repo / ".overhaul" / "plan-gaps.jsonl").is_file())

    def test_outside_allowed_paths_on_a_stray_diff_is_a_builder_retry(self):
        why = "FAIL src/beta/f1_stray.py is outside the allowed paths; remove it"
        repo = self.make_repo({"F1": {"build": ["stray", "ok"], "review": [why, "PASS"]}}, retries=2)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertIn("Attempt 1 failed: review FAIL: 1 verified blocking NOs", self.seen("F1-build-2.md"))
        self.assertIn("src/beta/f1_stray.py", self.seen("F1-build-2.md"))

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
        # 18 s, not 6: under a loaded full suite the fake worker's start and its child's Popen took longer than 6 s,
        # so the kill came before the pid files existed. The checks stay the same; only the margins grew.
        repo = self.make_repo({"F1": {"build": ["hang"]}}, retries=0, timeout_min=0.3)
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        self.assertIn("timed out", self.state(repo)["functions"]["F1"]["last_reason"])
        pids = self.root / "pids"
        self.assertTrue((pids / "F1.child").is_file(), "the worker was killed before it started its child")
        worker, child = int((pids / "F1.worker").read_text()), int((pids / "F1.child").read_text())
        self.assertTrue(wait_dead(worker, 30), "the timed-out worker is still running")
        self.assertTrue(wait_dead(child, 30), "the worker's child is still running")
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


NEEDS = CHAIN.replace("| files | status |", "| files | needs | status |").replace(
    "|-------|--------|", "|-------|-------|--------|").replace("| planned |", "| {n} | planned |")


WIDE = CHAIN.replace("| F2 | 1 | alpha | F1 |", "| F2 | 1 | alpha | - |").replace(
    "| F3 | 1 | alpha | F2 |", "| F3 | 1 | alpha | F2 |").replace("| F4 | 1 | alpha | F1 |", "| F4 | 1 | alpha | F2 |")


class TestCriticalPath(Toy):
    """Pass C: rows start and land in order of how many not-proven rows wait on them; st["width"] counts ready rows
    beside free slots and goes red after [harness] width_red_min with too few (pilot2026-11-09)."""

    def driver(self, repo: Path) -> drive.Driver:
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        return drv

    def test_ready_starts_the_row_most_others_wait_on_first(self):
        drv = self.driver(self.make_repo(functions=WIDE, max_parallel=4))
        self.assertEqual(drv.waiters(), {"F1": 0, "F2": 2, "F3": 0, "F4": 0})
        self.assertEqual([f.id for f in drv.ready()], ["F2", "F1"])  # F2 unblocks F3 and F4; F1 nothing
        drv = self.driver(self.make_repo(functions=CHAIN, max_parallel=4))
        self.assertEqual(drv.waiters()["F1"], 3)  # F2, F4 directly and F3 through F2

    def test_land_queue_orders_the_same_way(self):
        drv = self.driver(self.make_repo(functions=WIDE, max_parallel=4))
        drv.land_queue[:] = ["F1", "F2"]
        with mock.patch.object(drive, "spawn", return_value=SimpleNamespace(poll=lambda: None)):
            self.assertTrue(drv.pump_land())
        self.assertEqual((drv.land_job.fn, drv.land_queue), ("F2", ["F1"]))

    def test_one_slot_builds_the_unblocking_row_first(self):
        repo = self.make_repo(functions=WIDE, max_parallel=1)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = [(e["fn"], e["event"]) for e in self.events(repo)]
        self.assertLess(ev.index(("F2", "build_start")), ev.index(("F1", "build_start")))

    def test_width_red_after_30_min_names_the_rows_that_unblock_most(self):
        drv = self.driver(self.make_repo(functions=WIDE, max_parallel=4))
        drv.width_step()
        w = drv.st["width"]
        self.assertEqual((w["ready"], w["free"], w["slots"], w["red"]), (2, 4, 4, False))
        self.assertEqual(w["unblock"], [{"id": "F2", "waiting": 2, "status": "planned"}])
        drv.width_step()  # throttled: no recount inside WIDTH_EVERY_S
        drv.width_since -= 31 * 60
        drv.last_width = float("-inf")
        drv.width_step()
        self.assertTrue(drv.st["width"]["red"])
        self.assertEqual(drv.st["width"]["short_min"], 31)
        red = [e for e in self.events(drv.repo) if e["event"] == "width_red"]
        self.assertEqual(len(red), 1)
        self.assertIn("ready 2 < free 4 for 31 min; unblock most: F2 (2 waiting, planned)", red[0]["detail"])
        drv = self.driver(self.make_repo(functions=WIDE, max_parallel=2))
        drv.width_step()  # ready 2 fills 2 free slots: not short
        self.assertEqual((drv.st["width"]["ready"], drv.width_since), (2, None))


class TestNeeds(Toy):
    """v3: a `stub` dep does not block (its stub is on main); a `behaviour` dep must land first."""

    SLOW = {"F1": {"build": ["slow"]}}

    def order(self, repo: Path) -> list[tuple[str, str]]:
        return [(e["fn"], e["event"]) for e in self.events(repo)]

    def test_stub_dependents_start_before_their_dependency_lands(self):
        repo = self.make_repo(self.SLOW, functions=NEEDS.format(n="stub"), max_parallel=6)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.order(repo)
        for fn in ("F2", "F3", "F4"):
            self.assertLess(ev.index((fn, "build_start")), ev.index(("F1", "land_ok")), f"{fn} waited for F1")

    def test_behaviour_dependents_wait(self):
        repo = self.make_repo(self.SLOW, functions=NEEDS.format(n="behaviour"), max_parallel=6)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.order(repo)
        for fn, dep in (("F2", "F1"), ("F3", "F2"), ("F4", "F1")):
            self.assertLess(ev.index((dep, "land_ok")), ev.index((fn, "build_start")), f"{fn} before {dep}")

    def test_parse_needs(self):
        self.assertEqual(drive.parse_needs("stub", ["F1"]), [])
        self.assertEqual(drive.parse_needs("", ["F1"]), [])
        self.assertEqual(drive.parse_needs("behaviour", ["F1", "F2"]), ["F1", "F2"])
        self.assertEqual(drive.parse_needs("F1:stub, F2:behaviour", ["F1", "F2"]), ["F2"])
        self.assertIsNone(drive.parse_needs("F9:behaviour", ["F1"]))

    def test_merge_before_review_is_called_and_gates(self):
        repo = self.make_repo(functions=NEEDS.format(n="stub"), max_parallel=6)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.order(repo)
        for fn in ("F1", "F2", "F3", "F4"):
            self.assertLess(ev.index((fn, "sync")), ev.index((fn, "review_pass")))
        repo = self.make_repo(functions=NEEDS.format(n="stub"), pre_review='["lint"]',
                              commands_extra='lint = "py -3 -c \\"import sys; sys.exit(3)\\""')
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        # lint fails on clean main too: main red (mainred.py), no attempt counted, exit 3 for the manager
        self.assertIn("F1: lint red on clean main too; attempt not counted", out)
        self.assertIn("needs-plan: main red: lint", out)


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
    def test_a_third_plan_fix_of_one_function_is_refused(self):  # no endless manager rounds
        repo = self.make_repo(brief_max=50)
        for _ in range(2):
            self.assertEqual(self.drive_cli(repo)[0], 3)
            self.assertEqual(self.drive_cli(repo, "--reset", "F1")[0], 0)
        self.assertEqual(self.drive_cli(repo)[0], 3)
        rc, out = self.drive_cli(repo, "--reset", "F1")
        self.assertEqual(rc, 1, out)
        self.assertIn("already had 2 plan fixes", out)
        self.assertEqual(self.state(repo)["functions"]["F1"]["status"], "needs-plan")

    def test_an_uncommitted_plan_edit_in_main_stops_the_drive(self):  # vc1: every land then aborted
        repo = self.make_repo()
        f = repo / "spec" / "FUNCTIONS.md"
        f.write_text(f.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 1, out)
        self.assertIn("uncommitted changes to tracked files in the main checkout", out)
        self.assertIn("spec/FUNCTIONS.md", out)

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
        row = next(ln for ln in git(repo, "show", "main:spec/FUNCTIONS.md").splitlines() if ln.startswith("| F1 "))
        self.assertIn(" proven ", row)  # the status cell is committed on main, as a land would (vc1 F05)
        self.assertIn("mark F1: proven", git(repo, "log", "-1", "--format=%s", "main"))

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

    def test_an_unreadable_polish_audit_runs_once_more(self):  # trial 11: one bad escape voided the verdict
        repo = self.make_repo({"audit": [[]], "audit_garble": True})
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = [d.split(" model=")[0] for e, d in self.polish_events(repo) if e == "polish_audit"]
        self.assertEqual(ev, ["round 1 failed: audit-1.json missing or not a JSON list", "round 1: 0 issues"])

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
        r2 = self.seen("polish-audit-2.md")  # round 2 re-checks round 1's NOs and only the changed files
        self.assertIn("is a re-check", r2)
        self.assertIn("`src/alpha/x.py:3`", r2)
        self.assertIn("`src/alpha/tidy.py`", r2)
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
        self.assertEqual(len([1 for e, _ in ev if e == "polish_audit"]), 4)  # 3 polish fix rounds, then the re-audit
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
        self.assertIn("Round 1: the whole tree.", audit)
        self.assertIn("quality-standard.md", audit)
        self.assertIn("checklist.md", audit)
        self.assertIn("dedup", audit.lower())

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


def no(cid: str, line: int, evidence: str, file: str = "src/alpha/f1.py") -> dict:
    return {"id": cid, "answer": "no", "file": file, "line": line, "evidence": evidence, "fix": "fix it"}


class TestJudge(Toy):
    """v3 review rules: one pass lists every NO, only verified NOs block, the round rule, nits never dispatched."""

    def kinds(self, repo: Path, fid: str = "F1") -> list[str]:
        return [e["event"] for e in self.events(repo) if e.get("fn") == fid]

    def polish_events(self, repo: Path) -> list[tuple[str, str]]:
        return [(e["event"], e["detail"]) for e in self.events(repo) if e["event"].startswith("polish_")]

    def test_one_review_lists_every_no_and_the_retry_gets_the_whole_list(self):
        three = {"items": [no("K2", 1, "canned output"), no("C07", 2, "message twice"), no("K4", 1, "no error")]}
        repo = self.make_repo({"F1": {"review": [three, "PASS"]}}, retries=2)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        brief = self.seen("F1-build-2.md")
        self.assertIn("review FAIL: 3 verified blocking NOs; fix every one", brief)
        for text in ("canned output", "message twice", "no error"):
            self.assertIn(text, brief)
        self.assertEqual(self.seen("F1-verify-1.md").count(". [", 0), 3)  # one verify pass for the whole list
        self.assertIn("message twice", (repo / ".overhaul" / "reviews" / "F1-nos.md").read_text(encoding="utf-8"))
        self.assertEqual(self.state(repo)["functions"]["F1"]["status"], "proven")

    def test_an_unreproduced_no_does_not_block(self):
        repo = self.make_repo({"F1": {"review": ["FAIL looks like a stub"], "verify": ["none"]}})
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        kinds = self.kinds(repo)
        self.assertIn("review_unverified", kinds)
        self.assertNotIn("review_fail", kinds)
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("proven", 0))  # attempts counts failed attempts
        self.assertFalse((self.root / "seen" / "F1-build-2.md").exists())

    def test_only_reproduced_nos_reach_the_retry(self):
        two = {"items": [no("K2", 1, "real stub"), no("C07", 2, "imagined copy")]}
        repo = self.make_repo({"F1": {"review": [two, "PASS"], "verify": [[1]]}}, retries=2)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        brief = self.seen("F1-build-2.md")
        self.assertIn("1 verified blocking NOs", brief)
        self.assertIn("real stub", brief)
        self.assertNotIn("imagined copy", brief)

    def test_a_new_no_on_unchanged_code_in_round_2_is_overreach_and_does_not_block(self):
        late = {"items": [no("C07", 2, "found late on an unchanged line", "src/alpha/__init__.py")]}
        repo = self.make_repo({"F1": {"review": ["FAIL stub", late]}}, retries=2)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        kinds = self.kinds(repo)
        self.assertIn("review_overreach", kinds)
        self.assertFalse((self.root / "seen" / "F1-verify-2.md").exists())  # nothing left to verify
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("proven", 1))

    def test_nits_are_logged_and_never_dispatched(self):
        nits = {"items": [no("C06", i, f"nit {i}") for i in range(1, 8)]}
        repo = self.make_repo({"F1": {"review": [nits]}})
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = [e["detail"] for e in self.events(repo) if e["event"] == "review_nits"]
        self.assertEqual(len(ev), 1)
        self.assertIn("5 nits (+2 over the cap of 5, dropped)", ev[0])
        self.assertFalse((self.root / "seen" / "F1-verify-1.md").exists())
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec["attempts"]), ("proven", 0))  # attempts counts failed attempts

    def test_polish_runs_the_checklist_and_dedup_verifies_and_stops_after_3_rounds(self):
        repo = self.make_repo({"audit": [[finding("alpha", 3), finding("beta", 7)]]})
        toml = repo / "overhaul.toml"
        toml.write_text(toml.read_text(encoding="utf-8").replace("[limits]\n", "[limits]\npolish_rounds = 9\n"),
                        encoding="utf-8")
        git(repo, "commit", "-qam", "many rounds asked")
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        audit = self.seen("polish-audit-1.md")
        self.assertIn("K2", audit)
        self.assertIn("dedup", audit.lower())
        self.assertIn("`src/alpha/x.py:3`", self.seen("polish-verify-verify-1.md"))
        ev = self.polish_events(repo)
        self.assertEqual(ev[-1], ("polish_done", "2 issues after 3 fix rounds"))
        self.assertTrue(any(e == "polish_checklist" for e, _ in ev))



class TestPlanGaps(Toy):
    """Trial 6: five functions went needs-plan over shared-constant PLAN-GAP lines; they now batch into one pass."""

    def test_plan_gaps_batch_into_one_pass_that_runs_alone(self):
        repo = self.make_repo({"F1": {"review": ["FAIL PLAN-GAP: Optional: beta could export the label"]},
                               "F2": {"review": ["FAIL PLAN-GAP: alpha should export the label too"]},
                               "audit": [[]]})
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.events(repo)
        starts = [e for e in ev if e["event"] == "plan_gaps"]
        self.assertEqual([e["detail"] for e in starts], ["start: 2 gaps across alpha, beta"])
        brief = self.seen("polish-gaps-fix.md")
        self.assertIn("beta could export the label", brief)
        self.assertIn("alpha should export the label too", brief)
        self.assertIn("src/alpha/", brief)
        self.assertIn("src/beta/", brief)
        self.assertTrue(any(e["event"] == "polish_fix" and e["fn"] == "polish-gaps"
                            and e["detail"].startswith("landed") for e in ev), ev)
        i = ev.index(starts[0])
        end = next(k for k, e in enumerate(ev) if e["fn"] == "polish-gaps" and e["event"] == "polish_land_ok")
        self.assertFalse([e for e in ev[i:end] if e["fn"] not in (None, "polish-gaps")], ev[i:end])  # alone
        self.assertLess(end, next(k for k, e in enumerate(ev) if e["event"] == "polish_audit"))
        self.assertEqual(self.state(repo)["polish"]["gaps"], "started")


class TestFixRetry(Toy):
    """Trial 6: probe-core's fix (31 survivors) was dropped over one verified P1; a fix now gets retries ([limits] fix_retries, 2 since pass E)."""
    NO = {"checklist": "v1", "items": [{"id": "C10", "answer": "no", "file": "src/alpha/tidy.py", "line": 1,
                                        "evidence": "unused import", "fix": "remove it"}]}

    def probe_repo(self, score: float) -> Path:
        return TestProbe.probe_repo(self, score)

    def fix_events(self, repo: Path) -> list[tuple[str, str]]:
        return [(e["event"], e["detail"]) for e in self.events(repo) if e["fn"] == "probe-alpha"]

    def test_probe_fix_with_a_verified_no_gets_one_retry_then_lands(self):
        repo = self.probe_repo(0.5)
        put(self.root / "scenario.json", json.dumps({"probe-alpha": {"review": [self.NO, "PASS"]}}))
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.fix_events(repo)
        self.assertEqual(len([1 for _, d in ev if d.startswith("retry 1/2")]), 1, ev)
        self.assertIn(("probe_fix", "landed alpha"), ev)
        self.assertIn("Retry 1", self.seen("probe-alpha-fix-2.md"))
        self.assertIn("unused import", self.seen("probe-alpha-fix-2.md"))
        self.assertIn("Retry 1 of a polish fix", self.seen("probe-alpha-review-2.md"))
        self.assertEqual(self.state(repo)["probe"]["alpha"]["status"], "below_floor")

    def test_a_lone_backslash_in_an_agent_result_is_read_not_lost(self):  # trial 11 polish verifier
        f = Path(tempfile.mkdtemp()) / "v.json"
        f.write_text('[{"n": 1, "how": "git grep ' + chr(39) + chr(92) + '.file' + chr(92) + 'b' + chr(39) + '"}]',
                     encoding="utf-8")
        self.assertEqual(drive.read_json(f)[0]["n"], 1)
        f.write_text("[{", encoding="utf-8")
        self.assertIsNone(drive.read_json(f))

    def test_retry_count_is_kept_in_state_json(self):
        repo = self.probe_repo(0.5)
        put(self.root / "scenario.json", json.dumps({"probe-alpha": {"review": [self.NO]}}))
        self.drive_cli(repo)
        tries = self.state(repo).get("polish_tries", {})
        self.assertEqual(tries.get("probe-alpha", {}).get("round"), 2, tries)  # fix_retries 2

    def test_probe_fix_fails_after_its_two_retries(self):
        repo = self.probe_repo(0.5)
        put(self.root / "scenario.json", json.dumps({"probe-alpha": {"review": [self.NO]}}))
        self.drive_cli(repo)
        ev = self.fix_events(repo)
        self.assertEqual(len([1 for _, d in ev if d.startswith("retry")]), 2, ev)
        self.assertTrue(any(d.startswith("failed: review: 1 verified") for _, d in ev), ev)
        self.assertEqual(self.state(repo)["probe"]["alpha"]["status"], "fix_failed")


if __name__ == "__main__":
    unittest.main()


class TestProbe(Toy):
    """v3: after a module is proven, mutate.py runs; below [probe] mutation_floor ONE fix pass per module lands."""

    def probe_repo(self, score: float) -> Path:
        repo = self.make_repo(harness_extra="[limits]\npolish_rounds = 0\n" if False else "")
        toml = repo / "overhaul.toml"
        toml.write_text(toml.read_text(encoding="utf-8").replace("[limits]\n", "[limits]\npolish_rounds = 0\n"),
                        encoding="utf-8")
        survivor = {"file": "src/alpha/x.py", "line": 2, "op": "boundary", "before": "a > b", "after": "a >= b"}
        put(self.root / "scripts" / "mutate.py", "import json\nprint(json.dumps(" + repr(
            {"score": score, "mutants": 10, "survivors": [survivor] if score < 1 else [], "counterexamples": []})
            + "))\n")
        for m in ("alpha", "beta"):
            put(repo / "tests" / "unit" / m / "test_x.py", "def test_x():\n    pass\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "unit tests")
        return repo

    def probe_events(self, repo: Path) -> list[tuple[str, str]]:
        return [(e["event"], e["detail"]) for e in self.events(repo) if e["event"].startswith("probe_")]

    def test_below_the_floor_is_measured_again_after_each_of_two_fix_passes(self):  # trial 7: 72% never re-read
        repo = self.probe_repo(0.5)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.probe_events(repo)
        self.assertEqual(len([1 for e, _ in ev if e == "probe_mutation"]), 6, ev)
        self.assertEqual(sorted(d for e, d in ev if e == "probe_fix" and d.startswith("landed")),
                         ["landed alpha", "landed alpha", "landed beta", "landed beta"])
        self.assertIn("a >= b", self.seen("probe-alpha-fix.md"))
        self.assertEqual({m: r["status"] for m, r in self.state(repo)["probe"].items()},
                         {"alpha": "below_floor", "beta": "below_floor"})

    def test_at_or_above_the_floor_runs_no_fix(self):
        repo = self.probe_repo(0.95)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        ev = self.probe_events(repo)
        self.assertEqual(len([1 for e, _ in ev if e == "probe_mutation"]), 2, ev)
        self.assertFalse([1 for e, _ in ev if e == "probe_fix"], ev)


PHASED = """# Functions
| id | phase | module | deps | entry | acceptance | status |
|----|-------|--------|------|-------|------------|--------|
| F1 | 1 | alpha | - | `toy add 1 2` | tests/acceptance/test_f1.py::test_add | planned |
| F2 | 2 | beta | - | `toy double 4` | tests/acceptance/test_f2.py::test_double | planned |
"""


class TestScale(Toy):
    def test_phase_barrier_holds_the_next_phase_until_the_last_is_proven(self):
        repo = self.make_repo(functions=PHASED)
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        self.assertEqual([f.id for f in drv.ready()], ["F1"])  # F2 has no deps but is in phase 2
        drv.st["functions"]["F1"]["status"] = "needs-plan"
        self.assertEqual(drv.ready(), [])  # an unproven phase 1 function still holds phase 2
        drv.st["functions"]["F1"]["status"] = "proven"
        self.assertEqual([f.id for f in drv.ready()], ["F2"])

    def test_phase_barrier_off(self):
        repo = self.make_repo(functions=PHASED, harness_extra="phase_barrier = false")
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        self.assertEqual([f.id for f in drv.ready()], ["F1", "F2"])

    def test_each_running_job_gets_its_own_reused_slot_target_dir(self):
        repo = self.make_repo()
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        job = SimpleNamespace()
        self.assertEqual(drv.slot("A"), 0)
        drv.jobs["A"] = job
        self.assertEqual(drv.slot("B"), 1)
        drv.jobs["B"] = job
        self.assertEqual(drv.slot("A"), 0)  # keeps its own while it runs
        del drv.jobs["A"]
        self.assertEqual(drv.slot("C"), 0)  # A is done: its dir is reused
        drv.jobs["C"] = job
        self.assertEqual(drv.slot("A"), 2)  # A's old slot is held by C now
        env = drv.slot_env("B")
        self.assertEqual(Path(env["CARGO_TARGET_DIR"]), repo / ".overhaul" / "target" / "slot-1")
        with mock.patch.object(drive, "spawn") as sp:
            drv.start_job("B", "build", 1, repo / "brief.md", repo)
        self.assertEqual(Path(sp.call_args.args[3]["CARGO_TARGET_DIR"]).name, "slot-1")

    def test_land_gets_its_own_target_dir(self):
        repo = self.make_repo()
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        argv = json.loads((repo / ".overhaul" / "land-argv.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(argv[argv.index("--target-dir") + 1]), repo / ".overhaul" / "target" / "land-0")

    def test_main_flake_sends_a_fix_to_the_test_owner_and_lands_after_it(self):  # slice F18
        repo = self.make_repo({"F2": {"land": ["flake", "ok"]}}, retries=0)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state(repo)["functions"]["F2"]["status"], "proven")
        ev = [(e["event"], e["fn"], e["detail"]) for e in self.events(repo)]
        self.assertIn(("main_flake", "F2", "test=tests/acceptance/test_f1.py::test_add path=src/alpha/f1.py "
                                           "module=alpha"), ev)
        self.assertTrue(any(e == "dep_fix" and f == "depfix-flake-F2" and d == "landed alpha" for e, f, d in ev), ev)
        self.assertEqual([f for e, f, _ in ev if e == "build_start"].count("F2"), 1)  # no rebuild, no attempt
        self.assertFalse(any(e == "needs_plan" for e, _, _ in ev))

    def test_main_flake_twice_on_one_test_parks_the_function(self):
        repo = self.make_repo({"F2": {"land": ["flake", "flake"]}}, retries=0)
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 3, out)
        rec = self.state(repo)["functions"]["F2"]
        self.assertEqual(rec["status"], "needs-plan")
        self.assertIn("main flake on tests/acceptance/test_f1.py::test_add again", rec["last_reason"])
        ev = [(e["event"], e["fn"]) for e in self.events(repo)]
        self.assertIn(("dep_fix", "depfix-flake-F2"), ev)
        self.assertEqual(ev.count(("main_flake", "F2")), 2)


class TestResume(Toy):
    """A restart keeps the work on fn/<id> (pilot2026-11-09: 87 of 324 builds never ended, every restart reset
    their branches to main)."""

    def seed(self, repo: Path, status: str, commit: bool = True) -> str:
        """fn/F1 with one builder commit (or none), and state.json saying F1 was at status when the drive died."""
        git(repo, "branch", "fn/F1", "main")
        if commit:
            git(repo, "checkout", "-q", "fn/F1")
            put(repo / "src" / "alpha" / "kept.py", "KEPT = 1\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "keep me")
            git(repo, "checkout", "-q", "main")
        put(repo / ".overhaul" / "state.json", json.dumps({"cycle": 3, "proven_history": [], "functions": {
            "F1": {"status": status, "attempts": 0, "conflicts": 1, "last_reason": "", "branch": "fn/F1"}}}))
        return git(repo, "rev-parse", "fn/F1").strip()

    def test_building_restarts_the_builder_on_its_own_branch(self):
        repo = self.make_repo()
        tip = self.seed(repo, "building")
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        rec = drv.st["functions"]["F1"]
        self.assertEqual((rec["status"], rec.get("resume")), ("planned", "building"))
        with mock.patch.object(drive, "spawn"):
            self.assertTrue(drv.launch())
        wt = repo / ".overhaul" / "wt" / "F1"
        self.assertEqual(git(wt, "rev-parse", "HEAD").strip(), tip)  # never `-B` to main
        self.assertEqual(git(repo, "rev-parse", "fn/F1").strip(), tip)
        self.assertTrue((wt / "src" / "alpha" / "kept.py").is_file())
        rec = drv.st["functions"]["F1"]
        self.assertEqual((rec["status"], rec["conflicts"], rec.get("resume")), ("building", 1, None))
        brief = (repo / ".overhaul" / "briefs" / "F1.md").read_text(encoding="utf-8")
        self.assertIn("Resumed: the drive stopped while this attempt ran. fn/F1 keeps its 1 commits", brief)
        ev = [(e["event"], e["detail"]) for e in self.events(repo) if e["fn"] == "F1"]
        self.assertIn(("resume", "building when the last drive stopped; fn/F1 keeps 1 commits: resumes at building"),
                      ev)
        self.assertTrue(any(e == "build_start" and d.endswith(" resumed") for e, d in ev), ev)

    def test_building_with_uncommitted_work_keeps_it(self):
        repo = self.make_repo()
        self.seed(repo, "building", commit=False)
        wt = repo / ".overhaul" / "wt" / "F1"
        git(repo, "worktree", "add", "-q", str(wt), "fn/F1")
        put(wt / "src" / "alpha" / "half.py", "HALF = 1\n")  # the builder was killed before it committed
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        self.assertEqual(drv.st["functions"]["F1"].get("resume"), "building")
        self.assertIn("src/alpha/half.py", git(repo, "show", "--name-only", "fn/F1"))

    def test_a_branch_with_no_commits_starts_over(self):
        repo = self.make_repo()
        self.seed(repo, "building", commit=False)
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        rec = drv.st["functions"]["F1"]
        self.assertEqual((rec["status"], rec.get("resume")), ("planned", None))
        self.assertIn("has no commits ahead of main: starts over", self.events(repo)[-1]["detail"])

    def test_review_goes_straight_back_to_review(self):
        repo = self.make_repo()
        self.seed(repo, "review")
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        self.assertEqual(drv.st["functions"]["F1"]["status"], "review")
        with mock.patch.object(drv, "start_review") as sr, mock.patch.object(drv, "start_build") as sb:
            drv.resume_step()
            drv.launch()
        sr.assert_called_once_with("F1", 1)
        sb.assert_not_called()  # F1 is not planned; F2 waits on F1
        self.assertTrue((repo / ".overhaul" / "wt" / "F1" / "src" / "alpha" / "kept.py").is_file())

    def test_landing_lands_without_a_rebuild_end_to_end(self):
        repo = self.make_repo()
        self.seed(repo, "landing")
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state(repo)["functions"]["F1"]["status"], "proven")
        ev = [(e["event"], e["fn"]) for e in self.events(repo)]
        self.assertNotIn(("build_start", "F1"), ev)
        self.assertIn(("resume", "F1"), ev)
        self.assertIn("keep me", git(repo, "log", "--format=%s", "main"))

    def test_abort_marks_in_flight_work_for_the_next_drive(self):
        repo = self.make_repo()
        self.seed(repo, "planned")
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        drv.set("F1", status="landing")
        drv.land_queue.append("F1")
        drv.abort()
        rec = self.state(repo)["functions"]["F1"]
        self.assertEqual((rec["status"], rec.get("resume")), ("planned", "landing"))
        drv2 = drive.Driver(repo, drive.load_config(repo))
        drv2.load()
        self.assertEqual(drv2.st["functions"]["F1"]["status"], "landing")


class TestDiskGuard(Toy):
    """[limits] min_free_gb (pilot2026-11-09: 20 slot target dirs of ~10 GB filled Z: and ENOSPC killed drive)."""

    def setup_dirs(self, repo: Path) -> list[Path]:
        dirs = []
        for i, sub in enumerate(("slot-0/debug/incremental/a", "slot-1/debug/incremental/b",
                                 "land-0/release/incremental/c")):
            d = repo / ".overhaul" / "target" / sub
            put(d / "dep-graph.bin", "x" * 10)
            os.utime(d, (1000 + i, 1000 + i))  # a oldest, c newest
            dirs.append(d)
        return dirs

    def driver(self, repo: Path, dirs: list[Path], base: float) -> drive.Driver:
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        drv.free_gb = lambda: base + 20.0 * sum(not d.exists() for d in dirs)  # each dir frees 20 GB
        return drv

    def test_prunes_oldest_incremental_dirs_until_above_the_floor(self):
        repo = self.make_repo()
        dirs = self.setup_dirs(repo)
        drv = self.driver(repo, dirs, 10.0)
        self.assertTrue(drv.disk_guard("cycle"))
        self.assertEqual([d.exists() for d in dirs], [False, False, True])
        ev = [e for e in self.events(repo) if e["event"] == "disk_pruned"]
        self.assertEqual(ev[-1]["detail"], "freed 40.0 GB from 2 incremental dirs (cycle); free 50.0 GB")
        self.assertNotIn("disk_low", drv.st)

    def test_still_low_starts_no_build_and_logs_disk_low(self):
        repo = self.make_repo()
        dirs = self.setup_dirs(repo)
        drv = self.driver(repo, dirs, -50.0)  # 10 GB once all three are gone
        with mock.patch.object(drv, "start_build") as sb:
            self.assertFalse(drv.launch())
        sb.assert_not_called()
        self.assertFalse(any(d.exists() for d in dirs))
        low = [e["detail"] for e in self.events(repo) if e["event"] == "disk_low"]
        self.assertTrue(low and low[-1].startswith("disk low: 10.0 GB free < min_free_gb 40"), low)
        self.assertEqual(self.state(repo)["disk_low"]["free_gb"], 10.0)
        drv.free_gb = lambda: 100.0  # space came back
        with mock.patch.object(drv, "start_build") as sb:
            self.assertTrue(drv.launch())
        sb.assert_called()
        self.assertNotIn("disk_low", self.state(repo))
        self.assertIn("disk_ok", [e["event"] for e in self.events(repo)])

    def test_a_running_jobs_slot_is_not_pruned(self):
        repo = self.make_repo()
        dirs = self.setup_dirs(repo)
        drv = self.driver(repo, dirs, -100.0)
        drv.slots["F1"] = 0
        drv.jobs["F1"] = SimpleNamespace()
        self.assertFalse(drv.disk_guard("build start"))
        self.assertEqual([d.exists() for d in dirs], [True, False, False])

    def test_min_free_gb_zero_turns_it_off(self):
        repo = self.make_repo()
        put(repo / "overhaul.toml", (repo / "overhaul.toml").read_text(encoding="utf-8").replace(
            "[limits]\n", "[limits]\nmin_free_gb = 0\n"))
        dirs = self.setup_dirs(repo)
        drv = self.driver(repo, dirs, 0.0)
        self.assertTrue(drv.disk_guard("cycle"))
        self.assertTrue(all(d.exists() for d in dirs))
