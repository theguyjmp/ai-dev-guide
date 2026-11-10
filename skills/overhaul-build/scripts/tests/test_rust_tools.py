"""Rust paths of entry_lint.py, shape_check.py and dup_blocks.py on text fixtures in temp dirs (no cargo needed)."""
import os
import re
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(SCRIPTS, "entry_lint.py")
SHAPE = os.path.join(SCRIPTS, "shape_check.py")
DUP = os.path.join(SCRIPTS, "dup_blocks.py")
LINE = re.compile(r"^[\w./\-]+:\d+: \S.*$")
CARGO_ROOT = '[package]\nname = "shop"\nversion = "0.1.0"\n\n[workspace]\nmembers = ["crates/*"]\n'
CARGO_CORE = '[package]\nname = "shop-core"\nversion = "0.1.0"\n'
GOOD_TEST = ('mod entry;\nuse std::process::Command;\n\n#[test]\nfn cart_add() {\n'
             '    let out = Command::new(env!("CARGO_BIN_EXE_shop")).args(["cart", "add"]).output().unwrap();\n'
             '    assert!(out.status.success());\n}\n')
FUNCTIONS = ("| id | phase | module | deps | entry | acceptance | status | files | needs |\n"
             "|---|---|---|---|---|---|---|---|---|\n"
             "| F1 | 1 | core | - | `lib: shop_core::cart::total` | `tests/acceptance/total.rs::total_sums` | planned"
             " | crates/core/src/cart.rs | - |\n"
             "| F2 | 1 | cli | F1 | `cli: shop cart add` | `tests/acceptance/cart.rs::cart_add` | planned"
             " | src/main.rs | - |\n")


def body(n, var="x"):
    """n distinct statement lines."""
    return "".join(f"    let {var}{i} = {i} + 1;\n" for i in range(n))


class RustCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name

    def write(self, rel, text=""):
        path = os.path.join(self.repo, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def run_script(self, script, *args):
        proc = subprocess.run([sys.executable, script, "--repo", self.repo, *args], capture_output=True,
                              text=True, encoding="utf-8", timeout=120)
        return proc.returncode, proc.stdout + proc.stderr

    def assert_fails(self, script, *args):
        code, out = self.run_script(script, *args)
        self.assertEqual(code, 1, out)
        lines = out.strip().splitlines()
        self.assertTrue(lines[-1].endswith("problems"), out)
        for line in lines[:-1]:
            self.assertRegex(line, LINE)
        return out

    def assert_clean(self, script, *args):
        code, out = self.run_script(script, *args)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        return out


class EntryLintRustTest(RustCase):
    def setUp(self):
        super().setUp()
        self.write("Cargo.toml", CARGO_ROOT)
        self.write("crates/core/Cargo.toml", CARGO_CORE)
        self.write("spec/FUNCTIONS.md", FUNCTIONS)
        self.write("tests/acceptance/entry/mod.rs", "#![allow(dead_code)]\npub fn bin() -> &'static str {\n"
                                                    '    env!("CARGO_BIN_EXE_shop")\n}\n')

    def test_binary_driving_test_is_clean(self):
        self.write("tests/acceptance/cart.rs", GOOD_TEST)
        self.assertIn("ENTRY LINT OK 2 files", self.assert_clean(ENTRY))

    def test_mock_crates_banned(self):
        self.write("tests/acceptance/cart.rs", GOOD_TEST + "use mockall::predicate::*;\n\n"
                   "#[cfg_attr(test, automock)]\ntrait Store {}\n// wiremock in a comment is fine\n")
        out = self.assert_fails(ENTRY)
        self.assertIn("tests/acceptance/cart.rs:9: `mockall` is banned", out)
        self.assertIn("tests/acceptance/cart.rs:11: `automock` is banned", out)
        self.assertNotIn("wiremock", out)

    def test_path_attr_and_include_banned(self):
        self.write("tests/acceptance/cart.rs", '#[path = "../../src/cart.rs"]\nmod cart;\n'
                   'include!("../../src/money.rs");\n' + GOOD_TEST)
        out = self.assert_fails(ENTRY)
        self.assertIn("tests/acceptance/cart.rs:1: `#[path = ..]`", out)
        self.assertIn("tests/acceptance/cart.rs:3: `include!`", out)

    def test_product_crate_banned(self):
        self.write("tests/acceptance/cart.rs", "use shop_core::cart;\nextern crate shop;\n" + GOOD_TEST
                   + "fn helper() -> u32 {\n    ::shop_core::cart::total(&[])\n}\n")
        out = self.assert_fails(ENTRY)
        self.assertIn("tests/acceptance/cart.rs:1: uses product crate `shop_core`", out)
        self.assertIn("tests/acceptance/cart.rs:2: uses product crate `shop`", out)
        self.assertIn("tests/acceptance/cart.rs:12: uses product crate `shop_core`", out)

    def test_lib_row_file_may_use_its_crate_only(self):
        self.write("tests/acceptance/total.rs", "use shop_core::cart::total;\n\n#[test]\nfn total_sums() {\n"
                   "    assert_eq!(total(&[1, 2]), 3);\n}\n")
        self.assert_clean(ENTRY)
        self.write("tests/acceptance/total.rs", "use shop_core::cart::total;\nuse shop::cli;\n")
        out = self.assert_fails(ENTRY)
        self.assertIn("total.rs:2: uses product crate `shop`", out)
        self.assertNotIn("shop_core", out)

    def test_python_files_still_linted_beside_rust(self):
        self.write("tests/acceptance/cart.rs", GOOD_TEST)
        self.write("tests/acceptance/test_x.py", "from unittest import mock\n")
        self.assertIn("test_x.py:1: imports `unittest`", self.assert_fails(ENTRY))


class ShapeCheckRustTest(RustCase):
    def test_long_fn_fails_outside_tests(self):
        self.write("src/cart.rs", f"pub fn long() -> u32 {{\n{body(30)}    0\n}}\n")
        self.assertIn("src/cart.rs:1: function long is 33 lines, over 30", self.assert_fails(SHAPE, "src"))
        self.assert_clean(SHAPE, "src", "--max-lines", "40")

    def test_test_code_is_not_measured(self):
        long_body = body(40)
        self.write("src/cart.rs", "pub fn ok() {}\n\n#[cfg(test)]\nmod tests {\n    #[test]\n"
                   f"    fn long_case() {{\n{long_body}    }}\n    fn helper() {{\n{long_body}    }}\n}}\n")
        self.write("src/cart/tests.rs", f"fn helper() {{\n{long_body}}}\n")
        self.write("tests/acceptance/cart.rs", f"#[test]\nfn long() {{\n{long_body}}}\n")
        self.assertIn("SHAPE OK", self.assert_clean(SHAPE, "src", "tests"))

    def test_late_const_fails_unless_it_names_a_fn(self):
        self.write("src/cart.rs", 'const TOP: u32 = 1;\n\nfn add() {}\nfn sub<const N: usize>() {}\n\n'
                   'pub(crate) static TABLE: &[fn()] = &[add, sub::<1>];\nconst LATE: u32 = 2;\n'
                   'static mut COUNT: u32 = 0;\n\nimpl Cart {\n    const INNER: u32 = 3;\n}\n')
        out = self.assert_fails(SHAPE, "src")
        self.assertIn("src/cart.rs:7: const LATE is defined after the first fn or impl", out)
        self.assertIn("src/cart.rs:8: static COUNT is defined after the first fn or impl", out)
        for name in ("TOP", "TABLE", "INNER", " N "):
            self.assertNotIn(name, out)

    def test_const_after_impl_fails(self):
        self.write("src/a.rs", "struct A;\nimpl A {}\nconst LATE: u8 = 1;\nconst fn f() -> u8 { 1 }\n")
        out = self.assert_fails(SHAPE, "src")
        self.assertIn("src/a.rs:3: const LATE", out)
        self.assertNotIn("const f", out)


class DupBlocksTest(RustCase):
    def test_block_repeated_in_two_files_fails_once(self):
        block = body(7)
        self.write("src/a.rs", f"pub fn a() {{\n{block}}}\n")
        self.write("crates/core/src/b.rs", f"// copied\npub fn b() {{\n{block.replace(' + ', '+')}}}\n")
        out = self.assert_fails(DUP, "crates", "src")
        self.assertEqual(len(out.strip().splitlines()), 2, out)
        self.assertIn("crates/core/src/b.rs:3: 7 lines repeat at src/a.rs:2", out)

    def test_five_lines_trivial_lines_and_imports_pass(self):
        uses = "use std::collections::{\n    HashMap,\n    HashSet,\n    BTreeMap,\n    BTreeSet,\n    VecDeque,\n};\n"
        self.write("src/a.rs", f"{uses}pub fn a() {{\n{body(5)}    }}\n    }}\n}}\n")
        self.write("src/b.rs", f"{uses}pub fn b() {{\n{body(5)}    }}\n    }}\n}}\n")
        self.assertIn("DUP BLOCKS OK 2 files", self.assert_clean(DUP))
        self.assert_fails(DUP, "--min", "5")

    def test_test_code_and_python_ignored(self):
        block = body(8)
        self.write("src/a.rs", f"pub fn a() {{}}\n\n#[cfg(test)]\nmod tests {{\n    fn t() {{\n{block}    }}\n}}\n")
        self.write("src/b_test.rs", f"fn t() {{\n{block}}}\n")
        self.write("tests/c.rs", f"fn t() {{\n{block}}}\n")
        self.write("src/d.py", "x = 1\n" * 20)
        self.write("src/e.rs", f"fn t() {{\n{block}}}\n")
        self.assert_clean(DUP, "src", "tests")

    def test_same_file_copy_fails_but_overlap_does_not(self):
        self.write("src/a.rs", "fn a() {\n" + "    n += 1;\n" * 7 + "}\n")
        self.assert_clean(DUP)
        self.write("src/a.rs", f"fn a() {{\n{body(6)}}}\nfn b() {{\n{body(6)}}}\n")
        self.assertIn("src/a.rs:2: 6 lines repeat at src/a.rs:10", self.assert_fails(DUP))


if __name__ == "__main__":
    unittest.main()
