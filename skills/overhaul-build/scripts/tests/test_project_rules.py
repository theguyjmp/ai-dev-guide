"""Project rules per row (rules column, [paths] patterns/standards, brief injection, C30) and the drive side of
[commands.acceptance_by] and [commands.extra].

Run from the scripts dir: py -3 -m unittest tests.test_project_rules
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive  # noqa: E402
import plan_validate  # noqa: E402

PATTERNS = "# Patterns\n\n| id | pattern | rule |\n|---|---|---|\n| P01 | Picking an input selects instead | S20 |\n"
STANDARDS = "# Standards\n\nS18. Colours come only from tokens,\nin both themes.\n\nS20. Inputs take picks first.\n"
HEADER = "| id | phase | module | deps | entry | acceptance | status | files | needs | rules |\n" \
         "|---|---|---|---|---|---|---|---|---|---|\n"


class RuleDefsTest(unittest.TestCase):
    def test_table_rows_and_wrapped_paragraphs(self):
        defs = drive.rule_defs(PATTERNS + STANDARDS)
        self.assertEqual(defs["P01"], "P01 | Picking an input selects instead | S20")
        self.assertEqual(defs["S18"], "S18. Colours come only from tokens, in both themes.")
        self.assertEqual(defs["S20"], "S20. Inputs take picks first.")
        self.assertNotIn("id", defs)

    def test_drive_reads_the_rules_column(self):
        row = "| F1 | 1 | ui | - | ui: pick face | `tests/acceptance/a.rs::t` | planned | src/a.rs | stub | P01, S18 |"
        f = drive.parse_functions(HEADER + row)[0]
        self.assertEqual(f.rules, ["P01", "S18"])
        old = "| id | phase | module | deps | entry | acceptance | status |\n|---|---|---|---|---|---|---|\n" \
              "| F1 | 1 | ui | - | x | `tests/acceptance/a.rs::t` | planned |"
        self.assertEqual(drive.parse_functions(old)[0].rules, [])

    def test_brief_section_and_c30(self):
        files = {"spec/PATTERNS.md": PATTERNS, "spec/STANDARDS.md": STANDARDS}
        fake = types.SimpleNamespace(cfg={"paths": {"patterns": "spec/PATTERNS.md", "standards": "spec/STANDARDS.md"}},
                                     read_main=files.get)
        f = drive.Fn("F1", "1", "ui", [], "ui: pick", [], "planned", "", "", 0, rules=["P01", "S18", "S99"])
        text = drive.Driver.project_rules(fake, f)
        self.assertIn("## Project rules this row cites", text)
        self.assertIn("- P01 | Picking an input selects instead | S20", text)
        self.assertIn("- S18. Colours come only from tokens, in both themes.", text)
        self.assertIn("- S99: (not found in", text)
        f.rules = []
        self.assertEqual(drive.Driver.project_rules(fake, f), "")
        self.assertNotIn("C30", [i.id for i in drive.applicable_items("x")])
        self.assertIn("C30", [i.id for i in drive.applicable_items("x", True)])
        self.assertIn("{{project_rules}}", (drive.TEMPLATES / "build-brief.md").read_text(encoding="utf-8"))
        self.assertIn("{{project_rules}}", (drive.TEMPLATES / "review-brief.md").read_text(encoding="utf-8"))


class DriveConfigTest(unittest.TestCase):
    def test_tables_stay_out_of_commands(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "overhaul.toml").write_text(
                '[commands]\nacceptance = "cargo test --test {stem} -- --exact {test}"\nlint = "clippy"\n'
                '[commands.acceptance_by]\n"tests/acceptance/scale_*.rs" = "cargo test --release --test {stem} '
                '-- --exact {test} --test-threads=1"\n[commands.extra]\nstyle = "stylelint"\n', encoding="utf-8")
            cfg = drive.load_config(Path(d))
        self.assertTrue(all(isinstance(v, str) for v in cfg["commands"].values()))
        self.assertEqual(cfg["extra"], [("extra.style", "stylelint")])
        fake = types.SimpleNamespace(cfg=cfg)
        f = drive.Fn("F1", "1", "m", [], "x", ["tests/acceptance/scale_a.rs::big"], "planned", "", "", 0)
        text = drive.Driver.commands_text(fake, f)
        self.assertIn("cargo test --release --test scale_a -- --exact big --test-threads=1", text)
        self.assertIn("- extra.style: `stylelint`", text)
        self.assertEqual(drive.Driver.gate_list(fake, ("lint", "test")), [("lint", "clippy"), ("extra.style", "stylelint")])


class PlanRulesTest(unittest.TestCase):
    def check(self, rules, entry="ui: pick face", test_src=None, cfg=None, paths=None):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            (repo / "spec").mkdir()
            (repo / "spec/PATTERNS.md").write_text(PATTERNS, encoding="utf-8")
            (repo / "spec/STANDARDS.md").write_text(STANDARDS, encoding="utf-8")
            (repo / "tests/acceptance").mkdir(parents=True)
            (repo / "tests/acceptance/a.rs").write_text(test_src or "#[test]\nfn other() {}\n", encoding="utf-8")
            row = f"| F1 | 1 | ui | - | {entry} | `tests/acceptance/a.rs::pick` | planned | src/a.rs | stub | {rules} |\n"
            problems = []
            rows = plan_validate.parse_functions(HEADER + row, problems, "FUNCTIONS.md")
            for r in rows:
                r["tests"] = [("tests/acceptance/a.rs::pick", "pick")]
            pth = {"patterns": "spec/PATTERNS.md", "standards": "spec/STANDARDS.md"} if paths is None else paths
            plan_validate.check_project_rules(rows, cfg or {}, pth, repo, problems, "FUNCTIONS.md")
            return problems

    def test_cited_pattern_in_the_test_passes(self):
        src = "// proves P01: the pick goes to the input\n#[test]\nfn pick() {}\n#[test]\nfn other() {}\n"
        self.assertEqual(self.check("P01, S18", test_src=src), [])
        self.assertEqual(self.check("P01", test_src="#[test]\nfn pick_p01_input() {}\n".replace("pick_p01_input", "pick")
                                    .replace("fn pick", "// P01\nfn pick")), [])

    def test_unknown_id_missing_pattern_and_unproven_pattern(self):
        self.assertIn("rule P77 is not defined", " ".join(self.check("P01, P77")))
        self.assertIn("names no pattern id", " ".join(self.check("S18")))
        self.assertIn("pattern P01 does not appear", " ".join(self.check("P01", test_src="#[test]\nfn pick() {}\n"
                                                                         "// P01 belongs to the next test\n"
                                                                         "#[test]\nfn other() {}\n")))
        verb = self.check("-", entry="cli: part drag", cfg={"plan": {"interactive_verbs": ["drag"]}})
        self.assertIn("names no pattern id", " ".join(verb))

    def test_backward_compatible_without_paths_or_column(self):
        self.assertEqual(self.check("-", paths={}), [])
        self.assertIn("sets no [paths] patterns", " ".join(self.check("P01", paths={})))
        problems = []
        old = "| id | phase | module | deps | entry | acceptance | status | files | needs |\n|---|---|---|---|---|---|---|---|---|\n" \
              "| F1 | 1 | m | - | x | `tests/acceptance/a.rs::t` | planned | a.rs | stub |\n"
        self.assertEqual(len(plan_validate.parse_functions(old, problems, "F")), 1)
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
