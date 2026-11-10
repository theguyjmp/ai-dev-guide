"""feasibility.py: the pre-build check, with a fake agent command (no real claude).

Run from the scripts dir:  py -3 -m pytest -q tests/test_feasibility.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_drive import FILES4, Toy, git, put

import drive
import feasibility

# The fake agent: reads the prompt on stdin, answers for the row it names from answers.json (default: buildable,
# no files), logs each call to calls.txt and prints claude's --output-format json result object.
FAKE_AGENT = r'''
import json, re, sys
from pathlib import Path
here = Path(__file__).parent
prompt = sys.stdin.read()
fid = re.search(r"^## Row (\S+)", prompt, re.M).group(1)
with open(here / "calls.txt", "a", encoding="utf-8") as fh:
    fh.write(fid + "\n")
answers = json.loads((here / "answers.json").read_text(encoding="utf-8"))
ans = answers.get(fid, {"files": [], "needs": {}, "fits": {"ok": True, "why": "small"}})
if ans == "crash":
    print("boom", file=sys.stderr)
    sys.exit(3)
print(json.dumps({"type": "result", "result": "Here it is:\n```json\n" + json.dumps(ans) + "\n```",
                  "usage": {"input_tokens": 10, "cache_read_input_tokens": 4000, "output_tokens": 50}}))
'''


def row(**kw) -> feasibility.Row:
    base = {"id": "F2", "module": "alpha", "deps": ["F1"], "table": "| F2 |", "acceptance": "", "section": "## alpha",
            "cell": ["src/alpha/f2.py", "src/alpha/__init__.py"], "own": ["src/alpha/"], "dep_paths": ["src/core/"],
            "protected": ("tests/acceptance/", "spec/"), "unit": ["tests/unit/"],
            "plan": {"F1": ("alpha", "planned", "x"), "F2": ("alpha", "planned", "y"), "F9": ("beta", "planned", "z"),
                     "F0": ("core", "proven", "w")}}
    base.update(kw)
    return feasibility.Row(**base)


class TestParse(unittest.TestCase):
    def test_claude_result_object_and_bare_answer(self):
        ans = {"files": ["./src/a.py", "`src/b.py`"], "needs": {"add": "F1"}, "fits": {"ok": False, "why": "1M rows"}}
        out = json.dumps({"type": "result", "result": "text " + json.dumps(ans), "usage": {"cache_read_input_tokens": 7}})
        got, usage = feasibility.parse(out)
        self.assertEqual(got["files"], ["src/a.py", "src/b.py"])
        self.assertEqual(got["fits"], {"ok": False, "why": "1M rows"})
        self.assertEqual(usage["cache_read_input_tokens"], 7)
        self.assertEqual(feasibility.parse(json.dumps(ans))[0]["needs"], {"add": "F1"})
        self.assertIsNone(feasibility.parse("no json here")[0])

    def test_fits_and_needs_variants(self):
        n = feasibility.normalize({"files": "src/a.py", "fits": "no",
                                   "needs": [{"capability": "store add", "provider": "MISSING"}]})
        self.assertEqual((n["files"], n["fits"]["ok"], n["needs"]), (["src/a.py"], False, {"store add": "MISSING"}))
        self.assertTrue(feasibility.normalize({"files": [], "fits": {"answer": "yes"}})["fits"]["ok"])


class TestVerdict(unittest.TestCase):
    def verdict(self, ans: dict, r: feasibility.Row | None = None):
        ans = feasibility.normalize({"files": [], "fits": True, **ans})
        return feasibility.verdict(ans, r or row(), drive.paths_overlap)

    def test_buildable_row_has_no_problem(self):
        self.assertEqual(self.verdict({"files": ["src/alpha/f2.py", "tests/acceptance/test_f2.py", "tests/unit/t.py"],
                                       "needs": {"add": "F1", "parse": "main", "load": "F0"}}), ([], []))

    def test_missing_capability_and_unordered_provider(self):
        problems, _ = self.verdict({"needs": {"store add record": "MISSING", "render": "F9"}})
        self.assertEqual(len(problems), 2)
        self.assertIn("`store add record`: MISSING", problems[0])
        self.assertIn("from F9, which is neither proven nor a dep of F2", problems[1])

    def test_too_big(self):
        problems, _ = self.verdict({"fits": {"ok": False, "why": "three verbs and a 1M fixture"}})
        self.assertEqual(problems, ["does not fit one 30-min builder: three verbs and a 1M fixture"])

    def test_files_widen_in_module_or_dep_else_problem(self):
        problems, widen = self.verdict({"files": ["src/alpha/registry.py", "src/core/store.py", "src/beta/x.py"]})
        self.assertEqual(widen, ["src/alpha/registry.py", "src/core/store.py"])
        self.assertEqual(problems, ["must change `src/beta/x.py`, outside module alpha and the modules it depends on"])


class FeasToy(Toy):
    """drive.py with a fake feasibility agent."""

    def setup(self, answers: dict, extra: str = "", functions: str = FILES4.format(f4="src/alpha/f4.py")) -> Path:
        agent = Path(self.mk_agent_dir()) / "agent.py"
        put(agent, FAKE_AGENT)
        put(agent.parent / "answers.json", json.dumps(answers))
        self.agent_dir = agent.parent
        cmd = json.dumps([sys.executable, agent.as_posix()])
        return self.make_repo(functions=functions, max_parallel=4,
                              harness_extra=f"feasibility_cmd = {cmd}\n{extra}")

    def mk_agent_dir(self) -> str:
        import tempfile
        d = tempfile.mkdtemp(prefix="feas-agent-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d

    def drive_cli(self, repo: Path, *args: str) -> tuple[int, str]:
        """Toy.drive_cli with room for the checks: four drive runs at -n 6 took over Toy's 180 s."""
        p = subprocess.run([sys.executable, str(SCRIPTS / "drive.py"), "--repo", str(repo), *args],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=420)
        return p.returncode, p.stdout + p.stderr

    def calls(self) -> list[str]:
        p = self.agent_dir / "calls.txt"
        return p.read_text(encoding="utf-8").split() if p.is_file() else []

    def kinds(self, repo: Path, fid: str) -> list[str]:
        return [e["event"] for e in self.events(repo) if e.get("fn") == fid]


