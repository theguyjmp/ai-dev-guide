"""verdict.py and dash.py on temp toy repos with fake commands.

Run from the scripts dir: py -3 -m unittest
"""
import contextlib
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS)
import verdict as vd
import dash  # noqa: E402

HEAD = "| id | phase | module | deps | entry | acceptance | status |\n|---|---|---|---|---|---|---|\n"


def fake(code, tag=""):
    """A TOML literal string holding a command that exits with code (tag lands in argv, unused)."""
    return f"'\"{sys.executable}\" -c \"raise SystemExit({code})\" {tag}'"


def ago(**kw):
    return vd.iso(vd.now() - dt.timedelta(**kw))


class VerdictTest(unittest.TestCase):
    def repo(self, statuses, commands=None, state=None, prev=None, events=None):
        d = tempfile.mkdtemp(prefix="verdict-")
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "spec"))
        os.makedirs(os.path.join(d, ".overhaul"))
        rows = "".join(f"| F{i} | 0 | mod{i} | - | src/m{i}.py:run | test_f{i} | {s} |\n"
                       for i, s in enumerate(statuses, 1))
        with open(os.path.join(d, "spec", "FUNCTIONS.md"), "w", encoding="utf-8") as f:
            f.write("# Functions\n\n" + HEAD + rows + "\nNotes after the table.\n")
        cmds = commands if commands is not None else {k: fake(0) for k in vd.CHECKS}
        with open(os.path.join(d, "overhaul.toml"), "w", encoding="utf-8") as f:
            f.write("[commands]\n" + "".join(f"{k} = {v}\n" for k, v in cmds.items())
                    + "[limits]\nbuilder_max_calls = 60\n")
        for name, doc in (("state.json", state), ("verdict.json", prev)):
            if doc is not None:
                with open(os.path.join(d, ".overhaul", name), "w", encoding="utf-8") as f:
                    json.dump(doc, f)
        if events:
            with open(os.path.join(d, ".overhaul", "log.jsonl"), "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(e) + "\n" for e in events) + "not json\n")
        return d

    def verdict(self, repo, *extra):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = vd.main(["--repo", repo, *extra])
        with open(os.path.join(repo, ".overhaul", "verdict.json"), encoding="utf-8") as f:
            doc = json.load(f)
        return code, buf.getvalue().strip(), doc

    def kinds(self, doc):
        return [r["kind"] for r in doc["reds"]]

    def test_disk_low_is_a_red_with_the_number(self):  # drive.py's disk guard (pilot 2026-11-09 ENOSPC)
        state = {"cycle": 1, "functions": {}, "disk_low": {"free_gb": 12.5, "min_gb": 40.0, "t": ago(seconds=0)}}
        code, line, doc = self.verdict(self.repo(["proven", "planned"], state=state))
        self.assertEqual(code, 1)
        self.assertIn("disk", self.kinds(doc))
        self.assertIn("disk low: 12.5 GB free < min_free_gb 40.0", line)

    def test_good(self):
        state = {"cycle": 3, "functions": {"F3": {"status": "building", "attempts": 1}},
                 "proven_history": [[ago(hours=2), 0], [ago(hours=1), 1], [ago(seconds=0), 2]]}
        cmds = {k: fake(0) for k in vd.CHECKS}
        cmds["test"] = fake(5, "pytest")  # pytest exit 5, no tests collected, is a pass
        d = self.repo(["proven", "proven", "planned", "planned"], cmds, state, {"t": ago(hours=1), "proven": 1})
        code, line, doc = self.verdict(d)
        self.assertEqual(code, 0)
        self.assertEqual(line, "VERDICT GOOD proven 2/4 rate 1.0/h reds: none")
        self.assertEqual(doc["verdict"], "GOOD")
        self.assertEqual(doc["counts"], {"proven": 2, "building": 1, "planned": 1})
        self.assertTrue(all(c["ok"] for c in doc["checks"].values()))
        self.assertEqual(len(doc["checks"]), 5)
        last = vd.read_log(os.path.join(d, ".overhaul", "log.jsonl"))[-1]
        self.assertEqual((last["event"], last["detail"]), ("verdict", line))

    def test_bad_no_progress(self):
        state = {"functions": {"F3": {"status": "review", "attempts": 2, "last_reason": "acceptance test_f3 failed"}}}
        d = self.repo(["proven", "proven", "planned", "planned"], state=state, prev={"t": ago(hours=1), "proven": 2})
        code, line, doc = self.verdict(d, "--no-checks")
        self.assertEqual(code, 1)
        self.assertTrue(line.startswith("VERDICT BAD proven 2/4 rate 0.0/h reds: proven stuck at 2"), line)
        self.assertEqual(self.kinds(doc), ["progress"])
        self.assertIn("drive.py --repo R --reset F3", doc["reds"][0]["action"])
        self.assertEqual(doc["checks"], "skipped")

    def test_bad_red_check(self):
        cmds = {k: fake(0) for k in vd.CHECKS}
        cmds["lint"] = fake(1)
        d = self.repo(["proven", "planned"], cmds, prev={"t": ago(hours=1), "proven": 0})
        code, line, doc = self.verdict(d)
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(doc), ["check"])
        self.assertIn("lint red on main (exit 1)", line)
        self.assertFalse(doc["checks"]["lint"]["ok"])

    def test_bad_stale_needs_plan(self):
        fn = {"status": "needs-plan", "attempts": 1, "last_reason": "acceptance already green before change",
              "updated": ago(hours=3)}
        state = {"cycle": 5, "functions": {"F3": fn}}
        prev = {"t": ago(hours=1), "proven": 1, "needs_plan_since": {"F3": 3}}
        d = self.repo(["proven", "proven", "needs-plan"], state=state, prev=prev)
        code, line, doc = self.verdict(d, "--no-checks")
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(doc), ["needs-plan"])
        self.assertIn("F3 waiting in needs-plan 2 cycles", line)
        self.assertIn("--reset F3", line)
        # one cycle in needs-plan is still allowed
        prev["needs_plan_since"] = {"F3": 4}
        d = self.repo(["proven", "proven", "needs-plan"], state=state, prev=prev)
        self.assertEqual(self.verdict(d, "--no-checks")[0], 0)

    def test_good_needs_0_verified_blocking_nos_and_ignores_nits_and_the_trend(self):
        prev = {"t": ago(hours=1), "proven": 3}
        trend = {"t": ago(minutes=5), "event": "open_audit", "detail": "SCORES 6 5 7 4 6 5 6 | ISSUES 19"}
        state = {"polish": {"status": "done", "round": 3, "issues": 0, "nits": 5}}
        code, line, doc = self.verdict(self.repo(["proven"] * 3, state=state, prev=prev, events=[trend]),
                                       "--no-checks")
        self.assertEqual(code, 0, line)
        self.assertTrue(line.startswith("VERDICT GOOD proven 3/3"), line)
        self.assertIn(" issues 0 nits 5 trend SCORES 6 5 7 4 6 5 6 | ISSUES 19 reds: none", line)
        self.assertEqual((doc["nits"], doc["trend"][:6]), (5, "SCORES"))

    def test_a_verified_p1_left_after_polish_is_bad(self):
        state = {"polish": {"status": "done", "round": 3, "issues": 1, "nits": 0}}
        code, line, doc = self.verdict(self.repo(["proven"] * 2, state=state, prev={"proven": 2}), "--no-checks")
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(doc), ["polish"])
        self.assertIn("1 issues (verified P0/P1 checklist NOs)", line)

    def test_probe_floors_gate_only_when_probe_events_exist(self):
        ev = [{"t": ago(minutes=9), "event": "probe_mutation", "module": "mod1", "score": 0.5},
              {"t": ago(minutes=8), "event": "probe_mutation", "module": "mod1", "score": 93},
              {"t": ago(minutes=7), "event": "probe_mutation", "module": "mod2", "score": "85%"},
              {"t": ago(minutes=6), "event": "probe_mutation", "module": "mod3", "score": 0.7, "floor": 0.6}]
        code, line, doc = self.verdict(self.repo(["proven"] * 3, prev={"proven": 3}, events=ev), "--no-checks")
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(doc), ["probe"])  # mod1 latest is 93%, mod3 meets its own floor
        self.assertIn("mutation score 85% < floor 90% in mod2", line)

    def test_all_proven(self):
        d = self.repo(["proven"] * 3, prev={"t": ago(hours=1), "proven": 3})
        code, line, doc = self.verdict(d, "--no-checks")
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith("VERDICT GOOD proven 3/3"), line)

    def test_bad_over_budget_since_previous_verdict(self):
        events = [{"t": ago(hours=2), "fn": "F1", "event": "builder_over_budget", "detail": "70 calls"},
                  {"t": ago(minutes=10), "fn": "F2", "event": "builder_done", "detail": "stopped", "calls": 61}]
        d = self.repo(["proven", "proven"], events=events, prev={"t": ago(hours=1), "proven": 1})
        code, line, doc = self.verdict(d, "--no-checks")
        self.assertEqual(code, 1)
        self.assertEqual(self.kinds(doc), ["budget"])
        self.assertIn("builder F2 over budget", line)
        self.assertNotIn("F1", line)

    def test_reds_capped_at_five(self):
        statuses = ["proven"] + ["needs-plan"] * 7
        prev = {"t": ago(hours=1), "proven": 0, "needs_plan_since": {f"F{i}": 1 for i in range(2, 9)}}
        d = self.repo(statuses, state={"cycle": 4}, prev=prev)
        code, line, doc = self.verdict(d, "--no-checks")
        self.assertEqual((len(doc["reds"]), doc["reds_total"]), (5, 7))
        self.assertTrue(line.endswith("(+2 more)"), line)

    def test_width_red_and_ready_beside_slots(self):
        width = {"ready": 1, "free": 3, "slots": 4, "short_min": 35, "red": True,
                 "unblock": [{"id": "F3", "waiting": 7, "status": "building"}]}
        state = {"cycle": 2, "functions": {}, "width": width}
        d = self.repo(["proven", "planned", "planned"], state=state, prev={"proven": 0})
        code, line, doc = self.verdict(d, "--no-checks")
        self.assertIn("width", self.kinds(doc))
        self.assertIn("(ready 1)", line)
        self.assertIn("width red: ready 1 < free slots 3 for 35 min; unblock most: F3 (7 waiting, building)", line)
        self.assertEqual(doc["width"]["ready"], 1)
        self.assertIn("ready 1, build", vd.expected_text({"expected": 2.0, "slots": 4, "build_min": 11,
                                                          "builds_per_land": 2}, width))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dash.main(["--repo", d]), 0)
        with open(os.path.join(d, "dash", "index.html"), encoding="utf-8") as f:
            self.assertIn("ready, 3 of 4 slots free", f.read())

    def test_dash_renders(self):
        events = [{"t": ago(minutes=30), "fn": "F1", "event": "landed", "detail": "<script>alert(1)</script>",
                   "seconds": 300, "tokens": 1000},
                  {"t": ago(minutes=5), "fn": "F2", "event": "landed", "detail": "ok", "seconds": 200, "tokens": 3000}]
        state = {"cycle": 2, "functions": {"F3": {"status": "building", "attempts": 1, "last_reason": "a < b"}}}
        d = self.repo(["proven", "proven", "planned"], state=state, events=events, prev={"proven": 1})
        self.verdict(d, "--no-checks")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dash.main(["--repo", d]), 0)
        with open(os.path.join(d, "dash", "index.html"), encoding="utf-8") as f:
            page = f.read()
        for want in ("<!doctype html>", "prefers-color-scheme:dark", "mod1", "VERDICT GOOD proven 2/3",
                     "2.0k tokens per proven function", "a &lt; b", "&lt;script&gt;alert(1)&lt;/script&gt;"):
            self.assertIn(want, page)
        self.assertNotIn("<script>", page)
        self.assertNotIn("http", page.replace('http-equiv', ''))  # self-contained: no external fetches
        empty = tempfile.mkdtemp(prefix="dash-empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dash.main(["--repo", empty]), 0)
        self.assertTrue(os.path.isfile(os.path.join(empty, "dash", "index.html")))


if __name__ == "__main__":
    unittest.main()
