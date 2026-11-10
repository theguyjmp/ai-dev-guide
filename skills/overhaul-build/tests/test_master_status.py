"""master-status.py never suggests launching a held row or a later-phase build packet behind the Phase 0 gate.

Run: py -3 -m unittest discover -s skills/overhaul-build/tests
"""
import importlib.util, json, os, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("master_status", os.path.join(HERE, "..", "scripts", "master-status.py"))
ms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ms)

DISPATCH = """# DISPATCH
| id | description | model | effort | brief | follows | status |
|---|---|---|---|---|---|---|
| m0-gate | gate bot | opus | medium | FLEET/briefs/m0-gate.md | - | {m0} |
| m1-c38-comments | build comments | opus | medium | FLEET/briefs/m1-c38-comments.md | - | {build} |
| m1-c38-comments-accept | Acceptance specs for comments | opus | medium | FLEET/briefs/m1-c38-comments-accept.md | - | ready |
| m1-c02-undo | build undo | opus | medium | FLEET/briefs/m1-c02-undo.md | - | held: Phase 0 gate (start gate) |
| m0-fu-9 | follow-up fix | opus | medium | FLEET/briefs/m0-fu-9.md | - | ready |
"""

LAUNCH = """# LAUNCH-13
m1-c38-comments | opus | medium | FLEET/briefs/m1-c38-comments.md | main | x
m1-c02-undo | opus | medium | FLEET/briefs/m1-c02-undo.md | main | x
m1-c38-comments-accept | opus | medium | FLEET/briefs/m1-c38-comments-accept.md | main | x
m0-fu-9 | opus | medium | FLEET/briefs/m0-fu-9.md | main | x
"""


class LaunchGate(unittest.TestCase):
    def fleet(self, m0="running", build="ready", phase=None):
        d = tempfile.mkdtemp()
        tr = os.path.join(d, "transcripts")
        os.makedirs(tr)
        with open(os.path.join(d, "BUDGET.json"), "w") as f:
            json.dump({"repo": d, "transcripts": tr}, f)
        with open(os.path.join(d, "FLEET-TARGET"), "w") as f:
            f.write("20")
        with open(os.path.join(d, "DISPATCH.md"), "w") as f:
            f.write(DISPATCH.format(m0=m0, build=build))
        with open(os.path.join(d, "LAUNCH-13.md"), "w") as f:
            f.write(LAUNCH)
        if phase is not None:
            with open(os.path.join(d, "PHASE"), "w") as f:
                f.write(phase)
        return d

    def test_unproven_phase0_holds_later_phase_builds(self):
        s = ms.status(self.fleet(), check_scheduler=False)
        self.assertFalse(s["phase0_proven"])
        self.assertEqual(sorted(s["waiting"]), ["m0-fu-9.md", "m1-c38-comments-accept.md"])
        self.assertEqual(s["held"], 2)
        self.assertNotIn("m1-c38-comments.md", s["ready"])
        self.assertIn("m1-c38-comments-accept.md", s["ready"])
        text = "\n".join(ms.lines(s, "", mark=False))
        self.assertNotIn("m1-c38-comments.md", text.split("HELD")[0])
        self.assertNotIn("m1-c02-undo", text)

    def test_held_status_never_launchable_even_when_proven(self):
        s = ms.status(self.fleet(m0="landed", phase="1 phase0_proven"), check_scheduler=False)
        self.assertTrue(s["phase0_proven"])
        self.assertIn("m1-c38-comments.md", s["waiting"])
        self.assertNotIn("m1-c02-undo.md", s["waiting"])
        self.assertEqual(s["held"], 1)

    def test_derived_gate_when_no_phase_file(self):
        d = self.fleet(m0="landed")
        s = ms.status(d, check_scheduler=False)
        self.assertFalse(s["phase0_proven"])  # m0-fu-9 is still an open phase-0 row
        with open(os.path.join(d, "DISPATCH.md"), "w") as f:
            f.write(DISPATCH.format(m0="landed", build="ready").replace("FLEET/briefs/m0-fu-9.md | - | ready", "FLEET/briefs/m0-fu-9.md | - | merged"))
        s = ms.status(d, check_scheduler=False)
        self.assertTrue(s["phase0_proven"])
        self.assertIn("m1-c38-comments.md", s["ready"])

    def test_only_held_lines_are_not_launchable_work(self):
        d = self.fleet(build="held")
        with open(os.path.join(d, "LAUNCH-13.md"), "w") as f:
            f.write("\n".join(LAUNCH.splitlines()[:3]))
        with open(os.path.join(d, "DISPATCH.md"), "w") as f:
            f.write("\n".join(l for l in DISPATCH.format(m0="running", build="held").splitlines()
                              if "| ready" not in l))
        s = ms.status(d, check_scheduler=False)
        self.assertEqual(s["waiting"], [])
        self.assertEqual(s["ready"], [])
        self.assertFalse(s["launchable"])


if __name__ == "__main__":
    unittest.main()