class TestDriveGate(FeasToy):
    def test_missing_capability_parks_before_any_build(self):
        repo = self.setup({"F1": {"files": ["src/alpha/f1.py"], "needs": {"store add record": "MISSING"},
                                  "fits": {"ok": True, "why": "small"}}})
        _, out = self.drive_cli(repo)
        st = self.state(repo)["functions"]
        self.assertEqual(st["F1"]["status"], "needs-plan", out)
        self.assertIn("router infeasible", st["F1"]["last_reason"])
        self.assertIn("`store add record`: MISSING", st["F1"]["last_reason"])
        self.assertNotIn("build_start", self.kinds(repo, "F1"))  # no builder spent on it
        self.assertEqual(st["F1"]["attempts"], 0)
        route = json.loads((repo / ".overhaul" / "router" / "routes" / "F1.json").read_text(encoding="utf-8"))
        self.assertEqual(route["class"], "infeasible")
        self.assertTrue(any(e["event"] == "routed" and "class=infeasible" in e.get("detail", "")
                            for e in self.events(repo)))
        for fid in ("F2", "F3", "F4"):
            self.assertEqual(st[fid]["status"], "proven", out)
            self.assertIn("feasibility_ok", self.kinds(repo, fid))
        self.assertEqual(sorted(self.calls()), ["F1", "F2", "F3", "F4"])  # one call per row

    def test_too_big_parks(self):
        repo = self.setup({"F2": {"files": [], "needs": {}, "fits": {"ok": False, "why": "1M-row fixture"}}})
        self.drive_cli(repo)
        st = self.state(repo)["functions"]
        self.assertEqual(st["F2"]["status"], "needs-plan")
        self.assertIn("does not fit one 30-min builder: 1M-row fixture", st["F2"]["last_reason"])

    def test_file_in_module_beyond_cell_widens_claim_and_brief(self):
        repo = self.setup({"F1": {"files": ["src/alpha/f1.py", "src/alpha/registry.py"], "needs": {},
                                  "fits": {"ok": True, "why": "small"}}})
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        st = self.state(repo)["functions"]
        self.assertEqual(st["F1"]["status"], "proven")
        self.assertEqual(st["F1"]["files_extra"], ["src/alpha/registry.py"])
        widened = [e for e in self.events(repo) if e["event"] == "files_widened"]
        self.assertEqual([(e["fn"], e["detail"]) for e in widened], [("F1", "src/alpha/registry.py")])
        self.assertIn("`src/alpha/registry.py`", self.seen("F1-build-1.md"))  # in the builder's allowed paths
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        self.assertIn("src/alpha/registry.py", drv.claims(drv.fns["F1"]))

    def test_agent_failure_builds_as_before(self):
        repo = self.setup({"F3": "crash"})
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        st = self.state(repo)["functions"]
        self.assertEqual({k: v["status"] for k, v in st.items()}, {f"F{i}": "proven" for i in range(1, 5)})
        self.assertIn("feasibility_error", self.kinds(repo, "F3"))

    def test_off_means_old_behaviour(self):
        repo = self.setup({"F1": {"files": [], "needs": {"x": "MISSING"}, "fits": True}}, extra="feasibility = false")
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.state(repo)["functions"]["F1"]["status"], "proven")

    def test_custom_worker_without_feasibility_cmd_skips(self):
        self.assertIsNone(feasibility.agent_cmd({"worker_cmd": ["py", "w.py"]}))
        self.assertIsNone(feasibility.agent_cmd({"feasibility": False, "feasibility_cmd": "x"}))
        self.assertEqual(feasibility.agent_cmd({"worker_cmd": "w", "feasibility_cmd": "py a.py"}), ["py", "a.py"])


