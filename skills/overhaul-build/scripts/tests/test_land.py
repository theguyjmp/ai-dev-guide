"""land.py on a toy repo: a tiny CLI (toy.py) whose acceptance tests run it via subprocess.

Run from the scripts dir: py -3 -m unittest tests.test_land
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAND = os.path.join(SCRIPTS, "land.py")
sys.path.insert(0, SCRIPTS)
import land  # noqa: E402

PY = sys.executable

# A stand-in for pytest: PATH [NAME]; exit 0 pass, 1 fail, 5 no tests collected.
PYTEST_LITE = '''import pathlib, runpy, sys
path = pathlib.Path(sys.argv[1]); name = sys.argv[2] if len(sys.argv) > 2 else None
files = sorted(path.glob("test_*.py")) if path.is_dir() else ([path] if path.exists() else [])
ran = failed = 0
for f in files:
    for key, fn in runpy.run_path(str(f)).items():
        if key.startswith("test") and callable(fn) and name in (None, key):
            ran += 1
            try:
                fn()
            except Exception as e:
                failed += 1
                print(f"FAIL {f}::{key}: {e!r}")
print(f"{ran} ran, {failed} failed")
sys.exit(5 if ran == 0 else 1 if failed else 0)
'''

LINT = '''import pathlib, sys
bad = [str(p) for p in pathlib.Path(".").rglob("*.py") if p.name != "lint.py" and "BADLINT" in p.read_text()]
print("lint:", bad or "clean")
sys.exit(1 if bad else 0)
'''

ACCEPTANCE = '''import subprocess, sys


def test_{op}():
    out = subprocess.run([sys.executable, "toy.py", "{op}", "2", "3"], capture_output=True, text=True).stdout
    assert out.strip() == "{want}", out
'''

FUNCTIONS = """# Functions

