"""Pass D item 5: periodic gates on main with bisect (periodic.py, drive and verdict hooks)."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive  # noqa: E402
import periodic  # noqa: E402
import verdict  # noqa: E402
from tests.test_drive import Toy, git, put  # noqa: E402

B = {"ci_factor": 2.0, "reference": "desk", "record_only": ["macos"], "budgets": {"open_1m": 2.0}}


class Units(unittest.TestCase):
    def test_config_is_off_without_commands(self):
        self.assertEqual(periodic.config({}), {})
        self.assertEqual(periodic.config({"gates": {"periodic": {"every_lands": 3}}}), {})
        c = periodic.config({"gates": {"periodic": {"every_lands": 3, "commands": {"scale": "x"}}}})
        self.assertEqual((c["every_lands"], c["every_min"], c["commands"]), (3, 120, {"scale": "x"}))

    def test_bench_budgets_d9(self):
        self.assertEqual(periodic.benches("a\nBENCH open_1m windows 3.5\n BENCH fps desk 61"),
                         [("open_1m", "windows", 3.5), ("fps", "desk", 61.0)])
        self.assertEqual(periodic.judge_bench("open_1m", "desk", 2.1, B), (2.0, False))
        self.assertEqual(periodic.judge_bench("open_1m", "linux", 3.9, B), (4.0, True))
        self.assertEqual(periodic.judge_bench("open_1m", "macos", 99, B), (None, True))
        self.assertEqual(periodic.judge_bench("other", "desk", 99, B), (None, True))

    def test_bisect_and_culprit(self):
        self.assertEqual(periodic.next_mid(-1, 3), 1)
        self.assertIsNone(periodic.next_mid(2, 3))
        self.assertEqual(periodic.culprit_fid("land F12: proven"), "F12")
        self.assertEqual(periodic.culprit_fid("toy plan"), "")

    def test_ci_waits_for_the_run_and_prints_benches(self):
        calls = []

        def run(*a, timeout=0):
            calls.append(a[:2])
            if a[:2] == ("run", "list"):
                return 0, '[{"databaseId": 7, "status": "completed", "conclusion": "success", "headSha": "abc"}]'
            return 0, "log BENCH open_1m linux 1.5\n"
        with mock.patch("builtins.print"):
            self.assertEqual(periodic.ci("p.yml", "abc", 1, run=run, git=lambda *a: 0, sleep=lambda s: None), 0)
        self.assertEqual(calls, [("workflow", "run"), ("run", "list"), ("run", "view")])

    def test_verdict_is_bad_while_a_gate_is_red(self):
        p = {"view": [], "proven": 1, "total": 1, "fpath": "F", "cfg": {},
             "state": {"periodic": {"red": {"gpu": {"sha": "abc", "reason": "over budget", "culprit": "F2"}}}}}
        doc = verdict.judge(p, {}, verdict.now(), None, [])
        self.assertIn("periodic gate gpu red", str(doc))


class Bisect(Toy):
    def test_red_gate_bisects_to_the_culprit_and_queues_one_depfix(self):
        repo = self.make_repo()
        toml = repo / "overhaul.toml"
        toml.write_text(toml.read_text(encoding="utf-8") + '\n[gates.periodic]\nevery_lands = 1\n'
                        '[gates.periodic.commands]\nscale = "py -3 -c \\"import sys; sys.exit(open(\'flag\').read() '
                        '== \'bad\')\\""\n', encoding="utf-8")
        put(repo / "flag", "ok")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "plan gates")
        green = git(repo, "rev-parse", "HEAD").strip()
        for fid, flag in (("F1", "ok"), ("F2", "bad"), ("F3", "bad")):
            put(repo / "flag", flag)
            git(repo, "commit", "-qa", "--allow-empty", "-m", f"land {fid}: proven")
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        drv.fns["F3"] = drv.fns["F2"]
        drv.periodic.st.update(last_green=green, lands=3)
        with mock.patch.object(drv, "start_polish_fix") as fix:
            for _ in range(400):
                drv.periodic.step()
                if not drv.periodic.busy() and fix.called:
                    break
                time.sleep(0.05)
        self.assertEqual(drv.st["periodic"]["red"]["scale"]["culprit"], "F2")
        self.assertEqual(fix.call_args.kwargs["fid"], "depfix-periodic-scale")
        drv.periodic.stop()


if __name__ == "__main__":
    unittest.main()