PACKET = {"files": ["src/alpha/f1.py"], "needs": {"parse": "main"}, "fits": {"ok": True, "why": "one verb"},
          "packet": {"edits": [{"file": "src/alpha/f1.py", "after": "def main(argv)", "what": "dispatch `add`"}],
                     "implement": ["def add(a: int, b: int) -> int"],
                     "helpers": [{"ref": "src/alpha/core.py::parse_int", "signature": "def parse_int(s: str) -> int"}],
                     "test": "py -3 -m pytest -q tests/acceptance -k test_add"}}


class TestWorkPacket(FeasToy):
    def test_packet_tops_the_brief_and_cuts_the_first_budget(self):
        repo = self.setup({"F1": PACKET}, functions=FILES4.format(f4="src/alpha/f4.py"))
        toml = repo / "overhaul.toml"
        toml.write_text(toml.read_text(encoding="utf-8").replace('fake_worker.py"]', 'fake_worker.py", "--max-turns", "12"]')
            .replace("[limits]\n", "[limits]\npacket_max_calls = 5\n"), encoding="utf-8")
        git(repo, "commit", "-qam", "budget")
        rc, out = self.drive_cli(repo)
        self.assertEqual(rc, 0, out)
        brief = self.seen("F1-build-1.md")
        title, _, rest = brief.partition("\n")
        self.assertTrue(title.startswith("# Build F1"))
        self.assertTrue(rest.lstrip().startswith("## Work packet: start here"))
        for part in ("do not explore beyond these files unless a test fails", "Tool-call budget: 5",
                     "`src/alpha/f1.py` after `def main(argv)`: dispatch `add`", "`def add(a: int, b: int) -> int`",
                     "`src/alpha/core.py::parse_int`: `def parse_int(s: str) -> int`",
                     "Test: `py -3 -m pytest -q tests/acceptance -k test_add`", "Stop at about 5 tool calls"):
            self.assertIn(part, brief)
        self.assertNotIn("Work packet", self.seen("F2-build-1.md"))  # no packet: the brief as before
        ends = {e["fn"]: e["detail"] for e in self.events(repo) if e["event"] == "build_end"}
        self.assertIn("/5", ends["F1"].split()[1])  # turns=<n>/5: --max-turns was 5, not 12
        self.assertIn("/12", ends["F2"].split()[1])
        self.assertIn("packet", self.kinds(repo, "F1"))

    def test_retry_keeps_packet_with_full_budget_and_no_packet_without_one(self):
        repo = self.setup({"F1": PACKET})
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        f = drv.fns["F1"]
        feasibility.check(drv.feas.row(f), drv.feas.cmd, repo, drv.ov, "main")
        text, calls = drv.feas.packet(f, 1)
        self.assertEqual(calls, min(drv.max_calls, feasibility.PACKET_CALLS))  # never above builder_max_calls
        self.assertIn("## Work packet", text)
        text2, calls2 = drv.feas.packet(f, 2)
        self.assertEqual(calls2, drv.max_calls)
        self.assertIn(f"Tool-call budget: {drv.max_calls}", text2)
        self.assertEqual(drv.feas.packet(drv.fns["F2"], 1), ("", drv.max_calls))  # never checked: no packet
        self.assertEqual(feasibility.embed("# T\nbody", ""), "# T\nbody")
        self.assertEqual(feasibility.norm_packet({"edits": [], "test": "x"}), {})  # no edit: no packet

    def test_packet_that_does_not_fit_is_split_before_a_build(self):
        repo = self.setup({"F2": {**PACKET, "fits": {"ok": False, "why": "three verbs"}}})
        self.drive_cli(repo)
        st = self.state(repo)["functions"]["F2"]
        self.assertEqual(st["status"], "needs-plan")
        self.assertIn("split", st["last_reason"])
        self.assertNotIn("build_start", self.kinds(repo, "F2"))


