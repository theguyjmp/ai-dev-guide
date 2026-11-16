"""drop_stale_fingerprints removes a cargo fingerprint whose library output was pruned and keeps every other one.
Temp dirs only.

  py -3 tests/test_fingerprints.py
"""
from __future__ import annotations

import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import alarm_handlers as H  # noqa: E402


class Fingerprints(unittest.TestCase):
    def test_only_lib_fingerprints_without_output_go(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fp, deps = root / "release" / ".fingerprint", root / "release" / "deps"
            deps.mkdir(parents=True)
            for name, out, kind in (("sha2-aaaa", None, "lib-sha2"),
                                    ("miniz-oxide-bbbb", "libminiz_oxide-bbbb.rlib", "lib-miniz_oxide"),
                                    ("serde-derive-cccc", "serde_derive-cccc.dll", "lib-serde_derive"),
                                    ("app-dddd", None, "test-integration-test-x"),
                                    ("ring-eeee", None, "run-build-script-build")):
                (fp / name).mkdir(parents=True)
                (fp / name / kind).write_text("x")
                if out:
                    (deps / out).write_text("x")
            self.assertEqual(H.drop_stale_fingerprints(root), 1)
            self.assertEqual(sorted(p.name for p in fp.iterdir()),
                             ["app-dddd", "miniz-oxide-bbbb", "ring-eeee", "serde-derive-cccc"])
            self.assertEqual(H.drop_stale_fingerprints(root), 0)


if __name__ == "__main__":
    unittest.main()
