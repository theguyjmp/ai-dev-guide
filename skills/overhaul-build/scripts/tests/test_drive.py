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

ARCH = """# Architecture

## alpha
paths: `src/alpha/`
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
fid = first.split()[-1] if review else first.split()[2]
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


if review:
    n = len(list(seen.glob(fid + "-review-*.md"))) + 1
    (seen / f"{fid}-review-{n}.md").write_text(brief, encoding="utf-8")
    verdict, _, why = pick("review", n).partition(" ")
    Path(field("- Review file to write:")).write_text(f"{verdict}\\n{why or 'checked'}\\n", encoding="utf-8")
    finish(verdict)

n = int(first.rstrip(")").split()[-1])
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
if mode == "outside":  # edits a protected file and leaves it uncommitted
    p = Path("spec/FUNCTIONS.md")
    p.write_text(p.read_text(encoding="utf-8") + "\\n<!-- edited by the builder -->\\n", encoding="utf-8")
if mode == "ok":
    p = Path("src") / field("- Module:") / (fid.lower() + ".py")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"def run():\\n    return '{fid} real work'\\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", f"{fid}: build {n}")
finish("DONE")
'''

FAKE_LAND = '''
import argparse, json, re, subprocess, sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--repo", required=True)
ap.add_argument("--fn", required=True)
a = ap.parse_args()
sc = json.loads((Path(__file__).resolve().parent.parent / "scenario.json").read_text(encoding="utf-8")).get(a.fn, {})
mode = (sc.get("land") or ["ok"])[0]
if mode == "green_before":
    print(a.fn + ": acceptance already green before change")
    sys.exit(1)


def git(*args):
    subprocess.run(["git", *args], cwd=a.repo, check=True, capture_output=True)


git("merge", "--no-edit", "fn/" + a.fn)
spec = Path(a.repo) / "spec" / "FUNCTIONS.md"
text = re.sub(r"^(\\| " + re.escape(a.fn) + r" \\|.*)\\| planned \\|[ \\t]*$", r"\\1| proven |",
              spec.read_text(encoding="utf-8"), flags=re.M)
spec.write_text(text, encoding="utf-8", newline="\\n")
git("add", "spec/FUNCTIONS.md")
git("commit", "-q", "-m", "land " + a.fn)
print("landed " + a.fn)
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
                  brief_max: int = 40000) -> Path:
        self.root = Path(tempfile.mkdtemp(prefix="drive-test-")).resolve()
        self.addCleanup(self.cleanup_root)
        repo, scripts = self.root / "repo", self.root / "scripts"
        put(self.root / "fake_worker.py", FAKE_WORKER)
        put(self.root / "scenario.json", json.dumps(scenario or {}))
        put(scripts / "land.py", FAKE_LAND)
        put(scripts / "verdict.py", "print('VERDICT GOOD fake')\n")
        put(scripts / "dash.py", "pass\n")
        put(repo / ".gitignore", ".overhaul/\n__pycache__/\n")
        put(repo / "spec" / "FUNCTIONS.md", FUNCTIONS)
        put(repo / "spec" / "ARCHITECTURE.md", ARCH)
        put(repo / "tests" / "acceptance" / "test_f1.py", ACCEPT.format(name="add"))
        put(repo / "tests" / "acceptance" / "test_f2.py", ACCEPT.format(name="double"))
        py, worker = Path(sys.executable).as_posix(), (self.root / "fake_worker.py").as_posix()
        put(repo / "overhaul.toml", f"""[commands]
test = "py -3 -m pytest -q tests/unit"
acceptance = "py -3 -m pytest -q tests/acceptance -k {{test}}"

[limits]
max_parallel = 2
builder_max_calls = 12
brief_max_tokens = {brief_max}
retries_per_function = {retries}
builder_timeout_min = {timeout_min}

[harness]
worker_cmd = ["{py}", "{worker}"]
scripts_dir = "{scripts.as_posix()}"
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
                "acceptance_source commands max_calls previous".split()
        review = "id branch row allowed_paths acceptance_source review_file diffstat diff max_calls".split()
        for name, keys in (("build-brief.md", build), ("review-brief.md", review)):
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
                       "None. This is the first attempt."):
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


if __name__ == "__main__":
    unittest.main()