class TestCache(FeasToy):
    def test_cached_until_row_or_named_file_changes(self):
        repo = self.setup({})
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        gate, f = drv.feas, drv.fns["F1"]
        r = gate.row(f)
        doc = feasibility.check(r, gate.cmd, repo, drv.ov, "main")
        self.assertEqual(doc["usage"]["cache_read_input_tokens"], 4000)
        self.assertIsNotNone(feasibility.cached(drv.ov, r, repo, "main"))
        self.assertIn("src/alpha/f1.py", doc["blobs"])  # the files cell keys the cache, absent on main = ''
        put(repo / "src" / "alpha" / "f1.py", "def add(a, b):\n    return a + b\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "f1 lands")
        self.assertIsNone(feasibility.cached(drv.ov, r, repo, "main"))  # main's file changed: ask again
        feasibility.check(r, gate.cmd, repo, drv.ov, "main")
        self.assertIsNotNone(feasibility.cached(drv.ov, r, repo, "main"))
        changed = feasibility.Row(**{**r.__dict__, "table": r.table.replace("toy add", "toy sum")})
        self.assertIsNone(feasibility.cached(drv.ov, changed, repo, "main"))  # the row text changed

    def test_prompt_puts_fixed_instructions_first_and_maps_main(self):
        repo = self.setup({})
        put(repo / "src" / "alpha" / "core.py", "def helper(x):\n    return x\n\nclass Store:\n    pass\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "code")
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        text = feasibility.prompt(drv.feas.row(drv.fns["F2"]), repo, "main")
        self.assertTrue(text.startswith(feasibility.INSTRUCTIONS))  # one cached prefix for every row
        self.assertIn("src/alpha/core.py:1:def helper(x):", text)
        self.assertIn("src/alpha/core.py:4:class Store:", text)
        self.assertRegex(text, re.compile(r"^## Row F2 ", re.MULTILINE))


class TestPlanValidate(FeasToy):
    def test_feasibility_flag_reports_rows(self):
        repo = self.setup({"F1": {"files": ["src/alpha/f1.py", "src/alpha/reg.py"], "needs": {}, "fits": True},
                           "F2": {"files": [], "needs": {"undo": "MISSING"}, "fits": True}})
        import plan_validate
        lines = plan_validate.feasibility_rows(repo, {"F1", "F2"})
        self.assertEqual(lines, ["F1: files cell lacks `src/alpha/reg.py` (the change must edit it)",
                                 "F2: infeasible: needs `undo`: MISSING, no proven or dep row provides it"])
        self.assertEqual(sorted(self.calls()), ["F1", "F2"])
        self.assertEqual(plan_validate.feasibility_rows(repo, {"F1", "F2"}), lines)  # cached: no new call
        self.assertEqual(len(self.calls()), 2)
        p = subprocess.run([sys.executable, str(SCRIPTS / "plan_validate.py"), "--help"], capture_output=True,
                           text=True, check=False)
        self.assertIn("--feasibility", p.stdout)


if __name__ == "__main__":
    unittest.main()
