"""supervise.py: the one launcher. Fake drive.py (scripted exit codes), fake claude (plan-fix agents, integrator,
proof-check) and fake plan_validate; a toy plan whose two needs-plan rows would both split into the next free id."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import memcap  # noqa: E402
import plan_validate  # noqa: E402
import supervise  # noqa: E402

FUNCTIONS = """# Functions

| id | phase | module | deps | entry | acceptance | status |
|----|-------|--------|------|-------|------------|--------|
| S01 | 1 | alpha | - | `toy a` | tests/acceptance/test_a.py::test_a | planned |
| S02 | 1 | beta | - | `toy b` | tests/acceptance/test_b.py::test_b | planned |
| S03 | 1 | gamma | - | `toy c` | tests/acceptance/test_c.py::test_c | proven |
"""

COMMON = r'''
import json, os, re, subprocess, sys, time
from pathlib import Path
ROOT = Path(sys.argv[1])
SC = json.loads((ROOT / "scenario.json").read_text(encoding="utf-8"))
def note(kind, **kw):
    with open(ROOT / "calls.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, **kw}) + "\n")
def rows(text):
    return [l for l in text.splitlines() if re.match(r"\| [A-Z]\d+ \|", l)]
def rid(line):
    return line.split("|")[1].strip()
def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout
'''

FAKE_DRIVE = COMMON + r'''
args = sys.argv[2:]
repo = Path(args[args.index("--repo") + 1])
if "--reset" in args:
    note("reset", id=args[args.index("--reset") + 1])
    sys.exit(0)
n = sum(1 for l in (ROOT / "calls.jsonl").read_text().splitlines() if '"drive"' in l) if (ROOT / "calls.jsonl").exists() else 0
code = SC["exits"][min(n, len(SC["exits"]) - 1)]
note("drive", code=code)
table = rows((repo / "spec" / "FUNCTIONS.md").read_text(encoding="utf-8"))
fns = {}
for line in table:
    st = line.split("|")[7].strip()
    if st == "deferred":
        continue
    status = "needs-plan" if code == 3 and rid(line) in SC.get("needs", []) else (
        "planned" if rid(line) in SC.get("unproven", []) else "proven")
    fns[rid(line)] = {"status": status, "resets": SC.get("resets", {}).get(rid(line), 0), "last_reason": "fake"}
(repo / ".overhaul").mkdir(exist_ok=True)
(repo / ".overhaul" / "state.json").write_text(json.dumps({"functions": fns}))
print("drive: error: fake" if code not in (0, 3) else f"DRIVE EXIT {code}")
sys.exit(code)
'''

FAKE_VALIDATE = COMMON + r'''
args = sys.argv[2:]
repo = Path(args[args.index("--repo") + 1])
note("validate", repo=repo.as_posix(), run="--run" in args)
ids = [rid(l) for l in rows((repo / "spec" / "FUNCTIONS.md").read_text(encoding="utf-8"))]
dup = sorted({i for i in ids if ids.count(i) > 1})
if "--run" in args and SC.get("flaky"):
    print("spec/FUNCTIONS.md:7 S03: flaky under load: its acceptance tests/acceptance/test_c.py::test_c failed in the batch and passed alone")
print(f"PLAN FAIL {len(dup)} problems" if dup else f"PLAN OK {len(ids)} functions")
sys.exit(1 if dup else 0)
'''

FAKE_CLAUDE = COMMON + r'''
prompt = sys.argv[-1]
start = time.time()
env = os.environ.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS")
model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else ""
path = prompt.split("Read and do ", 1)[1].split(" for repo ")[0].rstrip(".")
if path.endswith("proof-check.md"):
    repo = Path(prompt.split(" for repo ", 1)[1].split(",")[0].rstrip("."))
    (repo / ".overhaul" / "proof-check.txt").write_text("\n".join(SC.get("proof", ["PROOF real 3/3", "S03 REAL a.rs:1 ok"])))
    note("claude", role="proof", env=env, model=model, prompt=prompt)
    sys.exit(0)
brief = Path(path).read_text(encoding="utf-8")
field = lambda name: re.search(r"- " + re.escape(name) + r"[^`]*`([^`]+)`", brief).group(1)
wt, report = Path(field("Worktree")), Path(field("Report"))
fn = wt / "spec" / "FUNCTIONS.md"
if brief.startswith("# Plan fix "):
    fid = brief.split()[3]
    kind = SC.get("kinds", {}).get(fid, "split")
    new = re.search(r"New ids reserved for you: (.*)", brief).group(1).split(", ")
    time.sleep(1.0)
    if kind == "split":
        text = fn.read_text(encoding="utf-8")
        old = next(l for l in rows(text) if rid(l) == fid)
        text = text.replace(old, old.rsplit("|", 2)[0] + "| deferred |")
        text += f"| {new[0]} | 1 | alpha | - | `toy n` | tests/acceptance/test_a.py::test_a | planned |\n"
        fn.write_text(text, encoding="utf-8")
    elif kind == "acceptance":
        (wt / "tests" / "acceptance").mkdir(parents=True, exist_ok=True)
        (wt / "tests" / "acceptance" / f"{fid}.txt").write_text("fixed")
    if kind != "none":
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", f"plan: {fid} {kind}: fake")
        report.write_text(f"PLAN-FIX {fid} {kind} PLAN OK\n")
    note("claude", role="fix", id=fid, env=env, model=model, start=start, end=time.time(), brief=brief)
else:
    branches = re.findall(r"- `(plan/fix-[^`]+)`", brief)
    base = fn.read_text(encoding="utf-8")
    table = rows(base)
    merged = {rid(l): l for l in table}
    for b in branches:
        for l in rows(git(wt, "show", f"{b}:spec/FUNCTIONS.md")):
            if l not in table:
                merged[rid(l)] = l
        for f in git(wt, "diff", "--name-only", f"main..{b}").split():
            if f != "spec/FUNCTIONS.md":
                git(wt, "checkout", b, "--", f)
    head = base.split(table[0])[0]
    fn.write_text(head + "\n".join(merged.values()) + "\n", encoding="utf-8")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "plan: round merge: fake")
    report.write_text("PLAN-MERGE round 1 PLAN OK merged " + " ".join(branches) + "\n")
    note("claude", role="merge", env=env, model=model, branches=branches)
'''


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class Toy(unittest.TestCase):
    def make(self, **scenario) -> Path:
        self.root = Path(tempfile.mkdtemp(prefix="supervise-test-")).resolve()
        self.addCleanup(self.cleanup)
        repo = self.root / "repo"
        (repo / "spec").mkdir(parents=True)
        (repo / "spec" / "FUNCTIONS.md").write_text(FUNCTIONS, encoding="utf-8", newline="\n")
        (repo / ".gitignore").write_text(".overhaul/\n")
        (repo / "overhaul.toml").write_text("[limits]\nproc_mem_gb = 0\n\n[harness]\nplan_fix_parallel = 4\n")
        for name, body in (("fake_drive.py", FAKE_DRIVE), ("fake_validate.py", FAKE_VALIDATE),
                           ("fake_claude.py", FAKE_CLAUDE)):
            (self.root / name).write_text(body, encoding="utf-8")
        (self.root / "scenario.json").write_text(json.dumps({"exits": [0], **scenario}))
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "m0: configure")
        self.repo = repo
        return repo

    def cleanup(self) -> None:
        subprocess.run(["git", "worktree", "prune"], cwd=self.root / "repo", capture_output=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def sup(self, max_rounds: int = 12) -> supervise.Supervisor:
        py = sys.executable
        return supervise.Supervisor(self.repo, max_rounds,
                                    drive_cmd=[py, str(self.root / "fake_drive.py"), str(self.root)],
                                    validate_cmd=[py, str(self.root / "fake_validate.py"), str(self.root)],
                                    claude_cmd=[py, str(self.root / "fake_claude.py"), str(self.root)])

    def calls(self, kind: str) -> list[dict]:
        path = self.root / "calls.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [c for c in map(json.loads, lines) if c["kind"] == kind]

    def log(self) -> str:
        return (self.repo / ".overhaul" / "supervise.log").read_text(encoding="utf-8")

    def main_rows(self) -> dict[str, str]:
        text = git(self.repo, "show", "main:spec/FUNCTIONS.md")
        return {ln.split("|")[1].strip(): ln.split("|")[7].strip() for ln in text.splitlines()
                if ln.startswith("| S")}


class Loop(Toy):
    def test_exit0_runs_the_proof_check_and_stops(self):
        self.make(exits=[0])
        self.assertEqual(self.sup().supervise(), 0)
        proof = self.calls("claude")
        self.assertEqual([c["role"] for c in proof], ["proof"])
        self.assertEqual(proof[0]["env"], "0")
        self.assertEqual(proof[0]["model"], "opus")
        self.assertIn("proof: PROOF real 3/3", self.log())
        self.assertIn("done: every function proven and REAL", self.log())

    def test_other_exit_runs_once_more_then_stops_with_its_line(self):
        self.make(exits=[7, 7])
        self.assertEqual(self.sup().supervise(), 1)
        self.assertEqual(len(self.calls("drive")), 2)
        self.assertEqual(self.calls("claude"), [])
        self.assertIn("stop: error: drive exit 7 twice: drive: error: fake", self.log().splitlines()[-1])

    def test_other_exit_then_zero_finishes(self):
        self.make(exits=[7, 0])
        self.assertEqual(self.sup().supervise(), 0)
        self.assertEqual(len(self.calls("drive")), 2)

    def test_no_proof_check_while_functions_remain(self):
        self.make(exits=[0], unproven=["S02"])
        self.assertEqual(self.sup().supervise(), 1)
        self.assertEqual(self.calls("claude"), [])
        self.assertIn("no proof-check while functions remain", self.log())

    def test_proof_not_real_stops_with_4(self):
        self.make(exits=[0], proof=["PROOF real 2/3 partial 1", "S01 REAL a:1 ok", "S02 PARTIAL b:2 import"])
        self.assertEqual(self.sup().supervise(), 4)
        self.assertIn("not REAL: S02 PARTIAL", self.log())

    def test_max_rounds_stops_with_3(self):
        self.make(exits=[3], needs=["S01"])
        self.assertEqual(self.sup(max_rounds=0).supervise(), 3)
        self.assertEqual(self.calls("claude"), [])
        self.assertIn("needs-plan: S01", self.log())


class Exit3(Toy):
    def test_two_needs_plan_rows_fix_in_parallel_and_never_collide_on_ids(self):
        self.make(exits=[3, 0], needs=["S01", "S02"], flaky=True)
        self.assertEqual(self.sup().supervise(), 0)
        fixes = [c for c in self.calls("claude") if c["role"] == "fix"]
        self.assertEqual(sorted(c["id"] for c in fixes), ["S01", "S02"])
        a, b = fixes
        self.assertLess(max(a["start"], b["start"]), min(a["end"], b["end"]), "the plan fixes ran one after another")
        self.assertTrue(all(c["env"] == "0" and c["model"] == "opus" for c in fixes))
        self.assertIn("New ids reserved for you: S04, S05, S06, S07, S08", next(c for c in fixes if c["id"] == "S01")["brief"])
        rows = self.main_rows()
        self.assertEqual((rows["S01"], rows["S02"]), ("deferred", "deferred"))
        self.assertEqual((rows["S04"], rows["S09"]), ("planned", "planned"))  # S02's split took its own range
        merge = [c for c in self.calls("claude") if c["role"] == "merge"]
        self.assertEqual(merge[0]["branches"], ["plan/fix-S01", "plan/fix-S02"])
        self.assertEqual(merge[0]["model"], "sonnet")
        runs = self.calls("validate")
        self.assertTrue(runs[0]["repo"].endswith("_merge") and not runs[0]["run"])
        self.assertEqual((Path(runs[1]["repo"]), runs[1]["run"]), (self.repo, True))
        self.assertEqual(self.calls("reset"), [])  # both rows were replaced, none resets
        self.assertIn("flaky under load: 1", self.log())
        self.assertIn("new rows: S04 S09", self.log())
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", "main").strip(), "plan: round merge: fake")
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)

    def test_a_fixed_row_that_remains_is_reset_without_an_integrator(self):
        self.make(exits=[3, 0], needs=["S01"], kinds={"S01": "acceptance"})
        self.assertEqual(self.sup().supervise(), 0)
        self.assertEqual([c["id"] for c in self.calls("reset")], ["S01"])
        self.assertEqual([c["role"] for c in self.calls("claude")], ["fix", "proof"])
        self.assertIn("reset S01", self.log())

    def test_after_two_plan_fixes_the_row_is_replaced_never_reset(self):
        self.make(exits=[3, 0], needs=["S01"], resets={"S01": 2}, kinds={"S01": "acceptance"})
        self.assertEqual(self.sup().supervise(), 1)
        brief = self.calls("claude")[0]["brief"]
        self.assertIn("never reset again: replace it", brief)
        self.assertEqual(self.calls("reset"), [])
        self.assertIn("third reset is refused", self.log())

    def test_after_two_plan_fixes_a_split_goes_on(self):
        self.make(exits=[3, 0], needs=["S01"], resets={"S01": 2})
        self.assertEqual(self.sup().supervise(), 0)
        self.assertEqual(self.calls("reset"), [])
        self.assertIn("S01: replaced", self.log())

    def test_no_successful_fix_stops(self):
        self.make(exits=[3, 0], needs=["S01"], kinds={"S01": "none"})
        self.assertEqual(self.sup().supervise(), 1)
        self.assertIn("no plan fix succeeded (S01)", self.log())
        self.assertEqual(len(self.calls("drive")), 1)


class Reserve(unittest.TestCase):
    def test_ranges_follow_the_highest_id_and_skip_taken_ones(self):
        got = supervise.reserve(["S01", "S07", "S09", "X99"], ["S01", "S07"], per=3)
        self.assertEqual(got, {"S01": ["S10", "S11", "S12"], "S07": ["S13", "S14", "S15"]})

    def test_width_and_prefix_kept(self):
        self.assertEqual(supervise.reserve(["F001", "F002"], ["F002"], per=2), {"F002": ["F003", "F004"]})


class Executables(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows resolution")
    def test_bash_is_git_bash_never_wsl(self):
        bash = memcap.which("bash")
        if bash is None:
            self.skipTest("Git for Windows not installed")
        low = bash.replace("\\", "/").lower()
        self.assertTrue(low.endswith("/bin/bash.exe"))
        self.assertNotIn("system32", low)
        self.assertNotIn("windowsapps", low)

    @unittest.skipUnless(os.name == "nt", "Windows resolution")
    def test_claude_resolves_to_the_npm_shim(self):
        d = Path(tempfile.mkdtemp(prefix="supervise-path-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "claude").write_text("#!/bin/sh\n")  # npm's extensionless sh shim, which CreateProcess cannot run
        (d / "claude.cmd").write_text("@echo off\r\necho shim %*\r\n")
        with mock.patch.dict(os.environ, {"PATH": str(d)}):
            self.assertEqual(Path(memcap.which("claude")), d / "claude.cmd")
            self.assertEqual(Path(memcap.resolve(["claude", "-p"])[0]), d / "claude.cmd")

    def test_missing_program_is_an_error_line(self):
        with self.assertRaises(memcap.NotFound):
            memcap.resolve(["no-such-program-xyz"])
        out = subprocess.run([sys.executable, memcap.__file__, "0", "--", "no-such-program-xyz"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 127)
        self.assertIn("memcap: error: cannot find 'no-such-program-xyz'", out.stdout)

    def test_supervisor_without_claude_stops_with_a_line(self):
        t = Toy()
        t.make()
        try:
            sup = supervise.Supervisor(t.repo)
            with mock.patch.object(memcap, "which", return_value=None):
                with self.assertRaises(supervise.Stop) as e:
                    sup.claude()
            self.assertIn("cannot find 'claude'", str(e.exception))
        finally:
            t.doCleanups()


class FlakyUnderLoad(unittest.TestCase):
    def test_a_proven_failure_reruns_once_alone(self):
        res = {"t.py::a": (1, "boom", False), "t.py::b": (1, "boom", False), "t.py::c": (0, "", False)}
        ran = []

        def runner(cmd):
            ran.append(cmd)
            return (0, "", "") if cmd.endswith(" a") else (1, "still", "")

        flaky = plan_validate.settle_flaky("pytest -k {test}", ["t.py::a", "t.py::b", "t.py::c"], res, runner)
        self.assertEqual(flaky, ["t.py::a"])
        self.assertEqual(ran, ["pytest -k a", "pytest -k b"])  # c passed: never rerun
        self.assertEqual(res["t.py::a"][0], 0)
        self.assertEqual(res["t.py::b"][:2], (1, "still"))




class PlanMergeStaticGates(unittest.TestCase):
    """A plan branch that would turn main red on dup or literals never lands (pilot2026-11-09 23:08)."""

    def gates(self, dup_exit: int) -> list[str]:
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            py = sys.executable.replace("\\", "/")
            sup = mock.Mock(cfg={"commands": {"dup": f'"{py}" -c "import sys; print(\'a.rs:1 repeats b.rs:2\'); '
                                                      f'sys.exit({dup_exit})"', "literals": ""}},
                            log=logs.append)
            supervise.Supervisor.static_gates(sup, "plan/fixes-rL1", work, work, 1)
        return logs

    def test_a_dup_failure_stops_the_round_before_main_moves(self):
        with self.assertRaises(supervise.Stop) as e:
            self.gates(1)
        self.assertIn("fails dup", str(e.exception))
        self.assertIn("a.rs:1 repeats b.rs:2", str(e.exception))

    def test_a_clean_branch_passes_and_an_empty_command_is_skipped(self):
        self.assertEqual(self.gates(0), ["dup plan/fixes-rL1: exit 0"])


class FreeBranch(unittest.TestCase):
    """A killed round's worktree holding plan/fix-<id> is removed before the next `worktree add -B`."""

    def test_a_stale_worktree_holding_the_branch_is_removed(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "r"
            repo.mkdir()

            def git(*args, cwd=None, check=True):
                p = subprocess.run(["git", *args], cwd=cwd or repo, capture_output=True, text=True)
                if check and p.returncode:
                    raise AssertionError(p.stderr)
                return p.stdout
            git("init", "-q", "-b", "main")
            git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "0")
            git("worktree", "add", "-q", "-b", "plan/fix-X", str(Path(d) / "old"), "main")
            logs = []
            sup = mock.Mock(git=git, log=logs.append)
            supervise.Supervisor.free_branch(sup, "plan/fix-X")
            git("worktree", "add", "-q", "-f", "-B", "plan/fix-X", str(Path(d) / "new"), "main")
            self.assertTrue(any("removed stale worktree" in x for x in logs))
            self.assertFalse((Path(d) / "old").exists())


