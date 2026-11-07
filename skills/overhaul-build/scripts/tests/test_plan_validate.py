"""Tests for plan_validate.py (overhaul v2). Run from the scripts dir:  py -3 -m unittest

Each test builds a toy repo in a temp dir: a tiny CLI (src/tiny.py), acceptance tests run by pytest, FUNCTIONS.md,
ARCHITECTURE.md and overhaul.toml. cycles and entry_lint are fake commands. Only the --run tests need pytest.
"""
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import plan_validate as pv  # noqa: E402

try:
    import pytest  # noqa: F401
    HAVE_PYTEST = True
except ImportError:
    HAVE_PYTEST = False

PY = "py -3" if shutil.which("py") else ("python3" if shutil.which("python3") else "python")
OK = f'{PY} -c "import sys; sys.exit(0)"'
BAD = f'{PY} -c "import sys; sys.exit(1)"'
FAST = "exit 0"  # the same fake without a Python start-up, for the many tests that do not look at cycles or entry_lint
ACCEPT = f"{PY} -m pytest -q -p no:cacheprovider tests/acceptance -k {{test}}"

CLI = """import sys


def main(argv):
    if len(argv) == 3 and argv[0] == "add":
        print(int(argv[1]) + int(argv[2]))
        return 0
    print("usage: tiny add A B", file=sys.stderr)
    return 2


sys.exit(main(sys.argv[1:]))
"""
HEAD = """import subprocess
import sys
from pathlib import Path

CLI = Path(__file__).resolve().parents[2] / "src" / "tiny.py"


def tiny(*args):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True).stdout.strip()


"""
CALC = HEAD + """def test_add():
    assert tiny("add", "2", "3") == "5"


def test_sub():
    assert tiny("sub", "5", "3") == "2"
"""
REPORT = HEAD + """def test_report_a():
    assert tiny("report") == "ok"


def test_report_b():
    assert tiny("report", "--all") == "ok"
"""
ARCH = """# Architecture

## Module calc
Paths: src/tiny.py
Depends on: -

## Module report
Paths: src/report.py
Depends on: calc
"""
CALC_T = "tests/acceptance/test_calc.py"
REPORT_T = "tests/acceptance/test_report.py"


def row(i="F001", phase="1", module="calc", deps="-", entry="`tiny add A B`", acc=f"{CALC_T}::test_add", status="proven"):
    return f"| {i} | {phase} | {module} | {deps} | {entry} | {acc} | {status} |"


def table(*rows):
    return "# Functions\n\n| id | phase | module | deps | entry | acceptance | status |\n|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n"


R1 = row()
R2 = row("F002", deps="F001", entry="`tiny sub A B`", acc=f"{CALC_T}::test_sub", status="planned")


def r3(**kw):
    d = dict(i="F003", phase="2", module="report", deps="F001, F002", entry="`tiny report`",
             acc=f"{REPORT_T}::test_report_a; {REPORT_T}::test_report_b", status="planned")
    d.update(kw)
    return row(**d)


def toml(**over):
    cmds = {"test": OK, "acceptance": ACCEPT, "lint": OK, "dup": OK, "cycles": FAST, "entry_lint": FAST}
    cmds.update(over)
    body = "\n".join(f"{k} = '{v}'" for k, v in cmds.items() if v is not None)
    return f"[commands]\n{body}\n[limits]\nmax_parallel = 4\n[paths]\nfunctions = 'spec/FUNCTIONS.md'\n"


