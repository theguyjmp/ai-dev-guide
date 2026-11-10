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

    def test_polish_lands_a_green_change_with_no_row_and_no_state_entry(self):
        repo = self.make_repo(main_toy=toy(mul="a * b"), branch={"toy.py": toy(mul="a * b", extra="# tidy\n")})
        before = git(repo, "rev-parse", "main")
        code, out, err = self.land(repo, "--polish")
        self.assertEqual(code, 0, out + err)
        self.assertNotEqual(git(repo, "rev-parse", "main"), before)
        self.assertEqual(git(repo, "show", "main:spec/FUNCTIONS.md"), FUNCTIONS.strip())
        self.assertIn("# tidy", git(repo, "show", "main:toy.py"))
        self.assertEqual(self.log(repo)[-1]["event"], "polish_land_ok")
        self.assertFalse(os.path.exists(os.path.join(repo, ".overhaul", "state.json")))

    def test_polish_that_breaks_a_proven_row_fails_and_leaves_main(self):
        repo = self.make_repo(branch={"toy.py": toy(add="a - b")})
        before = git(repo, "rev-parse", "main")
        code, out, _ = self.land(repo, "--polish")
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith("regression: proven F1"), out)
        self.assertEqual(git(repo, "rev-parse", "main"), before)
        self.assertEqual((self.log(repo)[-1]["event"], self.log(repo)[-1]["detail"]), ("polish_land_fail", out))

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