class PlanLandGate(Toy):
    """A plan branch that fails a proven acceptance test it can break never lands (pilot2026-11-10 03:30-05:20)."""

    RED = "import sys; print('PLAN LAND RED: plan/x: regression: proven S03 t fails (exit 1): boom'); sys.exit(1)"

    def red_sup(self) -> supervise.Supervisor:
        sup = self.sup()
        sup.plan_land_cmd = [sys.executable, "-c", self.RED]
        return sup

    def test_a_red_plan_land_stops_the_round_and_main_does_not_move(self):
        self.make(exits=[3, 0], needs=["S01"], kinds={"S01": "acceptance"})
        head = git(self.repo, "rev-parse", "main")
        self.assertEqual(self.red_sup().supervise(), 1)
        self.assertIn("fails proven tests (main would go red): PLAN LAND RED", self.log())
        self.assertEqual(git(self.repo, "rev-parse", "main"), head)
        self.assertEqual(self.calls("reset"), [])

    def test_a_green_plan_land_is_logged_before_main_moves(self):
        self.make(exits=[3, 0], needs=["S01"], kinds={"S01": "acceptance"})
        self.assertEqual(self.sup().supervise(), 0, self.log())
        self.assertRegex(self.log(), r"plan_land plan/fixes-r1: PLAN LAND OK: plan/fixes-r1: 1 proven rows in the "
                                     r"footprint, no \[commands\] acceptance to run\n(.*\n)?.*main -> plan/fixes-r1")