| id | phase | module | deps | entry | acceptance | status |
|----|-------|--------|------|-------|------------|--------|
| F1 | 1 | toy | - | toy.py | `tests/acceptance/test_add.py::test_add` | proven |
| F2 | 1 | toy | F1 | toy.py | `tests/acceptance/test_mul.py::test_mul` | planned |
"""

TOML = f"""[commands]
test = '"{PY}" pytest_lite.py tests/unit'
acceptance = '"{PY}" pytest_lite.py {{file}} {{test}}'
lint = '"{PY}" lint.py'
[paths]
functions = "spec/FUNCTIONS.md"
"""


def toy(add="a + b", mul=None, extra=""):
    ops = f'    if op == "add":\n        print({add})\n        return 0\n'
    if mul:
        ops += f'    if op == "mul":\n        print({mul})\n        return 0\n'
    return ("import sys\n\n\ndef main(argv):\n    op, a, b = argv[0], int(argv[1]), int(argv[2])\n" + ops
            + '    print("unknown op", op)\n    return 2\n\n\nif __name__ == "__main__":\n'
            + "    sys.exit(main(sys.argv[1:]))\n" + extra)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout.strip()


def write(repo, files):
    for rel, text in files.items():
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


def _force_remove(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


class LandTest(unittest.TestCase):
    def make_repo(self, main_toy=None, branch=None, unit=True):
        """main: the plan (F1 proven, F2 planned); fn/F2: `branch` files; then main moves on (a README commit)."""
        repo = tempfile.mkdtemp(prefix="landtoy-")
        kw = {"onexc": _force_remove} if sys.version_info >= (3, 12) else {"onerror": _force_remove}
        self.addCleanup(shutil.rmtree, repo, **kw)
        subprocess.run(["git", "init", "-q", repo], check=True)
        git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        for key, val in (("user.name", "toy"), ("user.email", "toy@example.com"), ("commit.gpgsign", "false"),
                         ("core.autocrlf", "false")):
            git(repo, "config", key, val)
        files = {".gitignore": ".overhaul/\n__pycache__/\n", "overhaul.toml": TOML, "spec/FUNCTIONS.md": FUNCTIONS,
                 "pytest_lite.py": PYTEST_LITE, "lint.py": LINT, "toy.py": main_toy or toy(),
                 "tests/acceptance/test_add.py": ACCEPTANCE.format(op="add", want=5),
                 "tests/acceptance/test_mul.py": ACCEPTANCE.format(op="mul", want=6)}
        if unit:
            files["tests/unit/test_smoke.py"] = "def test_smoke():\n    assert True\n"
        write(repo, files)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "plan")
        git(repo, "checkout", "-q", "-b", "fn/F2")
        write(repo, branch or {"toy.py": toy(mul="a * b")})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "build F2")
        git(repo, "checkout", "-q", "main")
        write(repo, {"README.md": "toy\n"})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "readme")
        return repo

    def land(self, repo, *extra):
        r = subprocess.run([PY, LAND, "--repo", repo, "--fn", "F2", "--wait", "0", *extra],
                           capture_output=True, text=True, timeout=300)
        return r.returncode, r.stdout.strip(), r.stderr

    def log(self, repo):
        with open(os.path.join(repo, ".overhaul", "log.jsonl"), encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def state(self, repo):
        with open(os.path.join(repo, ".overhaul", "state.json"), encoding="utf-8") as f:
            return json.load(f)

    def assert_failed(self, repo, before, code, out, prefix):
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith(prefix), out)
        self.assertEqual(len(out.splitlines()), 1, out)
        self.assertEqual(git(repo, "rev-parse", "main"), before)
        self.assertEqual(git(repo, "status", "--porcelain"), "")
        last = self.log(repo)[-1]
        self.assertEqual((last["fn"], last["event"], last["detail"], last["tokens"]), ("F2", "land_fail", out, None))
        entry = self.state(repo)["functions"]["F2"]
        self.assertEqual((entry["status"], entry["last_reason"]), ("planned", out))

    def test_landed_ok(self):
        repo = self.make_repo()
        before = git(repo, "rev-parse", "main")
        code, out, err = self.land(repo)
        self.assertEqual(code, 0, out + err)
        head = git(repo, "rev-parse", "main")
        self.assertEqual(git(repo, "log", "-1", "--format=%s", "main"), "land F2: proven")
        self.assertEqual(git(repo, "diff", "--name-only", "main~1", "main"), "spec/FUNCTIONS.md")
        self.assertEqual(len(git(repo, "log", "-1", "--format=%P", "main~1").split()), 2)  # main merged into fn/F2
        subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", before, head], check=True)
        self.assertEqual(git(repo, "rev-parse", "fn/F2"), head)
        self.assertRegex(git(repo, "show", "main:spec/FUNCTIONS.md"), r"(?m)^\| F2 \|.*\| proven *\|$")
        self.assertEqual(git(repo, "status", "--porcelain"), "")  # R's own working tree moved with main
        with open(os.path.join(repo, "toy.py"), encoding="utf-8") as f:
            self.assertIn('"mul"', f.read())
        st = self.state(repo)
        self.assertEqual(st["functions"]["F2"]["status"], "proven")
        self.assertEqual(st["functions"]["F2"]["branch"], "fn/F2")
        self.assertEqual(st["proven_history"][-1][1], 2)
        last = self.log(repo)[-1]
        self.assertEqual((last["event"], last["tokens"]), ("land_ok", None))
        self.assertFalse(os.path.exists(os.path.join(repo, ".overhaul", "lock")))

    def test_already_green_before(self):
        repo = self.make_repo(main_toy=toy(mul="a * b"), branch={"toy.py": toy(mul="a * b", extra="# tweak\n")})
        before = git(repo, "rev-parse", "main")
        code, out, _ = self.land(repo)
        self.assertEqual(out, "acceptance already green before change")
        self.assert_failed(repo, before, code, out, "acceptance already green before change")

    def test_still_red_after(self):
        repo = self.make_repo(branch={"toy.py": toy(mul="a + b")})
        before = git(repo, "rev-parse", "main")
        code, out, _ = self.land(repo)
        self.assert_failed(repo, before, code, out, "acceptance still red after merge: tests/acceptance/test_mul.py")

    def test_regression_of_proven_row(self):
        repo = self.make_repo(branch={"toy.py": toy(add="a - b", mul="a * b")})
        before = git(repo, "rev-parse", "main")
        code, out, _ = self.land(repo)
        self.assert_failed(repo, before, code, out, "regression: proven F1")

    def test_lint_fail(self):
        repo = self.make_repo(branch={"toy.py": toy(mul="a * b", extra="# BADLINT\n")})
        before = git(repo, "rev-parse", "main")
        code, out, _ = self.land(repo)
        self.assert_failed(repo, before, code, out, "lint failed (exit 1)")

    def test_branch_edits_acceptance(self):
        repo = self.make_repo(branch={"toy.py": toy(mul="a * b"),
                                      "tests/acceptance/test_mul.py": "def test_mul():\n    pass\n"})
        before = git(repo, "rev-parse", "main")
        code, out, _ = self.land(repo)
        self.assert_failed(repo, before, code, out, "fn/F2 edits plan files: tests/acceptance/test_mul.py")

    def test_lock_held(self):
        repo = self.make_repo()
        before = git(repo, "rev-parse", "main")
        lock = os.path.join(repo, ".overhaul", "lock")
        os.makedirs(os.path.dirname(lock))
        with open(lock, "w", encoding="utf-8") as f:
            f.write(f"drive.py pid {os.getpid()}\n")
        code, out, _ = self.land(repo)
        self.assertEqual(code, 1)
        self.assertEqual(out, f"lock held: .overhaul/lock (drive.py pid {os.getpid()})")
        self.assertTrue(os.path.exists(lock))
        self.assertEqual(git(repo, "rev-parse", "main"), before)
        self.assertFalse(os.path.exists(os.path.join(repo, ".overhaul", "log.jsonl")))

    def test_pytest_exit_5_is_pass(self):
        repo = self.make_repo(unit=False)  # tests/unit is empty: the test command collects nothing, exit 5
        code, out, err = self.land(repo)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(land.passes("py -3 -m pytest -q tests/unit", 5))
        self.assertFalse(land.passes("py -3 -m pylint src", 5))
        self.assertFalse(land.passes("py -3 -m pytest -q tests/unit", 1))


class FunctionsTableTest(unittest.TestCase):
    def test_set_status_keeps_the_rest_of_the_row(self):
        rows = land.parse_functions(FUNCTIONS)
        self.assertEqual(rows["F1"]["acceptance"], ["tests/acceptance/test_add.py::test_add"])
        self.assertEqual(rows["F2"]["status"], "planned")
        out = land.set_status(FUNCTIONS, rows["F2"])
        self.assertIn("| F2 | 1 | toy | F1 | toy.py | `tests/acceptance/test_mul.py::test_mul` | proven  |\n", out)
        self.assertEqual(out.replace("proven  |", "planned |"), FUNCTIONS)
        self.assertTrue(re.search(r"\| F1 \|.*\| proven \|", out))


if __name__ == "__main__":
    unittest.main()