class StackedLandTest(LandTest):
    """--stack: a land whose footprint is disjoint from the one in flight gates on main + that branch + its own,
    outside the lock, and commits right after it with no second gate; if the stacked land fails it regates."""

    def make_stacked(self, f2_files=None):
        repo = self.make_repo(branch=f2_files)
        extra = ("import sys\n\nop, a, b = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])\n"
                 "print(a - b if op == 'sub' else '?')\n")
        sub = ACCEPTANCE.format(op="sub", want=-1).replace('"toy.py"', '"extra.py"')
        rows = FUNCTIONS + "| F3 | 1 | toy | F1 | extra.py | `tests/acceptance/test_sub.py::test_sub` | planned |\n"
        write(repo, {"spec/FUNCTIONS.md": rows, "tests/acceptance/test_sub.py": sub})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "plan F3")
        git(repo, "checkout", "-q", "-b", "fn/F3")
        write(repo, {"extra.py": extra})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "build F3")
        git(repo, "checkout", "-q", "main")
        return repo

    def start_stacked(self, repo):
        p = subprocess.Popen([PY, LAND, "--repo", repo, "--fn", "F3", "--stack", "F2", "--timeout", "120"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        wt = os.path.join(repo, ".overhaul", "wt", "land-F3")
        for _ in range(600):  # its worktree exists once it has read main
            if os.path.isdir(wt) or p.poll() is not None:
                break
            import time
            time.sleep(0.05)
        return p

    def test_stacked_land_commits_after_the_first_without_a_second_gate(self):
        repo = self.make_stacked()
        p = self.start_stacked(repo)
        code, out, err = self.land(repo, "--wait", "60")
        self.assertEqual(code, 0, out + err)
        out3, err3 = p.communicate(timeout=300)
        self.assertEqual(p.returncode, 0, out3 + err3)
        self.assertIn("stacked on F2, no second gate", out3)
        rows = git(repo, "show", "main:spec/FUNCTIONS.md")
        self.assertRegex(rows, r"(?m)^\| F2 \|.*\| proven *\|$")
        self.assertRegex(rows, r"(?m)^\| F3 \|.*\| proven *\|$")
        self.assertIn('"mul"', git(repo, "show", "main:toy.py"))
        self.assertIn("sub", git(repo, "show", "main:extra.py"))
        self.assertEqual(git(repo, "log", "-1", "--format=%s", "main"), "land F3: proven")
        self.assertEqual([e["event"] for e in self.log(repo)], ["land_ok", "land_ok"])

    def test_stacked_land_regates_when_the_first_fails(self):
        repo = self.make_stacked(f2_files={"toy.py": toy(mul="a * b") + "# BADLINT\n"})
        p = self.start_stacked(repo)
        code, out, _ = self.land(repo, "--wait", "60")
        self.assertEqual(code, 1, out)
        out3, err3 = p.communicate(timeout=300)
        self.assertEqual(p.returncode, 0, out3 + err3)
        self.assertIn("gated alone on the new main", out3)
        self.assertNotIn('"mul"', git(repo, "show", "main:toy.py"))  # the failed land's code never reaches main
        self.assertRegex(git(repo, "show", "main:spec/FUNCTIONS.md"), r"(?m)^\| F3 \|.*\| proven *\|$")


# A stand-in for pytest run as `-m pytest` from the repo root: node ids in, `FAILED <node>` lines and a summary out;
# every call's node ids go to CALLS so a test can count the commands.
FAKE_PYTEST = """import json, runpy, sys
args = [a for a in sys.argv[1:] if not a.startswith("-")]
with open(CALLS, "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + chr(10))
passed = failed = 0
for node in args:
    path, _, name = node.partition("::")
    try:
        runpy.run_path(path)[name]()
        passed += 1
    except Exception as e:
        failed += 1
        print(f"FAILED {node} - {e!r}")
print(f"{failed} failed, {passed} passed in 0.01s")
sys.exit(1 if failed else 0 if passed else 5)
"""


class BatchedRegressionTest(LandTest):
    def test_proven_rows_run_in_one_command_and_the_failure_maps_to_its_row(self):
        repo = self.make_repo(branch={"toy.py": toy(add="a - b", mul="a * b")})
        calls = os.path.join(repo, ".overhaul-calls.jsonl")
        functions = FUNCTIONS.replace(
            "| F2 |", "| F0 | 1 | toy | - | toy.py | `tests/acceptance/test_add.py::test_zero` | proven |\n| F2 |")
        write(repo, {"pytest.py": f"CALLS = {calls!r}\n" + FAKE_PYTEST, "spec/FUNCTIONS.md": functions,
                     "tests/acceptance/test_add.py": ACCEPTANCE.format(op="add", want=5)
                     + "\n\ndef test_zero():\n    assert True\n",
                     "overhaul.toml": TOML.replace("pytest_lite.py {file} {test}", "-m pytest -q {node}"),
                     ".gitignore": ".overhaul/\n__pycache__/\n.overhaul-calls.jsonl\n"})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "two proven rows in one test file")
        before = git(repo, "rev-parse", "main")
        code, out, _ = self.land(repo)
        self.assert_failed(repo, before, code, out, "regression: proven F1 tests/acceptance/test_add.py::test_add")
        with open(calls, encoding="utf-8") as f:
            runs = [json.loads(x) for x in f if x.strip()]
        both = sorted(["tests/acceptance/test_add.py::test_add", "tests/acceptance/test_add.py::test_zero"])
        self.assertIn(both, [sorted(r) for r in runs])  # both proven rows in one command
        self.assertEqual(len([r for r in runs if "tests/acceptance/test_add.py::test_zero" in r]), 1, runs)


class TiersAndExtraTest(LandTest):
    """[commands.acceptance_by] per-glob templates, footprint-selected [tiers] tests, [commands.extra] gates."""

    def plan_commit(self, repo, files, msg="plan change"):
        write(repo, files)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", msg)

    def test_extra_command_runs_on_the_merged_tree(self):
        repo = self.make_repo()
        self.plan_commit(repo, {"overhaul.toml": TOML.replace(
            "[paths]", f"[commands.extra]\nstyle = '\"{PY}\" -c \"print(7); import sys; sys.exit(3)\"'\n[paths]")})
        before = git(repo, "rev-parse", "main")
        code, out, _ = self.land(repo)
        self.assert_failed(repo, before, code, out, "extra.style failed (exit 3) on merged tree")

    def test_acceptance_by_gives_a_test_file_its_own_template(self):
        repo = self.make_repo()
        never = f"\"{PY}\" -c \"print(9); import sys; sys.exit(1)\" {{test}}"
        self.plan_commit(repo, {"overhaul.toml": TOML.replace(
            "[paths]", f"[commands.acceptance_by]\n\"tests/acceptance/test_mul*.py\" = '{never}'\n[paths]")})
        before = git(repo, "rev-parse", "main")
        code, out, _ = self.land(repo)  # red before, then the always-red template keeps it red after the merge
        self.assert_failed(repo, before, code, out, "acceptance still red after merge: tests/acceptance/test_mul.py")

    def tier_repo(self, core_paths):
        """F1 (module core, its test in the scale tier) is broken by the branch, which touches only toy.py."""
        repo = self.make_repo(branch={"toy.py": toy(add="a - b", mul="a * b")})
        arch = f"# Architecture\n\n## Module core\n\nPaths: {core_paths}\n\n## Module toy\n\nPaths: toy.py\n"
        self.plan_commit(repo, {"spec/ARCHITECTURE.md": arch,
                                "spec/FUNCTIONS.md": FUNCTIONS.replace("| F1 | 1 | toy |", "| F1 | 1 | core |"),
                                "overhaul.toml": TOML + '[tiers]\nscale = ["tests/acceptance/test_add*.py"]\n'})
        return repo

    def test_scale_tier_test_outside_the_footprint_is_skipped(self):
        repo = self.tier_repo("`core/`")
        code, out, err = self.land(repo)
        self.assertEqual(code, 0, out + err)

    def test_scale_tier_test_of_a_touched_module_still_runs(self):
        repo = self.tier_repo("`core/`, `toy.py`")
        before = git(repo, "rev-parse", "main")
        code, out, _ = self.land(repo)
        self.assert_failed(repo, before, code, out, "regression: proven F1 tests/acceptance/test_add.py::test_add")


class TierHelpersTest(unittest.TestCase):
    def test_template_per_glob_and_batches_per_template(self):
        t = land.acceptance_template({"acceptance": "cargo test --test {stem} -- --exact {test}",
                                      "acceptance_by": {"tests/acceptance/scale_*.rs":
                                                        "cargo test --release --test {stem} -- --exact {test} "
                                                        "--test-threads=1"}})
        nodes = ["tests/acceptance/a.rs::x", "tests/acceptance/scale_b.rs::z", "tests/acceptance/a.rs::y",
                 "tests/acceptance/scale_b.rs::w"]
        self.assertEqual(land.batch_cmds(t, nodes), [
            (["tests/acceptance/a.rs::x", "tests/acceptance/a.rs::y"], "cargo test --test a -- --exact x y"),
            (["tests/acceptance/scale_b.rs::z", "tests/acceptance/scale_b.rs::w"],
             "cargo test --release --test scale_b -- --exact z w --test-threads=1")])
        self.assertEqual(land.acceptance_cmds(t, ["tests/acceptance/scale_b.rs::z"])[0][1],
                         "cargo test --release --test scale_b -- --exact z --test-threads=1")

    def test_no_new_keys_is_the_plain_template(self):
        self.assertEqual(land.acceptance_template({"acceptance": "pytest {node}"}), "pytest {node}")
        self.assertEqual(land.gate_cmds({"test": "t", "lint": "l"}), [("test", "t"), ("lint", "l")])
        self.assertEqual(land.tier_globs({}), [])

    def test_extra_follows_whole_tree_and_modules_by_paths(self):
        self.assertEqual(land.gate_cmds({"lint": "l", "extra": {"css": "stylelint", "i18n": "py i18n.py"}}),
                         [("lint", "l"), ("extra.css", "stylelint"), ("extra.i18n", "py i18n.py")])
        self.assertEqual(land.tier_globs({"tiers": {"scale": ["a*", "b*"], "gpu": ["g*"]}}), ["a*", "b*", "g*"])
        arch = "## Module doc\n- **Paths:** `crates/doc/`, `crates/x/*.rs`\n## Module ui\nPaths: crates/ui\n"
        self.assertEqual(land.touched_modules(arch, ["crates/ui/src/a.rs"]), {"ui"})
        self.assertEqual(land.touched_modules(arch, ["crates/x/y.rs", "README.md"]), {"doc"})
        self.assertEqual(land.touched_modules(arch, ["crates/uix/a.rs"]), set())


LIBTEST_OUT = """
running 3 tests
test store::a ... ok
test store::b ... FAILED
test store::c ... ok

failures:

---- store::b stdout ----
thread 'store::b' panicked at src/store.rs:9:5

failures:
    store::b

test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s
"""


class BatchResultsTest(unittest.TestCase):
    def results(self, template, nodes, outputs):
        """batch_results with a runner that answers each call from `outputs` (exit code, output) in turn."""
        seen = []

        def runner(cmd):
            seen.append(cmd)
            code, out = outputs[min(len(seen), len(outputs)) - 1]
            return code, out.strip().splitlines()[-1] if out.strip() else "", out
        return land.batch_results(template, nodes, runner), seen

    def test_libtest_failure_maps_to_its_node_in_one_run(self):
        nodes = ["tests/store.rs::store::a", "tests/store.rs::store::b", "tests/store.rs::store::c"]
        res, seen = self.results("cargo test -q --test {stem} -- --exact {test}", nodes, [(101, LIBTEST_OUT)])
        self.assertEqual(seen, ["cargo test -q --test store -- --exact store::a store::b store::c"])
        self.assertEqual({n: r[0] for n, r in res.items()}, {nodes[0]: 0, nodes[1]: 101, nodes[2]: 0})

    def test_pytest_failure_maps_to_its_node_in_one_run(self):
        nodes = ["tests/acceptance/test_a.py::test_x", "tests/acceptance/test_a.py::test_y"]
        out = "FAILED tests/acceptance/test_a.py::test_y - assert 0\n1 failed, 1 passed in 0.12s\n"
        res, seen = self.results("py -3 -m pytest -q {node}", nodes, [(1, out)])
        self.assertEqual(len(seen), 1)
        self.assertEqual({n: r[0] for n, r in res.items()}, {nodes[0]: 0, nodes[1]: 1})

    def test_a_batch_with_no_summary_reruns_each_node_alone(self):
        nodes = ["tests/store.rs::a", "tests/store.rs::b"]
        res, seen = self.results("cargo test -q --test {stem} -- --exact {test}", nodes,
                                 [(101, "error[E0425]: cannot find value `x`"), (0, "ok"), (101, "boom")])
        self.assertEqual(len(seen), 3)
        self.assertEqual({n: (r[0], r[2]) for n, r in res.items()}, {nodes[0]: (0, True), nodes[1]: (101, True)})

    def test_a_green_batch_passes_every_node(self):
        nodes = ["tests/a.py::test_x", "tests/a.py::test_y", "tests/b.py::test_z"]
        res, seen = self.results("py -3 -m pytest -q {file} -k {test}", nodes, [(0, "3 passed in 0.1s")])
        self.assertEqual(len(seen), 2)  # one command per test file (-k joins the names)
        self.assertTrue(all(r[0] == 0 for r in res.values()))


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