class IndexRebase(unittest.TestCase):
    """A plan branch's INDEX regenerate commit and a land's both changed spec/index: the rebase takes main's copy."""

    def test_a_rebase_stopped_only_on_generated_index_files_finishes_and_a_real_conflict_does_not(self):
        root = Path(tempfile.mkdtemp(prefix="sup-idx-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        repo = root / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")

        def commit(files, msg):
            for rel, text in files.items():
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text(text, encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", msg)

        commit({"spec/index/a.md": "a 1\n", "spec/INDEX.md": "a: 1\n", "src/a.py": "x = 1\n"}, "m0")
        git(repo, "checkout", "-q", "-b", "plan/p")
        commit({"spec/FUNCTIONS.md": "rows\n"}, "plan")
        commit({"spec/index/a.md": "a plan\n", "spec/INDEX.md": "a: plan\n"}, "plan: regenerate INDEX")
        git(repo, "checkout", "-q", "main")
        commit({"spec/index/a.md": "a land\n", "spec/INDEX.md": "a: land\n", "src/a.py": "x = 2\n"}, "land")
        sup = supervise.Supervisor.__new__(supervise.Supervisor)
        sup.repo, sup.main, sup.env = repo, "main", lambda: None
        git(repo, "checkout", "-q", "plan/p")
        sup.rebase("plan/p", repo)
        self.assertEqual(git(repo, "show", "plan/p:spec/index/a.md"), "a land\n")
        self.assertEqual(git(repo, "show", "plan/p:spec/FUNCTIONS.md"), "rows\n")
        self.assertEqual(git(repo, "rev-parse", "plan/p~1"), git(repo, "rev-parse", "main"))  # empty one skipped
        commit({"src/a.py": "x = 3\n"}, "plan edits code")
        git(repo, "checkout", "-q", "main")
        commit({"src/a.py": "x = 4\n"}, "land 2")
        git(repo, "checkout", "-q", "plan/p")
        with self.assertRaises(supervise.Stop):
            sup.rebase("plan/p", repo)
        self.assertEqual(git(repo, "status", "--porcelain", "--untracked-files=no"), "")  # aborted cleanly


if __name__ == "__main__":
    unittest.main()
