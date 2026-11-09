"""Pass D: plan-time owner checks (X16 reuse before new) in ARCHITECTURE.md and spec/ROADMAP.md."""
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plan_validate as pv  # noqa: E402
from tests.test_plan_validate import ARCH, Base  # noqa: E402


def arch(calc="", report="", extra=""):
    """ARCH with extra lines in the calc and report sections and an extra module section."""
    a = ARCH.replace("Paths: src/tiny.py\n", "Paths: src/tiny.py\n" + calc)
    a = a.replace("Paths: src/report.py\n", "Paths: src/report.py\n" + report)
    return a.replace("## Policies", extra + "## Policies") if extra else a


def roadmap(*rows, head="| phase | behaviour | owner | why new |"):
    return "# Roadmap\n\n" + head + "\n|---|---|---|---|\n" + "\n".join(rows) + "\n"


UNDO = "Forbidden types: *Undo*, *Redo*, *CommandStack*, Instance\n"


class StemMatch(unittest.TestCase):
    def test_names_and_sentences(self):
        self.assertEqual(pv.stem_hits(["*Undo*"], "vc-doc::undo", whole=True), ["*Undo*"])
        self.assertEqual(pv.stem_hits(["*CommandStack*"], "keeps a command stack of edits"), ["*CommandStack*"])
        self.assertEqual(pv.stem_hits(["*CommandStack*"], "CommandStackView", whole=True), ["*CommandStack*"])
        self.assertEqual(pv.stem_hits(["Instance"], "part instances"), [])
        self.assertEqual(pv.stem_hits(["Instance"], "an instance of a part"), ["Instance"])
        self.assertEqual(pv.stem_hits(["*Undo*", "*"], "sketch solver"), [])


class ArchitectureOwners(Base):
    def test_plain_plan_unchanged(self):
        self.assertEqual(self.problems(self.make(arch=arch(calc=UNDO))), [])

    def test_module_name_matching_another_owners_stem_fails(self):
        extra = "## Module undo_panel\nPaths: src/undo_panel.py\nDepends on: -\nWhy new: a panel\n\n"
        p = self.problems(self.make(arch=arch(calc=UNDO, extra=extra)))
        self.only(p, "module undo_panel", "module calc's forbidden types *Undo*", "X16")

    def test_capability_matching_another_owners_stem_fails(self):
        p = self.problems(self.make(arch=arch(calc=UNDO, report="Capability: a command stack for reports\n")))
        self.only(p, "module report", "*CommandStack*")

    def test_a_stem_has_one_owner(self):
        p = self.problems(self.make(arch=arch(calc=UNDO, report="Forbidden types: *Report*, *redo*\n")))
        self.only(p, "module report", "forbidden type *redo* is also module calc's")

    def test_why_new_line_must_hold_a_reason(self):
        p = self.problems(self.make(arch=arch(report="Why new: -\n")))
        self.only(p, "module report", "Why new: <one line>")
        self.assertEqual(self.problems(self.make(arch=arch(report="Why new: no owner formats text\n"))), [])

    def test_owners_since_marks_modules_missing_at_the_ref_as_new(self):
        root = self.make()
        base = ARCH[:ARCH.index("## Module report")] + ARCH[ARCH.index("## Policies"):]
        (root / "spec/ARCHITECTURE.md").write_text(base, encoding="utf-8")
        git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
            subprocess.run(git + cmd, cwd=root, check=True, capture_output=True)
        (root / "spec/ARCHITECTURE.md").write_text(ARCH, encoding="utf-8")
        toml = (root / "overhaul.toml").read_text(encoding="utf-8") + "[plan]\nowners_since = 'HEAD'\n"
        (root / "overhaul.toml").write_text(toml, encoding="utf-8")
        self.only(self.problems(root), "module report", "new owner")
        (root / "spec/ARCHITECTURE.md").write_text(arch(report="Why new: reports format output\n"), encoding="utf-8")
        self.assertEqual(self.problems(root), [])
        (root / "overhaul.toml").write_text(toml.replace("'HEAD'", "'nosuchref'"), encoding="utf-8")
        self.assertTrue(self.has(self.problems(root), "owners_since 'nosuchref'"))

    def test_functions_row_module_must_be_an_owner(self):
        p = self.problems(self.make(arch=ARCH.replace("## Module report", "## Module reports")))
        self.assertTrue(self.has(p, "module 'report' is not in ARCHITECTURE"), p)


class Roadmap(Base):
    def with_roadmap(self, text, arch_text=None, toml_extra=""):
        root = self.make(arch=arch_text or arch(calc=UNDO))
        (root / "spec/ROADMAP.md").write_text(text, encoding="utf-8")
        if toml_extra:
            t = root / "overhaul.toml"
            t.write_text(t.read_text(encoding="utf-8") + toml_extra, encoding="utf-8")
        return self.problems(root)

    def test_existing_owner_rows_pass(self):
        self.assertEqual(self.with_roadmap(roadmap("| 3 | multiply | calc | - |", "| 4 | csv report | report | |")), [])

    def test_new_owner_needs_a_reason_on_one_row(self):
        p = self.with_roadmap(roadmap("| 3 | plot a chart | chart | - |", "| 3 | plot axes | chart | - |"))
        self.only(p, "ROADMAP.md:5 owner chart", "new owner", "why-new")
        self.assertEqual(self.with_roadmap(roadmap("| 3 | plot a chart | chart (new) | no owner draws |",
                                                   "| 3 | plot axes | chart | - |")), [])

    def test_new_owner_matching_stems_fails(self):
        p = self.with_roadmap(roadmap("| 3 | undo for reports | redo_log | own log |"))
        self.only(p, "owner redo_log", "*Redo*", "*Undo*", "extend calc")
        p = self.with_roadmap(roadmap("| 3 | keeps a command stack | history | own log |"))
        self.only(p, "owner history", "*CommandStack*")

    def test_phase_size_and_malformed_rows(self):
        rows = [f"| 5 | step {i} | calc | - |" for i in range(3)] + ["| x | step | calc | - |", "| 6 |  | | - |"]
        p = self.with_roadmap(roadmap(*rows), toml_extra="[plan]\nmax_phase_size = 2\n")
        self.assertTrue(self.has(p, "phase 5 has 3 rows > max_phase_size 2"), p)
        self.assertTrue(self.has(p, "malformed phase 'x'"), p)
        self.assertTrue(self.has(p, "empty behaviour"), p)
        self.assertTrue(self.has(p, "empty owner"), p)

    def test_no_table_and_roadmap_path(self):
        self.only(self.with_roadmap("# Roadmap\n\nlater\n"), "no table with the header | phase | behaviour")
        p = self.with_roadmap("x", toml_extra="")
        root = self.make()
        (root / "plan").mkdir()
        (root / "plan/RM.md").write_text(roadmap("| 2 | x | nobody | |"), encoding="utf-8")
        t = root / "overhaul.toml"
        t.write_text(t.read_text(encoding="utf-8").replace("[paths]\n", "[paths]\nroadmap = 'plan/RM.md'\n"),
                     encoding="utf-8")
        self.only(self.problems(root), "plan/RM.md:5 owner nobody", "new owner")
        self.assertTrue(p)


if __name__ == "__main__":
    unittest.main()
