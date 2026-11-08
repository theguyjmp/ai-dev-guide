"""The skills quote their checkers: the plan skill's FUNCTIONS and ARCHITECTURE format text is what plan_validate.py
and check_cycles.py --help print, and the build skill names the manager's commands."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
SKILLS = SCRIPTS.parent.parent
sys.path.insert(0, str(SCRIPTS))
import plan_validate  # noqa: E402


def stripped(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]


def contains_block(have: list[str], block: list[str]) -> bool:
    return any(have[k:k + len(block)] == block for k in range(len(have)))


class TestSkillText(unittest.TestCase):
    def test_plan_skill_architecture_format_is_the_check_cycles_help(self):
        out = subprocess.run([sys.executable, str(SCRIPTS / "check_cycles.py"), "--help"], capture_output=True,
                             text=True, encoding="utf-8", stdin=subprocess.DEVNULL).stdout.splitlines()
        first = next(n for n, ln in enumerate(out) if ln.startswith("ARCHITECTURE.md format:"))
        last = next(n for n, ln in enumerate(out) if ln.endswith("or none for none."))
        block = [ln.strip() for ln in out[first:last + 1]]
        self.assertGreaterEqual(len(block), 6)
        self.assertTrue(contains_block(stripped(SKILLS / "overhaul-plan" / "SKILL.md"), block), block)

    def test_plan_skill_functions_format_is_the_plan_validate_text(self):
        doc = [ln.strip() for ln in plan_validate.__doc__.splitlines()]
        first = next(n for n, ln in enumerate(doc) if ln.startswith("FUNCTIONS.md table:"))
        last = next(n for n, ln in enumerate(doc) if ln.startswith("ARCHITECTURE.md:"))
        block = doc[first:last]
        self.assertEqual(len(block), 5)
        self.assertTrue(contains_block(stripped(SKILLS / "overhaul-plan" / "SKILL.md"), block), block)

    def test_build_skill_names_the_manager_commands(self):
        text = (SKILLS / "overhaul-build" / "SKILL.md").read_text(encoding="utf-8")
        for needle in ("drive.py --repo R --reset <id>", "--mark-proven <id> --evidence <file>",
                       "references/proof-check.md", "as one background command", "one long wait",
                       "directly, in the foreground"):
            self.assertIn(needle, text)
        self.assertTrue((SKILLS / "overhaul-build" / "references" / "proof-check.md").is_file())


if __name__ == "__main__":
    unittest.main()
