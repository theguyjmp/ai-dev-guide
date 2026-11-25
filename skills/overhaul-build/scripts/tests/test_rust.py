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
import rust_test_groups  # noqa: E402
import rust_tests  # noqa: E402

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
- `pub fn add(a: i64, b: i64) -> i64` [F01]
  Packet: edit crates/core/src/lib.rs after `mod tests`; calls -; test cargo test --test add

## Module app
Paths: src/
Depends on: core
- `fn sum(args: &[String]) -> i64` [F02]
  Packet: edit src/main.rs after `fn main`; calls shop_core::add; test cargo test --test sum

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


GROUP_ROOT = '//! Acceptance tests: one target, one module per test file.\n\nmod entry;\n\nmod sum;\nmod sub;\n'
GROUPED_SUM = SUM.replace("mod entry;", "use crate::entry;")
GROUPED_TOML = ROOT_TOML + ('\n[[test]]\nname = "acceptance"\npath = "tests/acceptance/group.rs"\n'
                            '\n[[test]]\nname = "add"\npath = "tests/acceptance/add.rs"\n')


class GroupedTargetsTest(unittest.TestCase):
    """rust_tests (a test file is its own [[test]] or a module of a group root) and the built-binary check through
    a group root's helper module."""

    def make(self, **files):
        d = Path(tempfile.mkdtemp(prefix="rs_group_"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        base = {"Cargo.toml": GROUPED_TOML, "tests/acceptance/group.rs": GROUP_ROOT,
                "tests/acceptance/entry/mod.rs": ENTRY, "tests/acceptance/sum.rs": GROUPED_SUM,
                "tests/acceptance/sub.rs": "#[test]\nfn subs() {}\n", "tests/acceptance/add.rs": ADD}
        base.update({k.replace("__", "/").replace("_DOT_", "."): v for k, v in files.items()})
        write(d, base)
        return d

    def test_targets_map_both_layouts(self):
        table = rust_tests.targets(self.make())
        self.assertEqual(table["tests/acceptance/add.rs"], rust_tests.Target("add"))
        self.assertEqual(table["tests/acceptance/group.rs"], rust_tests.Target("acceptance"))
        self.assertEqual(table["tests/acceptance/sum.rs"], rust_tests.Target("acceptance", "sum"))
        self.assertNotIn("tests/acceptance/entry/mod.rs", table)  # a helper directory module is no test file
        self.assertEqual(rust_tests.root_of(table, "tests/acceptance/sum.rs"), "tests/acceptance/group.rs")
        self.assertEqual(rust_tests.root_of(table, "tests/acceptance/add.rs"), "")
        self.assertEqual(rust_tests.root_of(table, "tests/acceptance/none.rs"), "")

    def test_target_names_and_filters(self):
        own, mod = rust_tests.Target("add"), rust_tests.Target("acceptance", "sum")
        self.assertEqual((own.test("adds_two"), own.filter, own.fn("adds_two")), ("adds_two", "", "adds_two"))
        self.assertEqual((mod.test("sums_two"), mod.filter, mod.fn("sum::sums_two")),
                         ("sum::sums_two", "sum::", "sums_two"))
        self.assertEqual(rust_tests.target_of({}, "./tests/acceptance/x.rs"), rust_tests.Target("x"))

    def test_problem_names_the_group_root_or_the_missing_target(self):
        table = rust_tests.targets(self.make())
        self.assertEqual(rust_tests.problem("tests/acceptance/sum.rs", table), "")
        self.assertEqual(rust_tests.problem("tests/acceptance/mul.rs", table),
                         "tests/acceptance/mul.rs is not a cargo test module: declare `mod mul;` in its area's "
                         "group root, or (a derived root) start its name with the root's area "
                         "(tests/acceptance/group.rs)")
        self.assertEqual(rust_tests.problem("tests/other/mul.rs", table),
                         'tests/other/mul.rs is not a cargo test target: add [[test]] name = "mul" '
                         'path = "tests/other/mul.rs" to the root Cargo.toml')

    def test_crate_uses(self):
        toks = rust_src.tokens('use crate::entry;\nuse crate::{fixtures::load, entry};\nuse std::fs;\n'
                               'let s = "use crate::fake";\n')
        self.assertEqual(rust_src.crate_uses(toks), ["entry", "fixtures"])

    def check(self, d):
        row = {"line": 3, "id": "F02", "entry": "`cli: shop sum A B`",
               "tests": [("tests/acceptance/sum.rs::sums_two", "sums_two")]}
        return entry_match.entry_problems(d, [row], "F")

    def test_grouped_file_runs_the_binary_through_its_group_roots_helper(self):
        self.assertEqual(self.check(self.make()), [])

    def test_grouped_binary_check_stays_strict(self):
        cases = {"the root does not declare the helper": {"tests__acceptance__group_DOT_rs": GROUP_ROOT.replace(
                     "mod entry;\n", "")},
                 "the file does not use the helper": {"tests__acceptance__sum_DOT_rs": GROUPED_SUM.replace(
                     "use crate::entry;", "use crate::sub;")},
                 "the helper does not run the binary": {"tests__acceptance__entry__mod_DOT_rs": ENTRY.replace(
                     'env!("CARGO_BIN_EXE_shop")', '"shop"')},
                 "the file is no module of the root": {"tests__acceptance__group_DOT_rs": GROUP_ROOT.replace(
                     "mod sum;\n", "")}}
        for what, files in cases.items():
            with self.subTest(what):
                out = self.check(self.make(**files))
                self.assertTrue(any("does not run the built binary" in p for p in out), out)


class RustTestGroupsTest(unittest.TestCase):
    """rust_test_groups: a per-file [[test]] target (or an untargeted test file) of a grouped folder moves into its
    area's group root; all or nothing; idempotent; line endings kept."""

    TOML = ROOT_TOML + ('\n[[test]]\nname = "acceptance_a"\npath = "tests/acceptance/group_a.rs"\n'
                        '\n[[test]]\nname = "acceptance_b1"\npath = "tests/acceptance/group_b1.rs"\n'
                        '\n[[test]]\nname = "property_a"\npath = "tests/property/group_a.rs"\n')
    ROOT_A = "//! a\n\nmod entry;\n\nmod a01_one;\nmod a03_three;\n"
    PROP_A = '//! a\n\n#[path = "../acceptance/entry/mod.rs"]\nmod entry;\n\nmod test_a01;\n'
    USES = "//! doc\n\nmod entry;\n\n#[test]\nfn runs() {\n    entry::shop(&[]);\n}\n"
    UNUSED = "//! doc\n\nmod entry;\n\n#[test]\nfn plain() {}\n"

    def make(self, toml_extra="", **files):
        d = Path(tempfile.mkdtemp(prefix="rs_groups_"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        base = {"Cargo.toml": self.TOML + toml_extra, "tests/acceptance/entry/mod.rs": ENTRY,
                "tests/acceptance/group_a.rs": self.ROOT_A, "tests/acceptance/group_b1.rs": "mod entry;\n\nmod b101_x;\n",
                "tests/acceptance/a01_one.rs": "use crate::entry;\n", "tests/acceptance/a03_three.rs": "\n",
                "tests/acceptance/b101_x.rs": "\n", "tests/property/group_a.rs": self.PROP_A,
                "tests/property/test_a01.rs": "\n"}
        base.update({k.replace("__", "/").replace("_DOT_", "."): v for k, v in files.items()})
        for rel, text in base.items():  # bytes: LF on every platform, so the line-ending checks mean something
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_bytes(text.encode("utf-8"))
        return d

    @staticmethod
    def own(rel):
        return f'\n[[test]]\nname = "{Path(rel).stem}"\npath = "{rel}"\n'

    def snapshot(self, d):
        return {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}

    def test_moves_into_the_longest_matching_group_and_is_idempotent(self):
        d = self.make(self.own("tests/acceptance/a02_two.rs") + self.own("tests/acceptance/b102_y.rs"),
                      tests__acceptance__a02_two_DOT_rs=self.USES, tests__acceptance__b102_y_DOT_rs=self.UNUSED)
        res = rust_test_groups.convert(d)
        self.assertEqual(res.refused, [])
        self.assertEqual(res.lines(), ["moved tests/acceptance/a02_two.rs into tests/acceptance/group_a.rs (acceptance_a)",
                                       "moved tests/acceptance/b102_y.rs into tests/acceptance/group_b1.rs (acceptance_b1)"])
        self.assertEqual((d / "Cargo.toml").read_bytes().decode(), self.TOML)
        self.assertEqual((d / "tests/acceptance/group_a.rs").read_bytes().decode(),
                         "//! a\n\nmod entry;\n\nmod a01_one;\nmod a02_two;\nmod a03_three;\n")
        self.assertEqual((d / "tests/acceptance/a02_two.rs").read_bytes().decode(),
                         self.USES.replace("mod entry;", "use crate::entry;"))
        self.assertEqual((d / "tests/acceptance/b102_y.rs").read_bytes().decode(), "//! doc\n\n#[test]\nfn plain() {}\n")
        table = rust_tests.targets(d)
        self.assertEqual(table["tests/acceptance/b102_y.rs"], rust_tests.Target("acceptance_b1", "b102_y"))
        before = self.snapshot(d)
        again = rust_test_groups.convert(d)
        self.assertEqual((again.lines(), again.changed), ([], []))
        self.assertEqual(self.snapshot(d), before)

    def test_path_helper_lead_rename_and_untargeted_file(self):
        prop = '#[path = "../acceptance/entry/mod.rs"]\nmod entry;\n\n#[test]\nfn p() {\n    entry::shop(&[]);\n}\n'
        d = self.make(self.own("tests/property/test_a02.rs"), tests__property__test_a02_DOT_rs=prop,
                      tests__acceptance__a05_new_DOT_rs="#[test]\nfn n() {}\n")
        (d / "tests/acceptance/a03_three.rs").unlink()  # renamed to a05_new: its file is gone, the new one untargeted
        res = rust_test_groups.convert(d)
        self.assertEqual(res.refused, [])
        self.assertIn("dropped mod a03_three; from tests/acceptance/group_a.rs (its file is gone)", res.lines())
        self.assertEqual((d / "tests/property/test_a02.rs").read_bytes().decode(), prop.replace(
            '#[path = "../acceptance/entry/mod.rs"]\nmod entry;', "use crate::entry;"))
        self.assertEqual((d / "tests/property/group_a.rs").read_bytes().decode(), self.PROP_A + "mod test_a02;\n")
        self.assertEqual((d / "tests/acceptance/group_a.rs").read_bytes().decode(),
                         "//! a\n\nmod entry;\n\nmod a01_one;\nmod a05_new;\n")
        self.assertEqual([rust_tests.problem(r, rust_tests.targets(d)) for r in
                          ("tests/acceptance/a05_new.rs", "tests/property/test_a02.rs")], ["", ""])

    def test_refusals_write_nothing(self):
        cases = {"no group root matches its name": (self.own("tests/acceptance/c01_z.rs"),
                                                    {"tests__acceptance__c01_z_DOT_rs": "\n"}),
                 "its [[test]] block sets harness": (self.own("tests/acceptance/a02_two.rs") + "harness = false\n",
                                                     {"tests__acceptance__a02_two_DOT_rs": "\n"}),
                 "declares `mod util;`": (self.own("tests/acceptance/a02_two.rs"),
                                          {"tests__acceptance__a02_two_DOT_rs": "mod util;\n"})}
        for why, (extra, files) in cases.items():
            with self.subTest(why):
                d = self.make(extra, tests__acceptance__a09_ok_DOT_rs="\n", **files)
                before = self.snapshot(d)
                res = rust_test_groups.convert(d)
                self.assertTrue(any(why in r for r in res.refused), res.refused)
                self.assertEqual((res.moved, res.changed), ([], []))
                self.assertEqual(self.snapshot(d), before)

    def test_line_endings_kept(self):
        d = self.make(self.own("tests/acceptance/a02_two.rs"), tests__acceptance__a02_two_DOT_rs=self.USES)
        for rel in ("Cargo.toml", "tests/acceptance/group_a.rs", "tests/acceptance/a02_two.rs"):
            (d / rel).write_bytes((d / rel).read_bytes().replace(b"\n", b"\r\n"))
        rust_test_groups.convert(d)
        root = (d / "tests/acceptance/group_a.rs").read_bytes().decode()
        self.assertIn("mod a02_two;\r\n", root)
        self.assertEqual(root.count("\n"), root.count("\r\n"))
        self.assertIn(b"use crate::entry;\r\n", (d / "tests/acceptance/a02_two.rs").read_bytes())
        self.assertEqual((d / "Cargo.toml").read_bytes().decode(), self.TOML.replace("\n", "\r\n"))


    def derive(self, d):
        """The group roots of `d` turned derived (build.rs declares their modules)."""
        for rel in ("tests/acceptance/group_a.rs", "tests/acceptance/group_b1.rs", "tests/property/group_a.rs"):
            text = (d / rel).read_bytes().decode()
            kept = [ln for ln in text.splitlines(keepends=True) if not ln.startswith("mod ") or ln == "mod entry;\n"]
            area = Path(rel).stem.removeprefix("group_")
            include = f'include!(concat!(env!("OUT_DIR"), "/test_groups/{Path(rel).parent.name}/{area}.rs"));\n'
            (d / rel).write_bytes(("".join(kept) + include).encode("utf-8"))

    def test_derived_roots_build_every_file_of_their_area(self):
        d = self.make(tests__acceptance__b1_DOT_rs="\n", tests__acceptance__c01_z_DOT_rs="\n")
        self.derive(d)
        table = rust_tests.targets(d)
        self.assertEqual(table["tests/acceptance/a01_one.rs"], rust_tests.Target("acceptance_a", "a01_one"))
        self.assertEqual(table["tests/acceptance/b101_x.rs"], rust_tests.Target("acceptance_b1", "b101_x"))  # longest
        self.assertEqual(table["tests/acceptance/b1.rs"], rust_tests.Target("acceptance_b1", "b1"))
        self.assertEqual(table["tests/property/test_a01.rs"], rust_tests.Target("property_a", "test_a01"))
        self.assertEqual(table["tests/acceptance/group_a.rs"], rust_tests.Target("acceptance_a"))
        self.assertNotIn("tests/acceptance/c01_z.rs", table)  # no area fits: build.rs stops the build
        self.assertIn("start its name with the root's area", rust_tests.problem("tests/acceptance/c01_z.rs", table))
        self.assertEqual(rust_tests.member_area("test_r0530", ["r0", "r", "s"]), "r0")
        self.assertIsNone(rust_tests.member_area("group_r0", ["r0", "g"]))

    def test_derived_root_gains_no_mod_line_and_loses_merged_ones(self):
        d = self.make(self.own("tests/acceptance/a02_two.rs"), tests__acceptance__a02_two_DOT_rs=self.USES)
        self.derive(d)
        root = d / "tests/acceptance/group_a.rs"
        derived = root.read_bytes().decode()
        root.write_bytes(derived.replace("mod entry;\n", "mod entry;\nmod a01_one;\n").encode("utf-8"))  # a merge
        res = rust_test_groups.convert(d)
        self.assertEqual(res.refused, [])
        self.assertEqual(res.lines(), ["moved tests/acceptance/a02_two.rs into tests/acceptance/group_a.rs (acceptance_a)",
                                       "dropped mod a01_one; from tests/acceptance/group_a.rs (build.rs declares its "
                                       "modules)"])
        self.assertEqual(root.read_bytes().decode(), derived)
        self.assertEqual((d / "Cargo.toml").read_bytes().decode(), self.TOML)
        table = rust_tests.targets(d)
        self.assertEqual(table["tests/acceptance/a02_two.rs"], rust_tests.Target("acceptance_a", "a02_two"))
        before = self.snapshot(d)
        again = rust_test_groups.convert(d)
        self.assertEqual((again.lines(), again.changed), ([], []))
        self.assertEqual(self.snapshot(d), before)

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
