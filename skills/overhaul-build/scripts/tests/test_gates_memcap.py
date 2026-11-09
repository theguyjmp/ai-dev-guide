"""Gates and memory cap: per-gate green, unavailable CI gates, the Windows per-process memory cap."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive  # noqa: E402
import land  # noqa: E402
import memcap  # noqa: E402
import periodic  # noqa: E402
import verdict  # noqa: E402
from tests.test_drive import Toy, git, put  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1]
BILLING = ("X The job was not started because recent account payments have failed or your spending limit needs to "
           "be increased. Please check the 'Billing & plans' section in your settings\nparity (ubuntu-latest, linux)\n"
           "X The configuration 'macos-14-arm64-us-xl' is not supported\nparity (macos-14, macos)\n")


def fake_gh(conclusion: str, jobs: list, view: str = BILLING):
    calls = []

    def run(*a, timeout=0):
        calls.append(a)
        if a[:2] == ("run", "list"):
            return 0, json.dumps([{"databaseId": 7, "status": "completed", "conclusion": conclusion, "headSha": "abc"}])
        if "--json" in a:
            return 0, json.dumps({"jobs": jobs})
        if "--log" in a:
            return 0, ""
        return (0, view) if a[:2] == ("run", "view") else (0, "")
    return run, calls


class CiUnavailable(unittest.TestCase):
    def ci(self, run):
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            code = periodic.ci("p.yml", "abc", 1, run=run, git=lambda *a: 0, sleep=lambda s: None)
        return code, out

    def test_jobs_never_started_is_unavailable_not_red(self):
        run, _ = fake_gh("cancelled", [{"name": "linux", "steps": []}, {"name": "macos", "steps": []}])
        code, out = self.ci(run)
        self.assertEqual(code, periodic.UNAVAILABLE)
        self.assertIn("ci: unavailable: The job was not started because recent account payments", out[-1])
        self.assertIn("'macos-14-arm64-us-xl' is not supported", out[-1])

    def test_a_job_that_ran_and_failed_is_a_real_red(self):
        run, _ = fake_gh("failure", [{"name": "linux", "steps": [{"name": "test", "conclusion": "failure"}]},
                                     {"name": "macos", "steps": []}])
        self.assertEqual(self.ci(run)[0], 1)

    def test_a_failure_without_billing_annotations_is_red(self):
        run, _ = fake_gh("failure", [{"name": "linux", "steps": []}], view="X Process completed with exit code 1.\n")
        self.assertEqual(self.ci(run)[0], 1)

    def test_success_reads_no_annotations(self):
        run, calls = fake_gh("success", [])
        self.assertEqual(self.ci(run)[0], 0)
        self.assertFalse([a for a in calls if "--json" in a and "jobs" in a])


class Verdict(unittest.TestCase):
    def test_unavailable_gate_is_an_owner_line(self):
        p = {"view": [], "proven": 1, "total": 1, "fpath": "F", "cfg": {},
             "state": {"periodic": {"unavailable": {"parity": {"sha": "abc", "reason": "spending limit"}}}}}
        doc = verdict.judge(p, {}, verdict.now(), None, [])
        self.assertIn("parity gate unavailable: spending limit", str(doc))
        self.assertNotIn("periodic gate parity red", str(doc))


class PerGateGreen(Toy):
    """parity cannot run, there is no all-gates green, scale was green once (only in the log): scale still bisects."""

    def test_red_gate_bisects_from_its_own_green_while_another_is_unavailable(self):
        repo = self.make_repo()
        toml = repo / "overhaul.toml"
        toml.write_text(toml.read_text(encoding="utf-8") + '\n[gates.periodic]\nevery_lands = 1\n'
                        '[gates.periodic.commands]\n'
                        'parity = "py -3 -c \\"import sys; print(\'ci: unavailable: spending limit\'); sys.exit(75)\\""\n'
                        'scale = "py -3 -c \\"import sys; sys.exit(open(\'flag\').read() == \'bad\')\\""\n',
                        encoding="utf-8")
        put(repo / "flag", "ok")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "plan gates")
        green = git(repo, "rev-parse", "HEAD").strip()
        for fid, flag in (("F1", "ok"), ("F2", "bad"), ("F3", "bad")):
            put(repo / "flag", flag)
            git(repo, "commit", "-qa", "--allow-empty", "-m", f"land {fid}: proven")
        ov = repo / ".overhaul"
        ov.mkdir(exist_ok=True)
        with (ov / "log.jsonl").open("a", encoding="utf-8") as fh:  # an older run: scale green, parity red
            fh.write(json.dumps({"event": "periodic_green", "detail": f"scale run at {green[:12]} "}) + "\n")
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        drv.fns["F3"] = drv.fns["F2"]
        drv.st["periodic"] = {"last_green": "", "lands": 3, "red": {"parity": {"sha": green, "reason": "cancelled",
                                                                              "culprit": "x (no earlier green)"}}}
        self.assertEqual(drv.periodic.st["green"], {"scale": green})
        with mock.patch.object(drv, "start_polish_fix") as fix:
            for _ in range(600):
                drv.periodic.step()
                if not drv.periodic.busy() and fix.called:
                    break
                time.sleep(0.05)
        per = drv.st["periodic"]
        drv.periodic.stop()
        self.assertEqual(per["red"]["scale"]["culprit"], "F2")
        self.assertEqual(fix.call_args.kwargs["fid"], "depfix-periodic-scale")
        self.assertNotIn("parity", per["red"])
        self.assertEqual(per["unavailable"]["parity"]["reason"], "spending limit")
        self.assertEqual(per["last_green"], "")
        self.assertEqual(fix.call_count, 1)


class FakeDrv:
    def __init__(self, statuses: dict):
        self.st = {"functions": {f: {"status": s} for f, s in statuses.items()}, "periodic": {"green": {}}}
        self.fns = {f: None for f in statuses}


class Cadence(unittest.TestCase):
    RAW = {"gates": {"periodic": {"every_lands": 10, "every_min": {"default": 120, "parity": 10080},
                                  "commands": {"parity": "p", "scale": "s"}}}}

    def per(self, statuses=None, **st):
        cfg = periodic.config(self.RAW)
        p = periodic.Periodic(FakeDrv(statuses or {"F1": "proven", "F2": "building"}), cfg)
        p.st.update(**st)
        return p

    def test_a_gate_with_its_own_cadence_waits_for_it(self):
        now = time.time()
        p = self.per(lands=10, last_run=now - 3 * 3600)
        self.assertEqual(p.due_gates(False), ["scale"])  # parity: weekly only, its lands do not count
        p.st.update(since={"parity": 50, "scale": 1}, ran={"parity": now - 8 * 86400, "scale": now})
        self.assertEqual(p.due_gates(False), ["parity"])
        self.assertEqual(p.every("every_lands", "parity"), float("inf"))
        self.assertEqual(p.every("every_min", "scale"), 120)

    def test_idle_runs_it_once_only_when_every_function_is_proven(self):
        now = time.time()
        p = self.per(lands=2, last_run=now)
        self.assertEqual(p.due_gates(True), ["scale"])  # build not finished: parity waits
        p = self.per({"F1": "proven", "F2": "proven"}, lands=2, last_run=now)
        self.assertEqual(p.due_gates(True), ["parity", "scale"])
        p.st["finish"] = {"parity": 2}  # its finish run done for this plan
        self.assertEqual(p.due_gates(True), ["scale"])

    def test_since_counts_per_gate(self):
        p = self.per(lands=0, last_run=time.time(), since={"parity": 3, "scale": 0})
        p.landed()
        self.assertEqual((p.st["lands"], p.st["since"]), (1, {"parity": 4, "scale": 1}))


class MemCap(unittest.TestCase):
    def test_note_names_the_cap_only_on_an_allocation_failure(self):
        with mock.patch.dict(os.environ, {memcap.ENV: "12"}):
            self.assertEqual(memcap.note("memory allocation of 17179869184 bytes failed", "exit 3"),
                             "process memory cap 12 GB hit: exit 3")
            self.assertEqual(memcap.note("assertion failed", "exit 1"), "exit 1")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(memcap.ENV, None)
            self.assertEqual(memcap.note("memory allocation of 1 bytes failed", "exit 3"), "exit 3")

    def test_off_and_non_windows_do_nothing(self):
        self.assertEqual(memcap.cap_self(0), "")
        with mock.patch.object(memcap.os, "name", "posix"):
            self.assertEqual(memcap.cap_self(12), "")

    def test_default_limit_and_land_reads_it(self):
        self.assertEqual(drive.DEFAULTS["limits"]["proc_mem_gb"], 12)
        self.assertEqual(land.proc_mem_gb(str(SCRIPTS / "no-such-repo")), 12.0)

    def test_land_command_failure_names_the_cap(self):
        out = SCRIPTS / "tests" / "_memcap_land.out"
        self.addCleanup(lambda: out.unlink(missing_ok=True))
        cmd = 'py -3 -c "import sys; print(\'memory allocation of 99 bytes failed\'); sys.exit(3)"'
        with mock.patch.dict(os.environ, {memcap.ENV: "12"}):
            code, tail = land.run(cmd, str(SCRIPTS), str(out), 60)
        self.assertEqual(code, 3)
        self.assertTrue(tail.startswith("process memory cap 12 GB hit"), tail)

    @unittest.skipUnless(os.name == "nt", "Job Objects are Windows only")
    def test_child_over_the_cap_fails_its_allocation_and_small_ones_run(self):
        prog = ("import subprocess, sys; sys.path.insert(0, sys.argv[1]); import memcap; print(memcap.cap_self(0.25));"
                "big = subprocess.run([sys.executable, '-c', 'b = bytearray(600 * 1024 ** 2)'], capture_output=True,"
                " text=True); small = subprocess.run([sys.executable, '-c', 'b = bytearray(50 * 1024 ** 2)']);"
                "print(big.returncode, 'MemoryError' in big.stderr, small.returncode)")
        r = subprocess.run([sys.executable, "-c", prog, str(SCRIPTS)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.stdout.split("\n")[0], "process memory cap 0.25 GB per process", r.stdout + r.stderr)
        self.assertEqual(r.stdout.split("\n")[1].split(), ["1", "True", "0"], r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
