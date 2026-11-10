"""mutate.py Rust backend: cargo-mutants on a tiny crate made in a temp dir (skipped without cargo-mutants)."""
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
import mutate  # noqa: E402

LIB = """/// Pass at 50 or more.
pub fn passes(score: u32) -> bool {
    // a comment with < and + "inside" is not mutated
    let _s = "x < y";
    score >= 50
}

pub fn double(x: u32) -> u32 {
    x * 2
}
"""
TESTS = """#[test]
fn passes_both_sides() { assert!(fx::passes(80)); assert!(!fx::passes(10)); }
#[test]
fn doubles() { assert_eq!(fx::double(2), 4); }
"""
RED = """#[test]
fn prop_boundary() { panic!("Test failed: boundary; minimal failing input: score = 49"); }
"""


def has_cargo_mutants() -> bool:
    if not shutil.which("cargo"):
        return False
    try:
        return subprocess.run(["cargo", "mutants", "--version"], capture_output=True, check=False,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def crate(root: Path, red: bool = False) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text('[package]\nname = "fx"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
    (root / "src" / "lib.rs").write_text(LIB, encoding="utf-8")
    (root / "tests" / "unit_fx.rs").write_text(TESTS + (RED if red else ""), encoding="utf-8")
    return root


class RustSelection(unittest.TestCase):
    """Backend choice and test-target mapping: pure Python, no cargo needed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mutrs_"))
        self.addCleanup(shutil.rmtree, self.root, True)
        crate(self.root)

    def test_rust_paths_pick_the_rust_backend_and_python_paths_do_not(self):
        (self.root / "py").mkdir()
        (self.root / "py" / "m.py").write_text("x = 1\n", encoding="utf-8")
        self.assertTrue(mutate.is_rust(self.root, ["src/"]))
        self.assertTrue(mutate.is_rust(self.root, ["src/lib.rs"]))
        self.assertFalse(mutate.is_rust(self.root, ["py"]))
        self.assertFalse(mutate.is_rust(self.root, ["src", "py/m.py"]))
        self.assertEqual(mutate.rs_files(self.root, ["."]), ["src/lib.rs"])

    def test_tests_map_to_cargo_targets_lib_and_filters(self):
        (self.root / "tests" / "unit").mkdir()
        (self.root / "tests" / "unit" / "core.rs").write_text("", encoding="utf-8")
        with open(self.root / "Cargo.toml", "a", encoding="utf-8") as f:
            f.write('\n[[test]]\nname = "unit_core"\npath = "tests/unit/core.rs"\n')
        opts, filters = mutate.rust_test_args(self.root, ["tests/unit/", "tests/unit_fx.rs", "src/lib.rs", "boundary"])
        self.assertEqual(opts, ["--test=unit_core", "--test=unit_fx", "--lib"])
        self.assertEqual(filters, ["boundary"])

    def test_exact_regex_escapes_metacharacters(self):
        self.assertEqual(mutate.re_exact("a.rs:9:7: replace * with + in f"), r"^a\.rs:9:7: replace \* with \+ in f$")


@unittest.skipUnless(has_cargo_mutants(), "cargo or cargo-mutants missing")
class RustMutants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="mutrs_"))
        patcher = mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": str(cls.tmp / "target")})
        patcher.start()
        cls.addClassCleanup(patcher.stop)
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.green = crate(cls.tmp / "green")
        cls.red = crate(cls.tmp / "red", red=True)

    def test_score_and_a_survivor_file_line(self):
        res = mutate.mutate_rust(self.green, ["src"], ["tests/unit_fx.rs"])
        self.assertEqual(res["baseline"], "green")
        self.assertEqual((res["sites"], res["mutants"], res["killed"], res["survived"]), (7, 7, 6, 1))
        self.assertEqual(res["score"], round(6 / 7, 4))
        s = res["survivors"][0]
        self.assertEqual((s["file"], s["line"], s["before"], s["after"]), ("src/lib.rs", 9, "*", "+"))

    def test_sampling_is_seeded_and_capped(self):
        a = mutate.mutate_rust(self.green, ["src/lib.rs"], ["tests/unit_fx.rs"], max_mutants=3, seed=1)
        self.assertEqual((a["sites"], a["mutants"]), (7, 3))

    def test_red_baseline_reads_proptest_counterexample(self):
        res = mutate.mutate_rust(self.red, ["src"], ["tests/unit_fx.rs"])
        self.assertEqual((res["baseline"], res["score"], res["survivors"]), ("red", 0.0, []))
        self.assertTrue(any("minimal failing input: score = 49" in c for c in res["counterexamples"]),
                        res["counterexamples"])

    def test_cli_prints_the_same_json_object(self):
        out = subprocess.run([sys.executable, str(Path(mutate.__file__)), "--repo", str(self.green), "--paths",
                              "src/", "--tests", "tests/unit_fx.rs", "--max-mutants", "2"], capture_output=True,
                             text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        res = json.loads(out.stdout.strip().splitlines()[-1])
        for key in ("score", "mutants", "killed", "survived", "timeouts", "sites", "files", "baseline", "survivors",
                    "counterexamples"):
            self.assertIn(key, res)
        self.assertEqual(res["mutants"], 2)


if __name__ == "__main__":
    unittest.main()
