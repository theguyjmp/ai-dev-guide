"""Pass E: polish converges. Visibility-only edits in the modules a fixer depends on, cross-module duplication routed
to the caller (owner-first when the owner has its own fix), a failed fix's verified NOs carried into the next round,
and 3 polish rounds / 2 fix retries by default."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drive  # noqa: E402
from test_drive import ARCH, CHAIN, Toy, git, put  # noqa: E402

DEP_ARCH = ARCH.replace("Doubles numbers through alpha.", "Depends on: alpha\nDoubles numbers through alpha.")
BETA_FNS = CHAIN.replace("| F2 | 1 | alpha | F1 | `toy double 4` | tests/acceptance/test_f2.py::test_double | src/alpha/f2.py",
                         "| F2 | 1 | beta | F1 | `toy double 4` | tests/acceptance/test_f2.py::test_double | src/beta/f2.py")
RS = "fn helper() -> u8 { 0 }\n"


def dup(module: str, evidence: str, file: str = "") -> dict:
    return {"id": "C05", "severity": "P1", "answer": "no", "module": module, "line": "3",
            "file": file or f"src/{module}/x.py", "evidence": evidence}


class Base(Toy):
    def setUp(self):
        repo = self.make_repo(functions=BETA_FNS)
        put(repo / "spec" / "ARCHITECTURE.md", DEP_ARCH)
        put(repo / "src" / "alpha" / "clock.rs", RS)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "deps")
        self.repo = repo
        self.drv = drive.Driver(repo, drive.load_config(repo))
        self.drv.load()

    def branch(self, name: str, text: str, own: str = "src/beta/y.py") -> str:
        git(self.repo, "checkout", "-q", "-B", name, "main")
        put(self.repo / "src" / "alpha" / "clock.rs", text)
        put(self.repo / own, "x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "edit")
        git(self.repo, "checkout", "-q", "main")
        return name


class VisibilityInDeps(Base):
    def test_module_deps_reads_the_depends_on_line(self):
        self.assertEqual(drive.module_deps(DEP_ARCH, "beta"), ["alpha"])
        self.assertEqual(drive.module_deps(DEP_ARCH, "alpha"), [])
        self.assertEqual(drive.module_deps("## core\nDepends on: -\n", "core"), [])

    def test_builder_brief_offers_visibility_edits_in_its_deps(self):
        text = self.drv.allowed(self.drv.fns["F2"])
        self.assertIn("and in the modules it depends on", text)
        self.assertIn("`src/alpha/`", text)
        self.assertNotIn("depends on", self.drv.allowed(self.drv.fns["F1"]))

    def test_builder_visibility_edit_in_a_dep_passes_and_a_body_edit_is_refused(self):
        f = self.drv.fns["F2"]
        b = self.branch("fn/F2", "pub(crate) " + RS, "src/beta/f2.py")
        changed = git(self.repo, "diff", "--name-only", f"main...{b}").split()
        self.assertEqual(self.drv.visibility_problem(f, changed, b), "")
        b = self.branch("fn/F2", RS.replace("0", "7"), "src/beta/f2.py")
        self.assertIn("src/alpha/clock.rs beyond visibility", self.drv.visibility_problem(f, changed, b))

    def test_polish_fix_may_only_make_visibility_edits_in_its_deps(self):
        self.assertIn("visibility-only edits in the modules `beta` depends on (`src/alpha/`)",
                      self.drv.polish_allowed("beta"))
        self.assertNotIn("visibility-only", self.drv.polish_allowed("alpha"))
        self.assertEqual(self.drv.polish_vis_problem("polish-r1-beta", "beta",
                                                     self.branch("fn/polish-r1-beta", "pub " + RS)), "")
        why = self.drv.polish_vis_problem("polish-r1-beta", "beta", self.branch("fn/polish-r1-beta", RS + "fn x() {}\n"))
        self.assertIn("src/alpha/clock.rs beyond visibility", why)
        # alpha's own fix edits alpha freely; beta is not its dep, so the review judges that edit
        self.assertEqual(self.drv.polish_vis_problem("polish-r1-alpha", "alpha",
                                                     self.branch("fn/polish-r1-alpha", RS + "fn x() {}\n")), "")


class DuplicationRouting(Base):
    def test_a_cross_module_dup_goes_to_the_caller_with_an_owner_note(self):
        issue = dup("alpha", "second copy of the label in src/beta/x.py:4", file="src/alpha/x.py")
        groups = self.drv.route_polish([issue, {"id": "C07", "module": "alpha", "evidence": "beta also"}])
        self.assertEqual(sorted(groups), ["alpha", "beta"])
        self.assertEqual(groups["beta"][0]["owners"], ["alpha"])
        self.assertEqual(groups["alpha"][0]["id"], "C07")  # only C03/C05 move
        self.assertEqual(self.drv.polish_after, {"alpha": [], "beta": ["alpha"]})  # alpha has its own fix: first
        self.assertIn("src/alpha/", self.drv.polish_claims("beta"))

    def test_no_dependency_between_the_modules_leaves_the_issue_home(self):
        self.drv.arch = ARCH
        groups = self.drv.route_polish([dup("alpha", "same helper as beta x.py")])
        self.assertEqual(list(groups), ["alpha"])
        self.assertNotIn("owners", groups["alpha"][0])

    def test_owner_first_then_the_caller(self):
        self.drv.route_polish([dup("beta", "copy of src/alpha/clock.rs helper"), dup("alpha", "plain dup in alpha")])
        self.drv.polish_queue = [("alpha", []), ("beta", [])]
        self.assertEqual([m for m, _ in self.drv.polish_ready()], ["alpha"])
        self.drv.polish_queue = [("beta", [])]  # alpha started (and is held): beta still overlaps by its claims
        self.drv.polish_meta["polish-r1-alpha"] = ("alpha", "")
        self.drv.polish_lands = ["polish-r1-alpha"]
        self.assertEqual(self.drv.polish_ready(), [])
        self.drv.polish_lands = []  # alpha landed (or failed): beta goes
        self.assertEqual([m for m, _ in self.drv.polish_ready()], ["beta"])

    def test_the_fix_brief_carries_the_owner_note_and_the_item_id(self):
        groups = self.drv.route_polish([dup("beta", "copy of src/alpha/clock.rs helper")])
        self.drv.polish_state()["round"] = 1
        self.drv.prepare_worktree = lambda fid: self.repo  # noqa: E731
        self.drv.start_polish_job = lambda *a, **k: None  # noqa: E731
        self.drv.jobs["polish-r1-beta"] = type("J", (), {"model": "x"})()
        self.drv.start_polish_fix("beta", groups["beta"])
        issues = self.drv.polish_meta["polish-r1-beta"][1]
        self.assertIn("[C05]", issues)
        self.assertIn("cross-module duplication: `beta` depends on `alpha`", issues)


class CarriedNos(Base):
    def test_a_failed_round_fix_carries_its_verified_nos_into_the_next_round(self):
        drv = self.drv
        drv.prepare_worktree = lambda fid: self.repo  # noqa: E731
        drv.start_polish_job = lambda fid, *a, **k: drv.jobs.__setitem__(fid, type("J", (), {"model": "x"})())
        pol = drv.polish_state()
        pol["round"] = 1
        drv.start_polish_fix("alpha", [{"file": "src/alpha/x.py", "line": 3, "id": "C18", "evidence": "dup"}])
        no = {"id": "C18", "severity": "P0", "file": "src/alpha/x.py", "line": 9, "evidence": "guard regressed"}
        drv.polish_failed("polish-r1-alpha", "review: 1 verified blocking NOs", [no])
        self.assertEqual(pol["failed_nos"]["alpha"]["round"], 1)
        pol["round"] = 2
        drv.start_polish_fix("alpha", [{"file": "src/alpha/x.py", "line": 3, "id": "C18", "evidence": "dup"}])
        issues = drv.polish_meta["polish-r2-alpha"][1]
        self.assertIn("The round 1 fix of this module failed its review", issues)
        self.assertIn("guard regressed", issues)
        self.assertNotIn("alpha", pol["failed_nos"])  # used once
        drv.polish_failed("polish-r2-alpha", "land: gate failed")  # no NOs: nothing carried
        self.assertNotIn("alpha", pol["failed_nos"])

    def test_stale_nos_from_an_older_round_are_dropped(self):
        drv = self.drv
        drv.prepare_worktree = lambda fid: self.repo  # noqa: E731
        drv.start_polish_job = lambda fid, *a, **k: drv.jobs.__setitem__(fid, type("J", (), {"model": "x"})())
        pol = drv.polish_state()
        pol.update(round=3, failed_nos={"alpha": {"round": 1, "nos": "- old NO"}})
        drv.start_polish_fix("alpha", [{"file": "src/alpha/x.py", "line": 3, "id": "C18", "evidence": "dup"}])
        self.assertNotIn("old NO", drv.polish_meta["polish-r3-alpha"][1])


class Defaults(Toy):
    def test_three_rounds_and_two_retries_by_default_both_overridable(self):
        repo = self.make_repo()
        drv = drive.Driver(repo, drive.load_config(repo))
        self.assertEqual((drv.polish_rounds, drv.fix_retries), (3, 2))
        toml = repo / "overhaul.toml"
        toml.write_text(toml.read_text(encoding="utf-8").replace(
            "[limits]\n", "[limits]\npolish_rounds = 1\nfix_retries = 0\n"), encoding="utf-8")
        drv = drive.Driver(repo, drive.load_config(repo))
        self.assertEqual((drv.polish_rounds, drv.fix_retries), (1, 0))


if __name__ == "__main__":
    unittest.main()
