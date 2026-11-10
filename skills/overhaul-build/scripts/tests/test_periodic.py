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


def gate_repo(test, cmd: str, culprits=(("F1", "ok"), ("F2", "bad"), ("F3", "bad")), extra=None):
    """A toy repo with one periodic gate `scale` = cmd, a green plan commit and one commit per (subject, flag)."""
    repo = test.make_repo()
    toml = repo / "overhaul.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + '\n[gates.periodic]\nevery_lands = 1\n'
                    f'[gates.periodic.commands]\nscale = {cmd}\n', encoding="utf-8")
    put(repo / "flag", "ok")
    for name, text in (extra or {}).items():
        put(repo / name, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "plan gates")
    green = git(repo, "rev-parse", "HEAD").strip()
    for subject, flag in culprits:
        put(repo / "flag", flag)
        if subject.startswith("src/"):  # a commit that only touches a module's file, with a plain subject
            put(repo / subject, flag)
            git(repo, "add", "-A")
            subject = "tidy"
        git(repo, "commit", "-qa", "--allow-empty", "-m", subject if ":" in subject or subject == "tidy"
            else f"land {subject}: proven")
    drv = drive.Driver(repo, drive.load_config(repo))
    drv.load()
    drv.fns["F3"] = drv.fns["F2"]
    drv.periodic.st.update(last_green=green, lands=len(culprits))
    return repo, drv


def spin(drv, done, n=400):
    for _ in range(n):
        drv.periodic.step()
        if not drv.periodic.busy() and done():
            return
        time.sleep(0.05)


FLAG = '"py -3 -c \\"import sys; sys.exit(open(\'flag\').read() == \'bad\')\\""'


class ActsAgain(Toy):
    """H1: on 2026-11-09 the pilot project's gates stayed red for a day with no bisect and no depfix: a culprit, once found,
    stood while the gate was red, one depfix per gate was allowed ever, a non-land culprit commit was the owner's,
    and a gate whose command could not run (exit 127) was bisected like a code red."""

    def test_a_gate_still_red_after_its_depfix_is_bisected_again_with_a_second_depfix(self):
        repo, drv = gate_repo(self, FLAG)
        with mock.patch.object(drv, "start_polish_fix") as fix:
            spin(drv, lambda: fix.call_count >= 1)
            self.assertEqual(fix.call_args.kwargs["fid"], "depfix-periodic-scale")
            put(repo / "flag", "bad")  # the depfix landed (none in flight) and main is still red
            git(repo, "commit", "-qa", "--allow-empty", "-m", "land depfix-periodic-scale: fixed")
            drv.periodic.landed()
            spin(drv, lambda: fix.call_count >= 2)
        red = drv.st["periodic"]["red"]["scale"]
        self.assertEqual((fix.call_count, fix.call_args.kwargs["fid"], red["culprit"], red["depfixes"]),
                         (2, "depfix-periodic-scale-2", "F2", 2))
        self.assertEqual(fix.call_args.args[0], "beta")
        drv.periodic.stop()

    def test_the_culprit_stands_while_its_depfix_is_in_flight(self):
        _, drv = gate_repo(self, FLAG)
        with mock.patch.object(drv, "start_polish_fix") as fix:
            spin(drv, lambda: fix.called)
            drv.polish_lands.append("depfix-periodic-scale")
            drv.periodic.landed()
            spin(drv, lambda: "scale" not in drv.periodic.due_gates(False))
        self.assertEqual((fix.call_count, drv.st["periodic"]["red"]["scale"]["culprit"]), (1, "F2"))
        drv.periodic.stop()

    def test_depfixes_stop_at_depfix_max_and_the_red_is_the_owners(self):
        repo, drv = gate_repo(self, FLAG)
        drv.periodic.cfg["depfix_max"] = 1
        head = git(repo, "rev-parse", "HEAD").strip()
        drv.st["periodic"]["red"] = {"scale": {"sha": head, "reason": "x", "culprit": "", "depfixes": 1}}
        with mock.patch.object(drv, "start_polish_fix") as fix, mock.patch.object(drv, "say") as say:
            spin(drv, lambda: drv.st["periodic"]["red"]["scale"].get("culprit") == "F2")
        self.assertFalse(fix.called)
        self.assertIn("the owner decides", " ".join(str(c.args[0]) for c in say.call_args_list))
        drv.periodic.stop()

    def test_a_non_land_commit_is_blamed_on_its_function_or_the_module_owning_its_files(self):
        for subject, culprit, module in (("F2: use the shared helper", "F2", "beta"),
                                         ("src/alpha/util.py", "tidy (alpha)", "alpha")):
            with self.subTest(subject=subject):
                _, drv = gate_repo(self, FLAG, (("F1", "ok"), (subject, "bad")))
                with mock.patch.object(drv, "start_polish_fix") as fix:
                    spin(drv, lambda: fix.called)
                self.assertEqual(fix.call_args.args[0], module)
                self.assertTrue(drv.st["periodic"]["red"]["scale"]["culprit"].endswith(culprit))
                drv.periodic.stop()

    def test_a_gate_that_cannot_run_is_gate_config_never_bisected(self):
        gate = "import sys\nprint('/bin/bash: X:/nowhere/linux-gate.sh: No such file or directory')\nsys.exit(127)\n"
        _, drv = gate_repo(self, '"py -3 gate.py {sha}"', extra={"gate.py": gate})
        with mock.patch.object(drv, "start_polish_fix") as fix:
            spin(drv, lambda: bool(drv.st["periodic"].get("config")))
        st = drv.st["periodic"]
        self.assertFalse(fix.called)
        self.assertEqual((st["red"], st["bisect"], st["green"].get("scale")), ({}, None, None))
        self.assertIn("exit 127", st["config"]["scale"]["reason"])
        p = {"view": [], "proven": 1, "total": 1, "fpath": "F", "cfg": {}, "state": {"periodic": st}}
        doc = str(verdict.judge(p, {}, verdict.now(), None, []))
        self.assertIn("gate_config scale", doc)
        self.assertNotIn("periodic gate scale red", doc)
        drv.periodic.stop()


class ConfigRed(unittest.TestCase):
    def test_config_reds_are_commands_that_cannot_run(self):
        self.assertTrue(periodic.config_red(127, ""))
        self.assertTrue(periodic.config_red(126, "Permission denied"))
        self.assertTrue(periodic.config_red(1, "/bin/bash: X:/x/linux-gate.sh: No such file or directory"))
        self.assertTrue(periodic.config_red(2, "/usr/bin/bash: line 1: C:\\x.sh: No such file"))
        self.assertTrue(periodic.config_red(1, "sh: cargo: command not found"))
        self.assertTrue(periodic.config_red(1, "'foo' is not recognized as an internal or external command,"))
        self.assertFalse(periodic.config_red(1, "run_proven: 1 failed"))
        self.assertFalse(periodic.config_red(0, "command not found"))


if __name__ == "__main__":
    unittest.main()
