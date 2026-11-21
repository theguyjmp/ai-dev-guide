"""Main-red self-heal (quarantine.py, its wiring in harness.try_batch) and the beat alarms built on it. Temp folders;
git, cargo and the gate are fakes."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="fleet-quarantine-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt")})
sys.path.insert(0, str(HERE))
import beat_alarms  # noqa: E402
import harness as h  # noqa: E402
import quarantine as Q  # noqa: E402

RED = "tests/acceptance/slow.rs::test_slow"  # a proven row's test, red on main
MINE = "tests/acceptance/new.rs::test_new"  # the batch row's own test
OTHER = "tests/acceptance/old.rs::test_old"  # a proven row's test, green on main
ROWS = {"R1": {"id": "R1", "tests": [MINE], "status": "planned", "file_list": ["src/a.rs"]},
        "R2": {"id": "R2", "tests": [], "status": "planned", "file_list": ["src/b.rs"]},
        "P1": {"id": "P1", "tests": [RED], "status": "proven", "file_list": ["src/slow.rs"]},
        "P2": {"id": "P2", "tests": [OTHER], "status": "proven", "file_list": ["src/old.rs"]}}
BATCH = [{"row": "R1", "sha": "1" * 40}, {"row": "R2", "sha": "2" * 40}]


class Fixture(unittest.TestCase):
    def setUp(self):
        for f in (h.ST / Q.KNOWN, h.ST / Q.BASELINE, h.ST / Q.CHECKS, h.ST / Q.BROKEN, h.ST / "events.jsonl"):
            f.unlink(missing_ok=True)
        for f in (h.HOME / "notes").rglob("*.md*") if (h.HOME / "notes").exists() else []:
            f.unlink()
        self.runs, self.log = [], []
        self.main("a" * 40, {})

    def main(self, sha, red):
        """main is `sha`; run_alone fails exactly the tests in `red` ({test: tail})."""
        self.sha, self.red = sha, red

        @contextmanager
        def at_main():
            yield sha
        for name, fake in {"git": lambda *a, **k: SimpleNamespace(stdout=sha + "\n" if a[:2] == ("rev-parse", "main") else "",
                                                                    returncode=0),
                           "at_main": at_main,
                           "run_alone": lambda tests, timeout=1800: (self.runs.append(list(tests)),
                                                                     {t: red[t] for t in tests if t in red})[1]}.items():
            p = mock.patch.object(h, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def batch(self, gate_results):
        it = iter(gate_results)

        def gate(ids, rows, quick=False):
            fails = [f for f in next(it) if f not in h.known_red()]  # the real gate skips quarantined tests
            self.log.append(("gate", tuple(fails)))
            return not fails, fails, ""
        fakes = {"build_train": lambda batch, rows: (list(batch), []), "batch_review": lambda merged, **k: [],
                 "finish_spec": lambda ids: self.log.append(("land", tuple(ids))),
                 "reject": lambda q, why: self.log.append(("reject", q["row"])),
                 "probe_culprit": lambda merged, rows, fails: self.log.append(("probe", tuple(fails))), "gate": gate}
        h.save("queue.json", list(BATCH))  # a row lands only while queued with its sha (harness.superseded)
        with mock.patch.multiple(h, **fakes):
            return h.try_batch(list(BATCH), dict(ROWS))

    def events(self):
        return [e["event"] for e in h.read_events()]


class Heal(Fixture):
    def test_red_on_main_is_quarantined_and_the_same_batch_regated(self):
        self.main("a" * 40, {RED: "test_slow timed out after 900 seconds"})
        landed = self.batch([[RED], []])
        self.assertEqual([q["row"] for q in landed], ["R1", "R2"])
        self.assertFalse([e for e in self.log if e[0] in ("reject", "probe")])
        self.assertEqual([e[0] for e in self.log].count("gate"), 2)
        entry = h.known_red()[RED]
        self.assertIn("timed out", entry["evidence"])
        self.assertTrue(entry["reason"].startswith("red alone on main"))
        req = h.HOME / entry["request"]
        self.assertIn("keep every assertion", req.read_text(encoding="utf-8"))
        self.assertIn("quarantine", self.events())
        n = len(self.runs)
        Q.main_red(h, [RED])
        self.assertEqual(len(self.runs), n, "verdicts are cached per main sha")

    def test_build_failure_is_not_cached(self):
        self.main("b" * 40, {OTHER: h.BUILD_FAILED + "extern location for sha2 does not exist"})
        self.assertIn(OTHER, Q.main_red(h, [OTHER]))
        self.main("b" * 40, {})
        self.assertEqual(Q.main_red(h, [OTHER]), {}, "a test build failure is rerun, never cached red")

    def test_green_on_main_bisects_as_before(self):
        self.batch([[OTHER]] * 3)
        self.assertNotIn(OTHER, h.known_red())
        self.assertIn(("probe", (OTHER,)), self.log)
        self.assertIn("reject", [e[0] for e in self.log])

    def test_own_test_is_never_run_on_main(self):
        self.main("a" * 40, {MINE: "unbuilt"})
        self.batch([[MINE], []])
        self.assertFalse(any(MINE in r for r in self.runs))
        self.assertIn(("reject", "R1"), self.log)

    def test_cap_raises_main_broken_and_pauses_until_main_moves(self):
        h.save(Q.KNOWN, {f"tests/acceptance/x{i}.rs::t": "manager entry" for i in range(3)})
        self.main("b" * 40, {OTHER: "red too"})
        with self.assertRaises(Q.MainBroken):
            self.batch([[OTHER]])
        self.assertNotIn("reject", [e[0] for e in self.log])
        self.assertIn("quarantine_cap", self.events())
        self.assertTrue(Q.paused(h))
        self.main("c" * 40, {})
        self.assertFalse(Q.paused(h))
        self.assertIn("main_unpaused", self.events())

    def test_pause_leaves_only_fix_rows_and_clears_when_tests_pass_alone(self):
        pq = [{"row": "R1", "ts": 1}, {"row": "CLEAN-fix-main", "ts": 2}]
        broken = {"t": h.iso(), "sha": "e" * 40, "quarantined": [], "red": [OTHER]}
        h.save(Q.BROKEN, dict(broken))
        self.main("e" * 40, {OTHER: "still red"})
        self.assertEqual([q["row"] for q in Q.train_queue(h, pq)], ["CLEAN-fix-main"])
        self.assertTrue(Q.paused(h))
        self.assertEqual(Q.train_queue(h, pq[:1]), [])
        h.save(Q.BROKEN, dict(broken))  # forget the recheck throttle
        self.main("e" * 40, {})  # green alone now, main unchanged
        self.assertFalse(Q.paused(h))
        self.assertEqual(Q.train_queue(h, pq), pq)


class Release(Fixture):
    def test_green_on_main_returns_to_the_gate_and_is_throttled(self):
        self.main("a" * 40, {RED: "slow"})
        self.batch([[RED], []])
        h.save(Q.KNOWN, {**h.known_red(), "tests/acceptance/x0.rs::t": "manager entry"})
        self.main("d" * 40, {"tests/acceptance/x0.rs::t": "still red"})
        self.assertEqual(Q.release(h), [RED])
        self.assertEqual(list(h.known_red()), ["tests/acceptance/x0.rs::t"])
        req = Q.request_path(h, RED)
        self.assertFalse(req.exists())
        self.assertTrue(req.with_name(req.name + ".done").exists())
        self.assertIn("unquarantine", self.events())
        n = len(self.runs)
        self.assertEqual(Q.release(h), [])
        self.assertEqual(len(self.runs), n)


class Alarms(Fixture):
    def test_known_red_says_whether_a_fix_is_worked(self):
        h.save(Q.KNOWN, {RED: "manager entry"})
        self.assertIn("NO fix request", beat_alarms.known_red(h)[0])
        Q.write_request(h, RED, "tail", "e" * 40)
        self.assertIn("NOT dispatched", beat_alarms.known_red(h)[0])
        disp = h.HOME / "notes" / "DISPATCH.md"
        disp.write_text("fix-slow-1 | opus | notes/requests/fix-test-slow.md | open | -\n", encoding="utf-8")
        self.assertIn("fix worked by fix-slow-1", beat_alarms.known_red(h)[0])
        disp.write_text("fix-slow-1 | opus | notes/requests/fix-test-slow.md | done | x\n", encoding="utf-8")
        self.assertIn("still red", beat_alarms.known_red(h)[0])
        h.save(Q.KNOWN, {})
        self.assertEqual(beat_alarms.known_red(h), [])

    def test_repeat_test_fires_on_two_gates_unless_quarantined(self):
        ev = [{"event": "gate_stage", "stage": "acceptance", "failed": [OTHER]},
              {"event": "gate_stage", "stage": "acceptance-quick", "failed": [OTHER, MINE]}]
        out = beat_alarms.repeat_test(h, ev)
        self.assertTrue(out and out[0].startswith(f"REPEAT-TEST {OTHER} failed 2"))
        self.assertEqual(beat_alarms.repeat_test(h, ev[:1]), [])
        h.save(Q.KNOWN, {OTHER: "manager entry"})
        self.assertEqual(beat_alarms.repeat_test(h, ev), [])

    def test_origin_behind(self):
        self.assertEqual(beat_alarms.origin_behind(lambda *a: "0\n"), [])
        self.assertEqual(beat_alarms.origin_behind(lambda *a: ""), [])
        self.assertTrue(beat_alarms.origin_behind(lambda *a: "3\n")[0].startswith("ORIGIN-BEHIND origin main is 3"))


class MergedSpec(Fixture):
    def test_the_gate_reads_the_acceptance_the_batch_lands(self):
        """A batch that rewrites R1's acceptance (MINE -> OTHER) is gated with OTHER: main's old line names a test the
        row may have moved and deleted."""
        spec = h.TRAIN / h.SPEC
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(f"| R1 | 1 | a | - | src/a.rs | {OTHER} | planned | src/a.rs |\n", encoding="utf-8")
        self.addCleanup(spec.unlink)
        seen = []
        with mock.patch.object(h, "gate", lambda ids, rows, quick=False: (seen.append(rows["R1"]["tests"]), (True, [], ""))[1]), \
                mock.patch.multiple(h, build_train=lambda batch, rows: (list(batch), []), batch_review=lambda m, **k: [],
                                    finish_spec=lambda ids: None, reject=lambda q, why: None):
            h.save("queue.json", list(BATCH))
            h.try_batch(list(BATCH), dict(ROWS))
        self.assertEqual(seen, [[OTHER]])
        self.assertEqual(ROWS["R1"]["tests"], [MINE])  # main's rows are not mutated

    def test_no_train_spec_keeps_main_rows(self):
        self.assertIs(h.train_spec(ROWS), ROWS)


class AloneRunner(unittest.TestCase):
    """run_tests and run_alone share one alone-runner: pass, failure tail, perf debt, timeout, missing binary."""
    OUT = {"t_ok": (0, "ok"), "t_red": (101, "assert left == right"), "t_perf": (101, "frame over budget: 12 ms"), "t_slow": None}
    T = [f"tests/acceptance/ra.rs::{n}" for n in ("t_ok", "t_red", "t_perf", "t_slow")] + ["tests/acceptance/rc.rs::t_ok"]

    def sh(self, cmd, cwd=None, check=True, env=None, timeout=None, stdin=None):
        if cmd[0] == "cargo":
            exe = json.dumps({"executable": "bin/ra", "target": {"name": "ra", "kind": ["test"]}})
            return SimpleNamespace(returncode=0, stdout=exe, stderr="")
        if self.OUT[cmd[-1]] is None:
            raise subprocess.TimeoutExpired(cmd, 900)
        rc, out = self.OUT[cmd[-1]] if cmd[2] == "--test-threads=1" else (101, "loaded")
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    def test_cargo(self):
        with mock.patch.object(h, "sh", self.sh), mock.patch.dict(h.P, accept="cargo"):
            want = sorted([self.T[1], self.T[3], self.T[4]])
            self.assertEqual(sorted(h.run_tests(self.T)), want)
            alone = h.run_alone(self.T)
        self.assertEqual(sorted(alone), want)
        self.assertIn("assert left", alone[self.T[1]])
        self.assertIn("900", alone[self.T[3]])
        self.assertIn("test build failed", alone[self.T[4]])

    def test_cmd_profile(self):
        def sh(argv, **k):
            name = next(a for a in argv if a.startswith("--n=")).split("=", 1)[1].strip("^$")
            if name == "t_slow":
                raise subprocess.TimeoutExpired(argv, 900)
            rc, out = self.OUT[name]
            return SimpleNamespace(returncode=rc, stdout=f"not ok 1 - {name}\n{out}" if rc else out, stderr="")
        prof = {"accept": "runner {names} {file}", "accept_name": "--n=^{name}$", "accept_fail_re": r"^not ok \d+ - (.+)$"}
        with mock.patch.object(h, "sh", sh), mock.patch.dict(h.P, prof):
            alone = h.run_alone(self.T[:4])
        self.assertEqual(sorted(alone), sorted([self.T[1], self.T[3]]))


if __name__ == "__main__":
    unittest.main()
