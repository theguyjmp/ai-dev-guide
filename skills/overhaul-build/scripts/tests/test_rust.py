"""Rust plans: rust_src, entry_match, anchors, land {stem}, drive.probe_tests and plan_validate on a 2-crate cargo
fixture. Run from the scripts dir:  py -3 -m unittest tests.test_rust

The cargo cases build a toy workspace in a temp dir (root package `shop` with bin `shop` and [[test]] targets, plus
`crates/core` = shop-core) and skip when cargo is not on PATH. No crates are downloaded: the property files define a
tiny local `proptest!` macro, so the plan compiles offline.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import anchors  # noqa: E402
import drive  # noqa: E402
import entry_match  # noqa: E402
import land  # noqa: E402
import plan_validate as pv  # noqa: E402
import rust_src  # noqa: E402

HAVE_CARGO = shutil.which("cargo") is not None
FAST = "exit 0"

ROOT_TOML = """[package]
name = "shop"
version = "0.1.0"
edition = "2021"

[dependencies]
shop-core = { path = "crates/core" }

[workspace]
members = ["crates/core"]
"""
TARGETS = ("tests/acceptance/add.rs", "tests/acceptance/sum.rs", "tests/property/test_f01.rs",
           "tests/property/test_f02.rs")
CORE_TOML = '[package]\nname = "shop-core"\nversion = "0.1.0"\nedition = "2021"\n'
CORE_LIB = """//! Shop core.

/// The sum of two amounts.
pub fn add(a: i64, b: i64) -> i64 {
    let _ = (a, b);
    todo!()
}
"""
MAIN = """//! `shop sum A B` prints A + B.

fn main() {
    let _ = shop_core::add;
    todo!("shop sum A B")
}
"""
ENTRY = """#![allow(dead_code)]
use std::process::{Command, Output};

pub fn shop(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_shop")).args(args).output().expect("run shop")
}
"""
ADD = """use shop_core::add;

#[test]
fn adds_two() {
    assert_eq!(add(2, 3), 5);
}
"""
SUM = """mod entry;

