"""Tests for check_cycles.py and entry_lint.py, each run as a subprocess on a toy repo built in a temp dir.

Run from the scripts dir:  py -3 -m unittest
"""
import json
import os
import shutil
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CYCLES = os.path.join(SCRIPTS, "check_cycles.py")
ENTRY = os.path.join(SCRIPTS, "entry_lint.py")
LINE = re.compile(r"^[\w./\-]+:\d+: \S.*$")
PRICE = "from toy.core import money\n\n\ndef price(x):\n    return money.cents(x)\n"


def arch(*mods):
    """ARCHITECTURE.md text from (name, paths, deps) tuples."""
    text = "# Architecture\n\nPublic API and notes are free text.\n"
    for name, paths, deps in mods:
        text += f"\n## Module {name}\nPaths: {paths}\nDepends on: {deps}\n"
    return text


class ToyCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name

    def write(self, rel, text=""):
        path = os.path.join(self.repo, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def pkg(self, *mods):
        """The toy package src/toy with one subpackage per module."""
        self.write("src/toy/__init__.py")
        for mod in mods:
            self.write(f"src/toy/{mod}/__init__.py")

    def run_script(self, script, *args):
        proc = subprocess.run([sys.executable, script, "--repo", self.repo, *args], capture_output=True,
                              text=True, encoding="utf-8", timeout=120)
        return proc.returncode, proc.stdout + proc.stderr

    def assert_fails(self, script, *args):
        code, out = self.run_script(script, *args)
        self.assertEqual(code, 1, out)
        lines = out.strip().splitlines()
        self.assertTrue(lines[-1].endswith("problems"), out)
        for line in lines[:-1]:
            self.assertRegex(line, LINE)
        return out

    def assert_clean(self, script, *args):
        code, out = self.run_script(script, *args)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        return out


class CyclesTest(ToyCase):
    def test_help_prints_format(self):
        out = subprocess.run([sys.executable, CYCLES, "--help"], capture_output=True, text=True).stdout
        for part in ("## Module <name>", "Paths: ", "Depends on: ", "trailing /", "glob", "longest", "none"):
            self.assertIn(part, out)

    def test_declared_cycle(self):
        self.pkg("a", "b")
        self.write("spec/ARCHITECTURE.md", arch(("a", "src/toy/a/", "b"), ("b", "src/toy/b/", "a")))
        out = self.assert_fails(CYCLES)
        self.assertRegex(out, r"(?m)^spec/ARCHITECTURE\.md:\d+: declared dependency cycle: a -> b -> a$")

    def test_undeclared_import(self):
        self.pkg("core", "api")
        self.write("src/toy/core/money.py", "def cents(x):\n    return x\n")
        self.write("src/toy/api/routes.py", PRICE)
        self.write("spec/ARCHITECTURE.md", arch(("core", "src/toy/core/", "-"), ("api", "src/toy/api/", "none")))
        out = self.assert_fails(CYCLES)
        self.assertIn("src/toy/api/routes.py:1: module api imports core (src/toy/core/money.py); "
                      "api may depend on: -", out)

    def test_real_file_cycle(self):
        self.pkg("core")
        self.write("src/toy/core/a.py", "from toy.core import b\n")
        self.write("src/toy/core/b.py", "from toy.core import a\n")
        self.write("spec/ARCHITECTURE.md", arch(("core", "src/toy/core/", "-")))
        out = self.assert_fails(CYCLES)
        self.assertIn("src/toy/core/a.py:1: import cycle: "
                      "src/toy/core/a.py -> src/toy/core/b.py -> src/toy/core/a.py", out)

    def test_real_module_cycle(self):
        self.pkg("core", "api")
        self.write("src/toy/api/routes.py", "from toy.core import money\n")
        self.write("src/toy/core/money.py", "from toy.api import routes\n")
        self.write("spec/ARCHITECTURE.md", arch(("core", "src/toy/core/", "-"), ("api", "src/toy/api/", "core")))
        out = self.assert_fails(CYCLES)
        self.assertIn("src/toy/core/money.py:1: module core imports api", out)
        self.assertIn("src/toy/api/routes.py:1: import cycle between modules: api -> core -> api", out)

    def test_clean_pass(self):
        self.pkg("core", "api")
        self.write("src/toy/core/money.py", "import json\n\n\ndef cents(x):\n    return json.dumps(x)\n")
        self.write("src/toy/api/routes.py", PRICE)
        self.write("spec/ARCHITECTURE.md", arch(("core", "src/toy/core/", "-\nPublic: toy.core.money"),
                                                ("api", "src/toy/api/", "core")))
        self.assertIn("CYCLES OK 2 modules", self.assert_clean(CYCLES))

    def public_toy(self, core_public=""):
        self.pkg("core", "api")
        self.write("src/toy/core/__init__.py", "from toy.core.money import cents\n\n__all__ = [\"cents\"]\n")
        self.write("src/toy/core/money.py", "CURRENCY = 1\n\n\ndef cents(x):\n    return x\n")
        deps = "-" + (f"\nPublic: {core_public}" if core_public else "")
        self.write("spec/ARCHITECTURE.md", arch(("core", "src/toy/core/", deps), ("api", "src/toy/api/", "core")))

    def test_import_through_face_passes(self):
        self.public_toy()
        self.write("src/toy/api/routes.py", "from toy.core import cents\n")
        self.assert_clean(CYCLES)

    def test_import_past_face_fails(self):
        self.public_toy()
        self.write("src/toy/api/routes.py", "from toy.core.money import CURRENCY\n")
        out = self.assert_fails(CYCLES)
        self.assertIn("src/toy/api/routes.py:1: imports CURRENCY from src/toy/core/money.py, past the public API "
                      "of module core", out)

    def test_name_missing_from_dunder_all_fails(self):
        self.public_toy()
        self.write("src/toy/api/routes.py", "from toy.core import money\n")
        out = self.assert_fails(CYCLES)
        self.assertIn("imports src/toy/core/money.py, past the public API of module core", out)

    def test_public_line_allows_listed_names_only(self):
        self.public_toy("toy.core.money.cents")
        self.write("src/toy/api/routes.py", "from toy.core.money import cents\n")
        self.assert_clean(CYCLES)
        self.write("src/toy/api/routes.py", "from toy.core.money import CURRENCY\n")
        self.assertIn("imports CURRENCY from src/toy/core/money.py", self.assert_fails(CYCLES))

    def test_public_entry_past_dunder_all_fails(self):
        # trial 3: `Public: toy.core.money.CURRENCY` let api import a name the core face does not export
        self.public_toy("toy.core.cents, toy.core.money.CURRENCY")
        self.write("src/toy/api/routes.py", "from toy.core.money import CURRENCY\n")
        out = self.assert_fails(CYCLES)
        self.assertIn("Public entry toy.core.money.CURRENCY of module core reaches past its face "
                      "src/toy/core/__init__.py", out)
        self.assertIn("src/toy/api/routes.py:1: imports CURRENCY from src/toy/core/money.py", out)

    def test_private_public_entry_fails(self):
        self.public_toy("toy.core.money._hidden")
        self.write("src/toy/core/money.py", "def _hidden():\n    return 1\n")
        self.write("src/toy/api/routes.py", "")
        self.assertIn("Public entry toy.core.money._hidden of module core is private", self.assert_fails(CYCLES))

    def test_unknown_public_entry_fails(self):
        self.public_toy("toy.core.nothing_here.x")
        self.write("src/toy/api/routes.py", "")
        out = self.assert_fails(CYCLES)
        self.assertIn("Public entry toy.core.nothing_here.x of module core is no module or name it owns", out)

    def test_longest_path_wins(self):
        self.pkg("core")
        self.write("src/toy/main.py", "from toy.core import money\n")
        self.write("src/toy/core/money.py", "from toy import main\n")
        self.write("spec/ARCHITECTURE.md", arch(("app", "src/toy/", "core"), ("core", "src/toy/core/", "-")))
        out = self.assert_fails(CYCLES)
        self.assertIn("src/toy/core/money.py:1: module core imports app", out)
        self.assertNotIn("src/toy/main.py:1: module", out)

    def test_type_checking_import_is_no_file_cycle(self):
        self.pkg("core")
        self.write("src/toy/core/a.py",
                   "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from toy.core import b\n")
        self.write("src/toy/core/b.py", "from toy.core import a\n")
        self.write("spec/ARCHITECTURE.md", arch(("core", "src/toy/core/", "-")))
        self.assert_clean(CYCLES)

    def test_relative_import_counts(self):
        self.pkg("core", "api")
        self.write("src/toy/core/money.py", "")
        self.write("src/toy/api/routes.py", "from ..core import money\n")
        self.write("spec/ARCHITECTURE.md", arch(("core", "src/toy/core/", "-"), ("api", "src/toy/api/", "-")))
        out = self.assert_fails(CYCLES)
        self.assertIn("src/toy/api/routes.py:1: module api imports core", out)

    def test_missing_architecture(self):
        self.pkg("core")
        out = self.assert_fails(CYCLES)
        self.assertIn("spec/ARCHITECTURE.md:1: missing", out)


class EntryLintTest(ToyCase):
    def test_help_prints_rules(self):
        out = subprocess.run([sys.executable, ENTRY, "--help"], capture_output=True, text=True).stdout
        for part in ("tests/acceptance", "subprocess", "http.client", "_entry", "monkeypatch", "MagicMock"):
            self.assertIn(part, out)

    def test_mock_banned(self):
        self.write("tests/acceptance/test_mock.py",
                   "from unittest.mock import MagicMock, patch\nimport pytest\n\n\n"
                   "@patch('toy.core.money.cents')\n"
                   "def test_cents(fake, monkeypatch):\n"
                   "    monkeypatch.setenv('A', '1')\n"
                   "    fake.return_value = MagicMock()\n")
        out = self.assert_fails(ENTRY)
        self.assertRegex(out, r"(?m)^tests/acceptance/test_mock\.py:1: imports `unittest\.mock`: mocks")
        self.assertIn("tests/acceptance/test_mock.py:1: `MagicMock` is banned", out)
        self.assertIn("tests/acceptance/test_mock.py:5: `patch` is banned", out)
        self.assertIn("tests/acceptance/test_mock.py:6: `monkeypatch` is banned", out)
        self.assertIn("tests/acceptance/test_mock.py:8: `MagicMock` is banned", out)

    def test_product_import_banned(self):
        self.pkg("core")
        self.write("src/toy/core/money.py", "def cents(x):\n    return x\n")
        self.write("tests/acceptance/test_prod.py",
                   "import toy\nfrom toy.core import money\n\n\ndef test_cents():\n    assert money.cents(1) == 1\n")
        out = self.assert_fails(ENTRY)
        self.assertRegex(out, r"(?m)^tests/acceptance/test_prod\.py:1: imports product package `toy`")
        self.assertRegex(out, r"(?m)^tests/acceptance/test_prod\.py:2: imports product package `toy\.core`")

    def test_clean_acceptance(self):
        self.pkg("core")
        self.write("tests/acceptance/_entry.py",
                   "import subprocess\nimport sys\n\n\ndef run_cli(*args):\n"
                   "    return subprocess.run([sys.executable, '-m', 'toy', *args], capture_output=True, text=True)\n")
        self.write("tests/acceptance/test_total.py",
                   "import http.client\nimport json\nimport os\nimport socket\nimport urllib.request\n"
                   "from http import client\nfrom pathlib import Path\n\nimport pytest\n\n"
                   "from _entry import run_cli\n\n\n"
                   "def test_total(tmp_path):\n"
                   "    out = run_cli('total')\n"
                   "    assert json.loads(out.stdout)['total'] == 3\n"
                   "    assert Path(tmp_path).exists() and os.sep and socket and client and http.client\n"
                   "    assert urllib.request and 'PATCH' != 'patch'\n"
                   "    with pytest.raises(ValueError):\n"
                   "        raise ValueError\n")
        self.assertIn("ENTRY LINT OK 2 files", self.assert_clean(ENTRY))

    def test_not_on_the_list_is_banned(self):
        self.write("tests/acceptance/test_x.py",
                   "import collections\nimport http.server\nimport unittest\nfrom http import client, server\n"
                   "from . import helpers\nfrom .. import toy\nfrom ._entry import run_cli\nimport importlib\n"
                   "x = __import__('toy')\n")
        out = self.assert_fails(ENTRY)
        for line, part in ((1, "`collections`"), (2, "`http.server`"), (3, "`unittest`"), (4, "`http.server`"),
                           (5, "relative import `.helpers`"), (6, "reaches out"), (8, "`importlib`"),
                           (9, "`__import__`")):
            self.assertRegex(out, rf"(?m)^tests/acceptance/test_x\.py:{line}: .*{re.escape(part)}")
        self.assertNotIn("test_x.py:7:", out)

    def test_explicit_path_and_missing(self):
        self.write("tests/acceptance/test_ok.py", "import json\n")
        self.write("tests/unit/test_u.py", "import collections\n")
        self.assertIn("ENTRY LINT OK 1 files", self.assert_clean(ENTRY, "tests/acceptance"))
        self.assertIn("tests/unit/test_u.py:1: imports `collections`", self.assert_fails(ENTRY, "tests/unit"))
        self.assertIn("tests/nope:1: not found", self.assert_fails(ENTRY, "tests/nope"))

    def test_concurrent_test_may_use_threads(self):
        self.write("tests/acceptance/test_burst.py",
                   "import threading\nfrom concurrent.futures import ThreadPoolExecutor\n\n\n"
                   "def test_burst():\n    assert threading and ThreadPoolExecutor\n")
        self.assert_clean(ENTRY)


LITERALS = os.path.join(SCRIPTS, "dup_literals.py")
MSG = "qty must be a positive whole number"


class DupLiteralsTest(ToyCase):
    def test_repeat_across_files_fails(self):
        self.write("src/toy/a.py", f'def a():\n    raise ValueError("{MSG}")\n')
        self.write("src/toy/b.py", f'\n\ndef b(q):\n    return f"{MSG}: {{q}}"\n')
        out = self.assert_fails(LITERALS)
        self.assertIn("src/toy/a.py:2: literal", out)
        self.assertIn("is repeated at src/toy/b.py:4; define it once and import it", out)

    def test_repeat_in_one_file_fails(self):
        self.write("src/toy/a.py", f'X = "{MSG}"\nY = "{MSG}"\n')
        self.assertIn("src/toy/a.py:1: literal", self.assert_fails(LITERALS))

    def test_docstrings_short_literals_and_tests_pass(self):
        doc = f'"""{MSG}."""\n'
        self.write("src/toy/a.py", doc + f'\n\ndef a():\n    {doc}    return "short text"\n')
        self.write("src/toy/b.py", doc + 'NAME = "short text"\n')
        self.write("src/toy/tests/test_a.py", f'M = "{MSG}"\n')
        self.write("tests/unit/test_b.py", f'M = "{MSG}"\n')
        self.write("src/toy/c.py", f'M = "{MSG}"\n')
        self.assertIn("LITERALS OK 3 files", self.assert_clean(LITERALS))

    def test_js_and_min_flag(self):
        self.write("src/web/a.ts", "export const a = 'no such item in stock';\n")
        self.write("src/web/b.ts", 'throw new Error("no such item in stock");\n')
        self.assert_fails(LITERALS)
        self.assert_clean(LITERALS, "--min", "40")


SHAPE = os.path.join(SCRIPTS, "shape_check.py")
FIXTURE = "import pytest\n\n\n@pytest.fixture\ndef data(tmp_path):\n    return tmp_path / \"data.json\"\n"


class ShapeCheckTest(ToyCase):
    def test_long_function_fails(self):
        body = "".join(f"    x{i} = {i}\n" for i in range(30))
        self.write("src/toy/a.py", f"def long():\n{body}    return 0\n")
        self.assertIn("src/toy/a.py:1: function long is 32 lines, over 30", self.assert_fails(SHAPE))
        self.assert_clean(SHAPE, "--max-lines", "40")

    def test_constant_after_def_fails_unless_it_uses_a_def(self):
        self.write("src/toy/a.py", "TOP = 1\n\n\ndef f():\n    return 1\n\n\nTABLE = {\"f\": f}\nLATE = 2\n")
        out = self.assert_fails(SHAPE)
        self.assertIn("src/toy/a.py:9: constant LATE is defined after the first def or class", out)
        self.assertNotIn("TABLE", out)
        self.assertNotIn("TOP", out)

    def test_copied_fixture_fails(self):
        self.write("tests/unit/test_a.py", FIXTURE)
        self.write("tests/unit/test_b.py", FIXTURE)
        out = self.assert_fails(SHAPE)
        self.assertIn("tests/unit/test_a.py:5: fixture data is copied at tests/unit/test_b.py:5; define it once in "
                      "a shared conftest.py", out)

    def test_shared_conftest_and_long_tests_pass(self):
        self.write("tests/conftest.py", FIXTURE)
        body = "".join(f"    assert {i} == {i}\n" for i in range(40))
        self.write("tests/unit/test_a.py", f"def test_many(data):\n{body}")
        self.write("src/toy/a.py", "LIMIT = 3\n\n\ndef f():\n    return LIMIT\n")
        self.assertIn("SHAPE OK 3 files", self.assert_clean(SHAPE))


if __name__ == "__main__":
    unittest.main()


class TestProbe(unittest.TestCase):
    """v3 probe: mutate.py kills one mutant and reports one survivor; plan_validate requires property tests."""

    def test_mutate_reports_one_survivor_and_one_killed(self):
        import mutate
        root = Path(tempfile.mkdtemp(prefix="mut_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "src" / "calc.py").write_text("def neg(x):\n    return -x if x > 0 else x\n", encoding="utf-8")
        (root / "tests" / "test_calc.py").write_text(
            "import sys, pathlib, unittest\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'src'))\n"
            "import calc\n\n\nclass T(unittest.TestCase):\n    def test_neg(self):\n"
            "        self.assertEqual(calc.neg(1), -1)\n", encoding="utf-8")
        res = mutate.mutate(root, ["src"], ["tests"], '"{python}" -m unittest discover -s {tests}')
        self.assertEqual((res["mutants"], res["killed"], res["survived"]), (4, 3, 1), res)
        self.assertEqual(res["survivors"][0]["op"], "boundary")

    def test_plan_validate_requires_property_tests(self):
        from tests.test_plan_validate import Base
        case = Base()
        case.addCleanup = self.addCleanup
        root = case.make(props=False)
        p = case.problems(root)
        self.assertTrue(any("no property tests" in x for x in p), p)
        self.assertFalse(any("no property tests" in x for x in case.problems(case.make())))

    def test_land_rejects_a_modified_planned_test_but_allows_a_new_one(self):
        import land
        root = Path(tempfile.mkdtemp(prefix="anc_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)

        def g(*a):
            return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=True).stdout

        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@e.x")
        g("config", "user.name", "t")
        for rel in ("spec/FUNCTIONS.md", "tests/property/test_f1.py", "tests/unit/a/test_a.py"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("x = 1\n", encoding="utf-8")
        g("add", "-A")
        g("commit", "-qm", "plan")
        base = g("rev-parse", "HEAD").strip()
        g("checkout", "-qb", "fn/ok")
        (root / "tests/property/test_extra.py").write_text("y = 2\n", encoding="utf-8")
        g("add", "-A")
        g("commit", "-qm", "add a test")
        land.check_anchors(str(root), base, g("rev-parse", "HEAD").strip(), "fn/ok", "spec/FUNCTIONS.md")
        g("checkout", "-qb", "fn/bad", base)
        (root / "tests/property/test_f1.py").write_text("x = 2\n", encoding="utf-8")
        (root / "tests/unit/a/test_a.py").unlink()
        g("add", "-A")
        g("commit", "-qm", "weaken")
        with self.assertRaises(land.LandFail) as cm:
            land.check_anchors(str(root), base, g("rev-parse", "HEAD").strip(), "fn/bad", "spec/FUNCTIONS.md")
        self.assertIn("tests/property/test_f1.py", str(cm.exception))
        self.assertIn("tests/unit/a/test_a.py", str(cm.exception))

    def test_lint_hook_returns_lint_failures_with_exit_2(self):
        root = Path(tempfile.mkdtemp(prefix="lh_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "lint.py").write_text("import sys\nsys.exit(1 if 'bad' in open(sys.argv[1]).read() else 0)\n")
        lint = f'"{Path(sys.executable).as_posix()}" lint.py .'
        (root / "overhaul.toml").write_text(f"[commands]\nlint = '{lint}'\n", encoding="utf-8")
        hook = str(Path(__file__).resolve().parents[1] / "lint_hook.py")
        for text, code in (("good = 1\n", 0), ("bad = 1\n", 2)):
            (root / "m.py").write_text(text, encoding="utf-8")
            event = json.dumps({"tool_name": "Edit", "cwd": str(root), "tool_input": {"file_path": str(root / "m.py")}})
            p = subprocess.run([sys.executable, hook], input=event, capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, code, p.stderr)
