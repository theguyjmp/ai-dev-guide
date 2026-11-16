"""Stack-profile fixes: node unit_one, HARNESS_SPEC_REF, cloud_status, builder wording from the profile, {changed} in qa,
copy_ignored assets, no console windows. Temp folders only."""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="fleet-profile-"))
os.environ.update({"HARNESS_HOME": str(TMP / "home"), "HARNESS_REPO": str(TMP / "repo"), "HARNESS_WT": str(TMP / "wt")})
sys.path.insert(0, str(HERE))
import beat_alarms  # noqa: E402
import harness as h  # noqa: E402

JS = HERE.parent / "workflows" / "builders.js"


class UnitOne(unittest.TestCase):
    node_one = h.STACKS["node"]["unit_one"]

    def test_a_node_unit_with_a_glob_keeps_the_glob_in_the_rerun(self):
        r = h.derive_unit_one('node --test --test-reporter=tap "tests/**/*.test.js"', self.node_one)
        self.assertEqual(r, 'node --test --test-reporter=tap "tests/**/*.test.js" --test-name-pattern="^{name}$"')

    def test_a_unit_that_is_not_node_test_has_no_rerun(self):
        self.assertEqual(h.derive_unit_one("npm test --silent", self.node_one), "")


class SpecRef(unittest.TestCase):
    def test_main_spec_reads_the_ref_from_the_env(self):
        calls = []
        with mock.patch.object(h, "git", lambda *a, **k: calls.append(a) or mock.Mock(stdout="")), \
                mock.patch.object(h, "parse_spec", lambda t: {}), mock.patch.object(h, "SPEC_REF", "plan/x"):
            h.main_spec()
        self.assertEqual(calls, [("show", f"plan/x:{h.SPEC}")])
        self.assertEqual(h.SPEC_REF, os.environ.get("HARNESS_SPEC_REF", "main"))


class CloudStatus(unittest.TestCase):
    def run_with(self, out=None, exc=None):
        def run(cmd, **k):
            if exc:
                raise exc
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return beat_alarms.cloud_status(run)

    def test_json_object_passes_through(self):
        self.assertEqual(self.run_with('noise\n{"sessions": 3}\n'), {"sessions": 3})

    def test_missing_text_or_non_object_output_is_empty(self):
        for out in ("", "no cloud here\n", "[1, 2]\n", "null\n"):
            self.assertEqual(self.run_with(out), {}, out)
        self.assertEqual(self.run_with(exc=OSError("no python")), {})


class BuilderWording(unittest.TestCase):
    def test_builders_js_names_no_stack_command(self):
        text = JS.read_text(encoding="utf-8")
        self.assertFalse(re.search(r"cargo|clippy|Rust", text), "builders.js must take stack wording from the claim JSON")

    def test_every_stack_profile_carries_the_wording_keys(self):
        for name, p in h.STACKS.items():
            for k in ("stack_label", "env_use", "done_brief", "tail_brief", "copy_ignored"):
                self.assertIn(k, p, f"{name}.{k}")
        self.assertIn("node --test", h.STACKS["node"]["done_brief"])
        self.assertIn("cargo test", h.STACKS["rust"]["done_brief"])


class QaChanged(unittest.TestCase):
    def test_changed_expands_to_the_batch_files_and_skips_when_none(self):
        self.assertEqual(h.expand("py -3 pincount.py {changed}", ["a.js", "b.js"]), ['py -3 pincount.py "a.js" "b.js"'])
        self.assertEqual(h.expand("py -3 pincount.py {changed}", []), [])


class CopyIgnored(unittest.TestCase):
    def test_copies_matching_ignored_files_not_junctions_and_skips_current_ones(self):
        src, dst = TMP / "src", TMP / "dst"
        (src / "assets").mkdir(parents=True)
        (src / "assets" / "a.glb").write_bytes(b"glb")
        (src / "node_modules").mkdir()
        (src / "node_modules" / "x.glb").write_bytes(b"no")
        dst.mkdir()
        self.assertEqual(h.copy_assets(dst, src, ["**/*.glb"]), 1)
        self.assertEqual((dst / "assets" / "a.glb").read_bytes(), b"glb")
        self.assertFalse((dst / "node_modules").exists())
        self.assertFalse((dst / "assets").is_symlink())
        self.assertEqual(h.copy_assets(dst, src, ["**/*.glb"]), 0)

    def test_no_globs_copies_nothing(self):
        self.assertEqual(h.copy_assets(TMP / "dst2", TMP / "src", []), 0)


class NoWindow(unittest.TestCase):
    def test_sh_passes_the_no_window_flag(self):
        with mock.patch.object(h.subprocess, "Popen") as P:
            P.return_value.communicate.return_value = ("", "")
            P.return_value.returncode = 0
            h.sh(["git", "--version"])
        self.assertEqual(P.call_args.kwargs["creationflags"], h.NO_WINDOW)

    def test_reexec_uses_the_project_wrapper_when_present(self):
        wrap = Path(h.__file__).with_name("run_train.py")
        self.assertFalse(wrap.exists())
        self.assertEqual(h.reexec_argv()[-3:], [h.__file__, "train", "--loop"][-3:])
        with mock.patch.object(Path, "exists", lambda self: self.name == "run_train.py"):
            argv = h.reexec_argv()
        self.assertTrue(argv[2].endswith("run_train.py") and argv[-2:] == ["train", "--loop"])


if __name__ == "__main__":
    unittest.main()