#[test]
fn sums_two() {
    let out = entry::shop(&["sum", "2", "3"]);
    assert!(out.status.success());
    assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "5");
}
"""
PROP_MACRO = """macro_rules! proptest {
    ($(#[test] fn $name:ident($arg:ident in $strat:expr) $body:block)*) => {
        $(#[test] fn $name() { for $arg in $strat { $body } })*
    };
}

"""
PROP_F01 = PROP_MACRO + """proptest! {
    #[test]
    fn add_commutes(a in 0i64..5) {
        assert_eq!(shop_core::add(a, 1), shop_core::add(1, a));
    }
}
"""
PROP_F02 = PROP_MACRO + """proptest! {
    #[test]
    fn sum_prints(a in 0i64..3) {
        let out = std::process::Command::new(env!("CARGO_BIN_EXE_shop"))
            .args(["sum", &a.to_string(), "0"]).output().expect("run shop");
        assert!(out.status.success());
    }
}
"""
ARCH = """# Architecture

## Module core
Paths: crates/core/
Depends on: -

## Module app
Paths: src/
Depends on: core

## Policies
Concurrency: app - single-threaded CLI; one process per command
Persistence: core - none; nothing is stored
Errors: app - usage errors print one line to stderr and exit 2
Untrusted input: app - argv only; a non-number prints usage and exits 2
Retries: app - none; no retry loops
Concurrent writers: -
Multi-store writes: -
Boundaries: -
Domain rules: -
"""
R1 = "| F01 | 1 | core | - | `lib: shop_core::add` | tests/acceptance/add.rs::adds_two | planned | crates/core/src/lib.rs | stub |"
R2 = "| F02 | 1 | app | F01 | `cli: shop sum A B` | tests/acceptance/sum.rs::sums_two | planned | src/main.rs | stub |"
COMMANDS = {
    "test": "cargo test -q --workspace --lib --bins",
    "acceptance": "cargo test -q --test {stem} -- --exact {test}",
    "property": "cargo test -q --test {stem}",
    "lint": "cargo clippy -q --workspace --all-targets -- -D warnings",
    "dup": FAST, "literals": FAST, "shape": FAST, "cycles": FAST, "entry_lint": FAST,
}


def table(*rows):
    return ("# Functions\n\n| id | phase | module | deps | entry | acceptance | status | files | needs |\n"
            "|---|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n")


def cargo_toml(targets=TARGETS):
    return ROOT_TOML + "".join(f'\n[[test]]\nname = "{Path(t).stem}"\npath = "{t}"\n' for t in targets)


def toml(**over):
    cmds = dict(COMMANDS, **over)
    return "[commands]\n" + "".join(f"{k} = '{v}'\n" for k, v in cmds.items() if v is not None)


def write(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


class RustSrcTest(unittest.TestCase):
    def test_test_fns_skip_comments_strings_and_non_test_fns(self):
        toks = rust_src.tokens('// #[test] fn fake() {}\nfn helper() {}\n#[test]\nfn real() { let s = "fn x() {}"; }\n'
                               '#[tokio::test]\nasync fn later() {}\n')
        self.assertEqual({f["name"]: f["test"] for f in rust_src.functions(toks)},
                         {"helper": False, "real": True, "later": True})

    def test_stub_and_proptest_counts_read_tokens(self):
        d = Path(tempfile.mkdtemp(prefix="rs_src_"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        write(d, {"a.rs": "fn f() -> i32 { 1 } // todo!()\n", "b.rs": "fn f() -> i32 { unimplemented!() }\n",
                  "p.rs": PROP_F01 + "#[test]\nfn outside() {}\n"})
        self.assertFalse(pv.rust_stubbed(d / "a.rs"))
        self.assertTrue(pv.rust_stubbed(d / "b.rs"))
        self.assertEqual(pv.proptest_tests(d / "p.rs"), 1)
        self.assertEqual(pv.rust_test_fns(d / "p.rs"), {"add_commutes", "outside"})


class RustPathsTest(unittest.TestCase):
    def test_anchors_and_property_file(self):
        self.assertEqual(anchors.property_file("F01", ".rs"), "tests/property/test_f01.rs")
        self.assertEqual(anchors.property_file("F01"), "tests/property/test_f01.py")
        self.assertTrue(anchors.is_test("tests/acceptance/entry/mod.rs"))
        self.assertTrue(anchors.is_test("tests/acceptance/test_x.py"))
        self.assertFalse(anchors.is_test("tests/acceptance/_entry.py"))
        self.assertFalse(anchors.is_test("src/lib.rs"))

    def test_acceptance_cmds_fill_stem(self):
        cmds = land.acceptance_cmds(COMMANDS["acceptance"], ["tests/acceptance/cart.rs::adds_line"])
        self.assertEqual(cmds, [("tests/acceptance/cart.rs::adds_line",
                                 "cargo test -q --test cart -- --exact adds_line")])
        py = land.acceptance_cmds("pytest -q -k {test}", ["tests/acceptance/test_a.py::test_x"])
        self.assertEqual(py[0][1], "pytest -q -k test_x")

    def test_entry_match_reads_rust_cli_and_lib_tests(self):
        d = Path(tempfile.mkdtemp(prefix="rs_em_"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        write(d, {"tests/acceptance/add.rs": ADD, "tests/acceptance/sum.rs": SUM,
                  "tests/acceptance/entry/mod.rs": ENTRY,
                  "tests/acceptance/bare.rs": SUM.replace("mod entry;", "mod entry {\n    pub fn shop(_: &[&str]) "
                                                          "-> std::process::Output { todo!() }\n}")})
        def row(entry, item):
            return {"line": 3, "id": "F01", "entry": entry, "tests": [(item, item.split("::")[1])]}
        rows = [row("`lib: shop_core::add`", "tests/acceptance/add.rs::adds_two"),
                row("`cli: shop sum A B`", "tests/acceptance/sum.rs::sums_two")]
        self.assertEqual(entry_match.entry_problems(d, rows, "F"), [])
        bad = [row("`cli: shop sum A B`", "tests/acceptance/bare.rs::sums_two"),
               row("`lib: shop_core::sub`", "tests/acceptance/add.rs::adds_two")]
        out = entry_match.entry_problems(d, bad, "F")
        self.assertTrue(any("does not run the built binary" in p for p in out), out)
        self.assertEqual(len(out), 2, out)


class ProbeTestsTest(unittest.TestCase):
    def test_rust_module_gives_cfg_test_files_tests_dirs_and_property_files(self):
        d = Path(tempfile.mkdtemp(prefix="rs_probe_"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        write(d, {"crates/core/src/lib.rs": "pub mod m;\n", "crates/core/src/m.rs": "#[cfg(test)]\nmod tests {}\n",
                  "crates/core/tests/it.rs": "#[test]\nfn t() {}\n", "tests/property/test_f01.rs": PROP_F01,
                  "src/pkg/x.py": "", "tests/unit/py/test_x.py": ""})
        for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"]):
            subprocess.run(["git", *args], cwd=d, check=True, capture_output=True)
        arch = "## Module core\nPaths: crates/core/\n\n## Module py\nPaths: src/pkg/\n"
        fake = SimpleNamespace(arch=arch, main="main", repo=d,
                               order=[SimpleNamespace(id="F01", module="core"), SimpleNamespace(id="F02", module="py")])
        self.assertEqual(drive.Driver.probe_tests(fake, "core"),
                         ["crates/core/src/m.rs", "crates/core/tests", "tests/property/test_f01.rs"])
        self.assertEqual(drive.Driver.probe_tests(fake, "py"), ["tests/unit/py"])


@unittest.skipUnless(HAVE_CARGO, "cargo is not on PATH")
class RustPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.target = tempfile.mkdtemp(prefix="pv_rust_target_")
        cls.old_target = os.environ.get("CARGO_TARGET_DIR")
        os.environ["CARGO_TARGET_DIR"] = cls.target  # one build dir for every fixture: no dependency rebuilds

    @classmethod
    def tearDownClass(cls):
        if cls.old_target is None:
            os.environ.pop("CARGO_TARGET_DIR", None)
        else:
            os.environ["CARGO_TARGET_DIR"] = cls.old_target
        shutil.rmtree(cls.target, ignore_errors=True)

    def make(self, functions=None, toml_text=None, **files):
        root = Path(tempfile.mkdtemp(prefix="pv_rust_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        base = {"Cargo.toml": cargo_toml(), "crates/core/Cargo.toml": CORE_TOML, "crates/core/src/lib.rs": CORE_LIB,
                "src/main.rs": MAIN, "tests/acceptance/entry/mod.rs": ENTRY, "tests/acceptance/add.rs": ADD,
                "tests/acceptance/sum.rs": SUM, "tests/property/test_f01.rs": PROP_F01,
                "tests/property/test_f02.rs": PROP_F02, "spec/ARCHITECTURE.md": ARCH,
                "spec/FUNCTIONS.md": functions or table(R1, R2),
                "overhaul.toml": toml_text or toml(lint=FAST)}
        base.update({k.replace("__", "/").replace("_DOT_", "."): v for k, v in files.items()})
        write(root, base)
        return root

    def problems(self, root, **kw):
        return pv.validate(root, **kw)[0]

    def test_good_plan_is_plan_ok_with_clippy_and_run(self):
        root = self.make(toml_text=toml())
        self.assertEqual(self.problems(root, run=True), [])  # clippy compiles every target; every test is red
        self.assertFalse((root / "tests" / "conftest.py").exists())

    def test_missing_test_fn(self):
        root = self.make(table(R1.replace("::adds_two", "::adds_three"), R2))
        self.assertIn("spec/FUNCTIONS.md:5 F01: tests/acceptance/add.rs has no #[test] fn adds_three",
                      self.problems(root))

    def test_stub_without_todo(self):
        root = self.make(crates__core__src__lib_DOT_rs="/// Sum.\npub fn add(a: i64, b: i64) -> i64 {\n    a + b // todo!()\n}\n")
        self.assertIn("spec/FUNCTIONS.md:5 F01: no stub in crates/core/src/lib.rs: no body calls todo!() or "
                      "unimplemented!()", self.problems(root))

    def test_clippy_without_deny_warnings_or_all_targets(self):
        root = self.make(toml_text=toml(lint="cargo clippy -q --workspace"))
        out = self.problems(root)
        self.assertIn("overhaul.toml: [commands].lint (clippy) does not deny warnings: end it with `-- -D warnings`", out)
        self.assertIn("overhaul.toml: [commands].lint (clippy) does not check the tests: add --all-targets", out)

    def test_acceptance_that_does_not_run_the_binary(self):
        root = self.make(tests__acceptance__sum_DOT_rs=SUM.replace("mod entry;\n", "").replace("entry::shop", "shop")
                         + "\nfn shop(_: &[&str]) -> std::process::Output {\n    todo!()\n}\n")
        out = self.problems(root)
        self.assertTrue(any(p.startswith("spec/FUNCTIONS.md:6: F02") and "does not run the built binary" in p
                            for p in out), out)

    def test_acceptance_file_not_a_test_target(self):
        root = self.make(Cargo_DOT_toml=cargo_toml(TARGETS[1:]))
        self.assertIn('spec/FUNCTIONS.md:5 F01: tests/acceptance/add.rs is not a cargo test target: add [[test]] '
                      'name = "add" path = "tests/acceptance/add.rs" to the root Cargo.toml', self.problems(root))

    def test_property_file_missing_or_without_proptest(self):
        root = self.make(tests__property__test_f02_DOT_rs="#[test]\nfn plain() {}\n")
        out = self.problems(root)
        self.assertIn("spec/FUNCTIONS.md:6 F02: tests/property/test_f02.rs has 0 #[test] fns inside proptest!, "
                      "want 1 to 3", out)
        (root / "tests/property/test_f01.rs").unlink()
        self.assertIn("spec/FUNCTIONS.md:5 F01: no property tests: write tests/property/test_f01.rs with 1 to 3 "
                      "#[test] fns inside proptest!", self.problems(root))


if __name__ == "__main__":
    unittest.main()