class Base(unittest.TestCase):
    def make(self, functions=None, arch=ARCH, toml_text=None, **cmds):
        root = Path(tempfile.mkdtemp(prefix="pv_toy_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        files = {"src/tiny.py": CLI, "tests/acceptance/test_calc.py": CALC, "tests/acceptance/test_report.py": REPORT,
                 "spec/FUNCTIONS.md": functions if functions is not None else table(R1, R2, r3()),
                 "overhaul.toml": toml_text if toml_text is not None else toml(**cmds)}
        if arch is not None:
            files["spec/ARCHITECTURE.md"] = arch
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        return root

    def problems(self, root, **kw):
        return pv.validate(root, **kw)[0]

    def has(self, problems, *needles):
        return any(all(n in p for n in needles) for p in problems)

    def only(self, problems, *needles):
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(self.has(problems, *needles), problems)


class StaticChecks(Base):
    def test_clean_plan_passes(self):
        self.assertEqual(pv.validate(self.make(cycles=OK, entry_lint=OK)), ([], 3))

    def test_malformed_rows(self):
        cases = {
            "6 cells": (f"| F003 | 2 | report | F001 | `x` | {REPORT_T}::test_report_a |", "row has 6 cells"),
            "phase 0": (r3(phase="0"), "malformed phase"),
            "phase text": (r3(phase="two"), "malformed phase"),
            "phase decimal": (r3(phase="1.5"), "malformed phase"),
            "status": (r3(status="done"), "malformed status"),
            "deps space": (r3(deps="F001 F002"), "malformed deps"),
            "deps empty item": (r3(deps="F001,,F002"), "malformed deps"),
            "deps blank": (r3(deps=""), "malformed deps"),
            "acceptance no prefix": (r3(acc="test_report.py::test_report_a"), "malformed acceptance"),
            "acceptance no test": (r3(acc=REPORT_T), "malformed acceptance"),
            "acceptance parent dir": (r3(acc="tests/acceptance/../x.py::test_a"), "malformed acceptance"),
            "acceptance empty": (r3(acc=""), "no acceptance"),
            "id": (r3(i="bad id"), "malformed id"),
            "module empty": (r3(module=""), "malformed module"),
        }
        for name, (bad, needle) in cases.items():
            with self.subTest(name):
                self.only(self.problems(self.make(table(R1, R2, bad))), needle)

    def test_duplicate_id_and_missing_table(self):
        self.only(self.problems(self.make(table(R1, R2, r3(), r3(i="F002")))), "duplicate id")
        self.only(self.problems(self.make("# Functions\n\nnothing here\n")), "no table with the header")
        self.assertTrue(self.has(self.problems(self.make(table())), "no function rows"))

    def test_acceptance_file_and_def_missing(self):
        p = self.problems(self.make(table(R1, R2, r3(acc="tests/acceptance/test_nope.py::test_x"))))
        self.only(p, "acceptance file tests/acceptance/test_nope.py is missing")
        p = self.problems(self.make(table(R1, R2, r3(acc=f"{REPORT_T}::test_report_a; {REPORT_T}::test_zzz"))))
        self.only(p, "test_report.py has no def test_zzz")
        p = self.problems(self.make(table(R1, row("F002", acc=f"{CALC_T}::test_ad", deps="F001", status="planned"))))
        self.only(p, "has no def test_ad")  # a prefix of test_add is not a match

    def test_deps(self):
        self.only(self.problems(self.make(table(R1, R2, r3(deps="F001, F999")))), "dep F999 is unknown")
        later = table(R1, row("F002", deps="F003", acc=f"{CALC_T}::test_sub", status="planned"), r3(deps="F001"))
        self.only(self.problems(self.make(later)), "F002", "dep F003 is in phase 2", "later phase")
        same = table(R1, R2, r3(phase="1"))  # the same phase is allowed
        self.assertEqual(self.problems(self.make(same)), [])
        loop = table(row(deps="F002"), R2, r3())
        self.only(self.problems(self.make(loop)), "dependency cycle")
        self.only(self.problems(self.make(table(row(deps="F001"), R2, r3()))), "dependency cycle F001 -> F001")

    def test_module_unknown_and_architecture_missing(self):
        self.only(self.problems(self.make(table(R1, R2, r3(module="ghost")))), "F003", "module 'ghost' is not in ARCHITECTURE")
        self.only(self.problems(self.make(arch=None)), "spec/ARCHITECTURE.md: missing")
        self.assertTrue(self.has(self.problems(self.make(arch="# Architecture\n\njust prose\n")), "no `## Module"))

    def test_architecture_structure(self):
        no_paths = ARCH.replace("Paths: src/tiny.py\n", "")
        self.only(self.problems(self.make(arch=no_paths)), "module calc", "no Paths:")
        no_deps = ARCH.replace("Depends on: -\n", "")
        self.only(self.problems(self.make(arch=no_deps)), "module calc", "no Depends on:")
        ghost = ARCH.replace("Depends on: calc", "Depends on: calc, ghost")
        self.only(self.problems(self.make(arch=ghost)), "Depends on unknown module ghost")
        cyc = ARCH.replace("Depends on: -", "Depends on: report")
        self.only(self.problems(self.make(arch=cyc)), "module cycle")
        styled = "## Module calc\n- **Paths:** `src/tiny.py`\n- **Depends on:** none\n### API\ntext\n## Module report\nPaths: src/r.py\nDepends on: calc\n"
        self.assertEqual(self.problems(self.make(arch=styled)), [])

    def test_no_entry(self):
        for entry in ("", "-", "`-`", "TBD"):
            with self.subTest(entry=entry):
                self.only(self.problems(self.make(table(R1, R2, r3(entry=entry)))), "F003", "no entry")

    def test_missing_commands(self):
        for k in pv.COMMANDS:
            with self.subTest(command=k):
                self.only(self.problems(self.make(**{k: None})), f"[commands].{k} is missing")
        self.only(self.problems(self.make(acceptance=f"{PY} -m pytest tests/acceptance")), "no {test} placeholder")
        self.only(self.problems(self.make(lint="  ")), "[commands].lint is missing")
        self.assertEqual(len(self.problems(self.make(toml_text="[limits]\nmax_parallel = 4\n"))), len(pv.COMMANDS))

    def test_toml_missing_or_broken(self):
        root = self.make()
        (root / "overhaul.toml").unlink()
        self.only(self.problems(root), "overhaul.toml: missing")
        self.only(self.problems(self.make(toml_text="[commands\nx = 1\n")), "overhaul.toml: unreadable")

    def test_cycles_and_entry_lint_run_through_toml(self):
        self.only(self.problems(self.make(cycles=BAD)), "cycles command exited 1")
        self.only(self.problems(self.make(entry_lint=BAD)), "entry_lint command exited 1")
        talk = f'{PY} -c "import sys; print(sys.argv[1]); sys.exit(3)" a-b-a'
        self.only(self.problems(self.make(cycles=talk)), "cycles command exited 3: a-b-a")
        both = self.problems(self.make(cycles=BAD, entry_lint=BAD))
        self.assertEqual(len(both), 2, both)
        at_root = f'{PY} -c "import os, sys; sys.exit(0 if os.path.isfile(sys.argv[1]) else 1)" overhaul.toml'
        self.assertEqual(self.problems(self.make(cycles=at_root, entry_lint=at_root)), [])

    def test_paths_table_moves_the_spec_files(self):
        root = self.make(toml_text=toml().replace("functions = 'spec/FUNCTIONS.md'", "functions = 'plan/F.md'"))
        self.only(self.problems(root), "plan/F.md: missing")
        (root / "plan").mkdir()
        (root / "plan" / "F.md").write_text(table(R1, R2, r3()), encoding="utf-8")
        self.assertEqual(self.problems(root), [])

    def test_toml_reader_without_tomllib_matches_tomllib(self):
        text = toml(cycles=BAD) + "# note\n[extra]\nn = 3 # c\nq = \"a \\\"b\\\" # not a comment\"\n"
        root = self.make(toml_text=text)
        want = pv.load_toml(root / "overhaul.toml")
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            self.assertEqual(pv.load_toml(root / "overhaul.toml"), want)


@unittest.skipUnless(HAVE_PYTEST, "pytest is not installed (py -3 -m pip install pytest)")
class RunChecks(Base):
    def test_clean_plan_passes_with_run(self):
        self.assertEqual(pv.validate(self.make(), run=True), ([], 3))

    def test_nonproven_whose_acceptance_passes_fails(self):
        passing = table(R1, row("F002", deps="F001", acc=f"{CALC_T}::test_add", status="planned"))
        root = self.make(passing)
        self.assertEqual(self.problems(root), [])  # only --run looks
        self.only(self.problems(root, run=True), "F002", "planned", "already PASSES")
        for status in ("building", "review", "landing", "needs-plan"):
            with self.subTest(status=status):
                rows = table(row("F002", acc=f"{CALC_T}::test_add", status=status))
                self.only(self.problems(self.make(rows), run=True), "F002", status, "already PASSES")

    def test_every_test_of_a_nonproven_function_must_be_red(self):
        rows = table(r3(i="F009", phase="1", deps="-", acc=f"{REPORT_T}::test_report_a; {CALC_T}::test_add"))
        self.only(self.problems(self.make(rows), run=True), "F009", f"{CALC_T}::test_add", "already PASSES")

    def test_proven_whose_acceptance_fails_fails(self):
        rows = table(R1, row("F002", deps="F001", acc=f"{CALC_T}::test_sub", status="proven"))
        self.only(self.problems(self.make(rows), run=True), "F002", "proven", "FAILS")

    def test_run_skips_tests_that_do_not_exist(self):
        rows = table(R1, row("F002", deps="F001", acc=f"{CALC_T}::test_missing", status="planned"))
        self.only(self.problems(self.make(rows), run=True), "has no def test_missing")

    def test_a_hung_acceptance_test_is_killed(self):
        slow = f'{PY} -c "import time; time.sleep(60)" {{test}}'
        root = self.make(table(R1), acceptance=slow, arch=ARCH)
        t0 = time.time()
        p = self.problems(root, run=True, timeout=2)
        self.assertLess(time.time() - t0, 30)
        self.only(p, "F001", "proven", "FAILS (exit 124)", "timed out after 2s")


class Cli(Base):
    SCRIPT = str(SCRIPTS / "plan_validate.py")

    def cli(self, *args):
        r = subprocess.run([sys.executable, self.SCRIPT, *args], capture_output=True, text=True)
        return r.returncode, r.stdout.splitlines(), r.stderr

    def test_ok_exit_0(self):
        code, out, _ = self.cli("--repo", str(self.make(cycles=OK, entry_lint=OK)))
        self.assertEqual((code, out), (0, ["PLAN OK 3 functions"]))

    def test_fail_exit_1_counts_every_problem_line(self):
        code, out, _ = self.cli("--repo", str(self.make(table(R1, R2, r3(module="ghost", entry="")), cycles=BAD)))
        self.assertEqual(code, 1)
        self.assertEqual(out[-1], f"PLAN FAIL {len(out) - 1} problems")
        self.assertEqual(len(out) - 1, 3, out)

    def test_bad_usage_exit_2(self):
        self.assertEqual(self.cli()[0], 2)
        self.assertEqual(self.cli("--repo", str(Path(tempfile.gettempdir()) / "pv_no_such_dir_x"))[0], 2)

    @unittest.skipUnless(HAVE_PYTEST, "pytest is not installed")
    def test_run_flag(self):
        self.assertEqual(self.cli("--repo", str(self.make()), "--run")[:2], (0, ["PLAN OK 3 functions"]))
        passing = table(R1, row("F002", deps="F001", acc=f"{CALC_T}::test_add", status="planned"))
        code, out, _ = self.cli("--repo", str(self.make(passing)), "--run")
        self.assertEqual((code, out[-1]), (1, "PLAN FAIL 1 problems"))


if __name__ == "__main__":
    unittest.main()
