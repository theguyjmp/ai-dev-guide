"""rules_gate.py: per-language counts, the shrink-only debt, the text caps and the owner check. Temp git repos only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
os.environ["HARNESS_QS"] = str(HERE.parents[1] / "overhaul-build" / "scripts")
sys.path.insert(0, str(HERE))
import rules_gate as g  # noqa: E402
import rules_langs as rl  # noqa: E402

ARCH = """# Architecture

## Module a
Paths: crates/a/
Depends on: -

## Module b
Paths: crates/b/
Depends on: -
Forbidden types: *Widget*
"""
CLEAN_RS = "pub fn run(x: &str) -> Result<u32, String> {\n    x.parse::<u32>().map_err(|e| e.to_string())\n}\n"


def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout


def write(repo, files):
    for rel, text in files.items():
        p = Path(repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rules-gate-"))
        self.st = self.tmp / "state"
        self.st.mkdir()
        self.plan_lines, g.plan_lines = g.plan_lines, lambda repo: []  # plan_validate is tested in overhaul-build

    def tearDown(self):
        g.plan_lines = self.plan_lines

    def repo(self, files):
        """A repo whose main holds `files`, checked out on branch `row`."""
        r = self.tmp / "repo"
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        for k, v in (("user.email", "t@t"), ("user.name", "t"), ("core.autocrlf", "false")):
            git(r, "config", k, v)
        write(r, files)
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "base")
        git(r, "checkout", "-q", "-b", "row")
        return r

    def commit(self, r, files):
        write(r, files)
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "change")

    def problems(self, r, lang="rust"):
        return g.problems(r, "main", self.st, self.tmp, lang)

    def debt(self, rules):
        (self.st / g.DEBT).write_text(json.dumps({"rules": rules}), encoding="utf-8")


class RustCounts(Base):
    def counts(self, text, rel="crates/a/src/lib.rs"):
        return [h.rule for h in g.scan(rl.RUST, rel, text, None)]

    def test_swallow_shapes(self):
        for src in ("fn f() { let _ = std::fs::remove_file(p); }", "fn f() { x.send(v).ok(); }",
                    "fn f() { match r { Ok(v) => v, Err(_) => 0 } }", "fn f() { let v = s.parse::<u8>().unwrap_or(0); }"):
            self.assertEqual(self.counts(src), ["swallow"], src)

    def test_marker_and_mapped_error_exempt(self):
        self.assertEqual(self.counts("fn f() {\n let v = s.parse::<u8>().unwrap_or(0); // default: optional field\n}"), [])
        self.assertEqual(self.counts("fn f() { match r { Ok(v) => Ok(v), Err(_) => Err(E::Bad) } }"), [])

    def test_expect_panic_allow_and_test_code(self):
        self.assertEqual(sorted(self.counts("#[allow(dead_code)]\nfn f() { a.expect(\"x\"); panic!(\"y\"); }")),
                         ["allow", "expect", "panic"])
        self.assertEqual(self.counts("#[cfg(test)]\nmod tests { fn t() { a.expect(\"x\"); } }"), [])
        self.assertEqual(self.counts("fn f() { a.expect(\"x\"); }", rel="crates/a/tests/t.rs"), [])

    def test_ratchet_allows_debt_and_refuses_growth(self):
        r = self.repo({"crates/a/src/lib.rs": "fn f() { a.expect(\"x\"); }\n"})
        self.commit(r, {"crates/a/src/lib.rs": "fn f() { a.expect(\"x\"); }\nfn g() { b.expect(\"y\"); }\n"})
        out = self.problems(r)
        self.assertTrue(out and "2 x `.expect(`" in out[0] and "rules_gate.py init" in out[0], out)
        self.debt({"expect": {"crates/a/src/lib.rs": 2}})
        self.assertEqual(self.problems(r), [])

    def test_owner_stem_in_another_module_and_overlaps(self):
        r = self.repo({"spec/ARCHITECTURE.md": ARCH, "crates/a/src/lib.rs": CLEAN_RS})
        self.commit(r, {"crates/a/src/lib.rs": CLEAN_RS + "pub struct WidgetCache;\n"})
        out = self.problems(r)
        self.assertTrue(out and "WidgetCache matches module b" in out[0], out)
        self.commit(r, {"spec/ARCHITECTURE.md": ARCH + "Overlaps: crates/a/src/lib.rs::WidgetCache\n"})
        self.assertEqual(self.problems(r), [])


EXPECT = "pub fn f() -> u32 { \"1\".parse().expect(\"n\") }\n"


def manifest(name, dev="", normal=""):
    return (f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'
            + (f'[dependencies]\n{normal} = {{ path = "../{normal}" }}\n' if normal else "")
            + (f'[dev-dependencies]\n{dev} = {{ path = "../{dev}" }}\n' if dev else ""))


WORKSPACE = {"Cargo.toml": '[workspace]\nmembers = ["crates/a", "crates/b", "crates/kit"]\nresolver = "2"\n',
             "crates/a/Cargo.toml": manifest("a", dev="kit"), "crates/b/Cargo.toml": manifest("b"),
             "crates/kit/Cargo.toml": manifest("kit"), "crates/a/src/lib.rs": CLEAN_RS, "crates/b/src/lib.rs": CLEAN_RS,
             "crates/kit/src/lib.rs": "//! kit\n"}


class TestCode(Base):
    """Test code by the tree, never by a hand list: out-of-line #[cfg(test)] modules, crates only dev-dependencies use."""

    def test_out_of_line_cfg_test_module_and_its_submodule(self):
        r = self.repo({"crates/a/src/lib.rs": CLEAN_RS})
        self.commit(r, {"crates/a/src/lib.rs": CLEAN_RS + "/// fixture\n#[cfg(test)]\npub(crate) mod fixture;\n",
                        "crates/a/src/fixture.rs": "mod deep;\n" + EXPECT, "crates/a/src/fixture/deep.rs": EXPECT,
                        "crates/a/benches/b.rs": EXPECT})
        self.assertEqual(self.problems(r), [])

    def test_the_same_module_without_cfg_test_is_product(self):
        r = self.repo({"crates/a/src/lib.rs": CLEAN_RS})
        self.commit(r, {"crates/a/src/lib.rs": CLEAN_RS + "pub(crate) mod fixture;\n", "crates/a/src/fixture.rs": EXPECT})
        p = self.problems(r)
        self.assertEqual(len(p), 1, p)
        self.assertTrue(p[0].startswith("crates/a/src/fixture.rs:"), p)

    def test_product_counts_and_dev_only_crate_does_not(self):
        r = self.repo(WORKSPACE)
        self.assertEqual(rl.rust_tree(r).packages, ("crates/kit/",))
        self.commit(r, {"crates/kit/src/lib.rs": EXPECT, "crates/a/src/lib.rs": CLEAN_RS + EXPECT})
        p = self.problems(r)
        self.assertEqual(len(p), 1, p)
        self.assertTrue(p[0].startswith("crates/a/src/lib.rs:"), p)

    def test_dev_only_crate_with_a_normal_dependent_counts_again(self):
        for change in ({"crates/b/Cargo.toml": manifest("b", normal="kit")},
                       {"crates/a/Cargo.toml": manifest("a", normal="kit")}):
            with self.subTest(change=list(change)):
                self.tearDown(), self.setUp()
                r = self.repo({**WORKSPACE, "crates/kit/src/lib.rs": EXPECT})
                self.commit(r, change)  # the manifest only: no file of the crate changed
                p = self.problems(r)
                self.assertEqual(len(p), 1, p)
                self.assertTrue(p[0].startswith("crates/kit/src/lib.rs:"), p)

    def test_cargo_failing_is_a_problem_and_nothing_is_dev_only(self):
        r = self.repo({**WORKSPACE, "Cargo.toml": "[workspace\n"})
        self.commit(r, {"crates/kit/src/lib.rs": EXPECT})
        p = self.problems(r)
        self.assertEqual(len(p), 2, p)
        self.assertIn("cargo metadata failed", p[0])
        self.assertTrue(p[1].startswith("crates/kit/src/lib.rs:"), p)


class Debt(Base):
    def test_save_refuses_growth_and_tighten_lowers(self):
        self.debt({"expect": {"f.rs": 2}})
        with self.assertRaises(ValueError):
            g.save_debt(self.st, {"rules": {"expect": {"f.rs": 3}}}, g.load_debt(self.st))
        r = self.repo({"crates/a/src/lib.rs": "fn f() { a.expect(\"x\"); }\n"})
        self.debt({"expect": {"crates/a/src/lib.rs": 3, "crates/a/src/gone.rs": 1}})
        self.assertEqual(sorted(g.tighten(rl.RUST, self.st, r)),
                         ["expect:crates/a/src/gone.rs 1->0", "expect:crates/a/src/lib.rs 3->1"])
        self.assertEqual(g.load_debt(self.st)["rules"], {"expect": {"crates/a/src/lib.rs": 1}})

    def test_init_records_main_counts(self):
        r = self.repo({"crates/a/src/lib.rs": "fn f() { a.expect(\"x\"); }\n"})
        new = g.init_debt(rl.RUST, self.st, r, "init")
        self.assertEqual(new["rules"]["expect"], {"crates/a/src/lib.rs": 1})
        self.assertNotIn("text", new["rules"])  # no [text] caps: no text debt


class Text(Base):
    def test_line_cap_and_no_loosening(self):
        r = self.repo({"overhaul.toml": "[text]\nline_words = 5\n", "spec/NOTES.md": "short\n"})
        self.commit(r, {"spec/NOTES.md": "one two three four five six seven\n"})
        self.assertTrue(any("line of 7 words (cap 5)" in p for p in self.problems(r)))
        self.commit(r, {"spec/NOTES.md": "short\n", "overhaul.toml": "[text]\nline_words = 9\n"})
        self.assertTrue(any("raised or removed" in p for p in self.problems(r)))

    def test_claude_md_whole_file_cap(self):
        hits = g.text_hits("CLAUDE.md", "word " * 50, g.TextCaps(always_read_words=40))
        self.assertEqual(len(hits), 1)


class Node(Base):
    def counts(self, text, rel="src/app.js"):
        return [h.rule for h in g.scan(rl.NODE, rel, text, None)]

    def test_empty_catch_and_silenced_lint(self):
        self.assertEqual(self.counts("try { f(); } catch (e) {}\np.catch(() => {});\n"), ["swallow", "swallow"])
        self.assertEqual(self.counts("// eslint-disable-next-line no-unused-vars\nconst a = 1;\n"), ["allow"])
        self.assertEqual(self.counts("try { f(); } catch (e) { log(e); throw e; }\nconst s = '{}';\n"), [])

    def test_test_files_and_owner_items(self):
        self.assertEqual(self.counts("try { f(); } catch {}", rel="src/app.test.js"), [])
        self.assertEqual(rl.NODE.items(rl.NODE.tokens("export default class WidgetBox {}\nclass Local {}\n")),
                         [("WidgetBox", 1)])

    def test_gate_on_a_node_tree(self):
        r = self.repo({"package.json": "{}", "src/app.js": "export const a = 1;\n"})
        self.commit(r, {"src/app.js": "export const a = 1;\ntry { f(); } catch (e) {}\n"})
        self.assertEqual(rl.detect(r), "node")
        out = g.problems(r, "main", self.st, self.tmp)
        self.assertTrue(out and "src/app.js: 1 x dropped error" in out[0], out)


class PlanCache(Base):
    def test_plan_validate_cached_by_tree_content(self):
        r = self.repo({"spec/F.md": "a\n"})
        calls = []
        g.plan_lines = lambda repo: calls.append(1) or ["x"]
        self.commit(r, {"spec/F.md": "b\n"})
        key = g.tree_id(r)
        self.assertEqual(g.cached_plan_lines(key, self.st, lambda: g.plan_lines(r)), ["x"])
        self.assertEqual(g.cached_plan_lines(key, self.st, lambda: g.plan_lines(r)), ["x"])
        self.assertEqual(len(calls), 1)
        git(r, "checkout", "-q", "main"), git(r, "merge", "-q", "--ff-only", "row")  # landed: it is the next base
        self.assertEqual(g.base_plan_lines(r, "main", self.st, self.tmp), ["x"])
        self.assertEqual(len(calls), 1)
        write(r, {"spec/F.md": "dirty\n"})  # an uncommitted edit is a different tree
        self.assertNotEqual(g.tree_id(r), key)


FN_HEAD = "| id | phase | module | deps | entry | acceptance | status | files | needs |\n" + "|---" * 9 + "|\n"
STUB, DONE = "pub fn f() -> u32 {\n    todo!()\n}\n", "pub fn f() -> u32 {\n    1\n}\n"


def fn_row(rid, status, files):
    return f"| {rid} | 1 | a | - | cli: x {rid} | tests/acceptance/{rid}.rs::t | {status} | {files} | stub |\n"


FNS = FN_HEAD + fn_row("R1", "planned", "crates/a/src/one.rs") + fn_row("R2", "planned", "crates/a/src/two.rs") \
    + fn_row("R3", "planned", "crates/a/src/three.rs") + fn_row("R5", "proven", "crates/a/src/two.rs")
PLAN_BASE = {"overhaul.toml": "[text]\n", "spec/FUNCTIONS.md": FNS, "crates/a/src/one.rs": STUB, "crates/a/src/two.rs": STUB,
             "crates/a/src/three.rs": DONE, "crates/b/src/lib.rs": "//! b\n"}


class PlanApplies(Base):
    """No overhaul.toml = no overhaul plan: the plan checks stay silent whatever spec/FUNCTIONS.md holds."""

    def test_no_overhaul_toml_no_plan_lines(self):
        row = "| Y002 | P18 | kit | - | a.mjs | t.test.mjs::* | open |\n"
        r = self.repo({"spec/FUNCTIONS.md": row})
        self.commit(r, {"spec/FUNCTIONS.md": row + "| Z001 | P19 | kit | - | b.mjs | u.test.mjs::* | open |\n"})
        self.assertFalse(g.plan_applies(r))
        self.assertEqual(g.plan_problems(r, "main", self.tmp, self.tmp, ["spec/FUNCTIONS.md"], always=True), [])
        self.assertEqual(g.row_plan_problems(r, "main", self.tmp, ("Z001",)), [])

    def test_overhaul_toml_turns_it_on(self):
        r = self.repo({"overhaul.toml": "[text]\n", "spec/NOTES.md": "x\n"})
        self.assertTrue(g.plan_applies(r))


class RowPlan(Base):
    """row_plan_problems: plan_validate's real check_stubs on the planned rows one row's diff reaches."""

    def row_plan(self, files, landing=("R1",), delete=()):
        self.tmp = Path(tempfile.mkdtemp(dir=self.tmp))  # a fresh repo per call
        r = self.repo(PLAN_BASE)
        for rel in delete:
            (r / rel).unlink()
        self.commit(r, files)
        return g.row_plan_problems(r, "main", self.tmp, landing)

    def test_own_stub_passes_only_when_landing(self):
        self.assertEqual(self.row_plan({"crates/a/src/one.rs": DONE}), [])
        p = self.row_plan({"crates/a/src/one.rs": DONE}, landing=())
        self.assertTrue(len(p) == 1 and "R1: no stub in crates/a/src/one.rs" in p[0], p)

    def test_another_rows_stub_spent_or_deleted_fails(self):
        p = self.row_plan({"crates/a/src/one.rs": DONE, "crates/a/src/two.rs": DONE})
        self.assertTrue(len(p) == 1 and "R2: no stub in crates/a/src/two.rs" in p[0] and "R1:" not in p[0], p)
        p = self.row_plan({"crates/b/src/lib.rs": "//! b2\n"}, delete=("crates/a/src/two.rs",))
        self.assertTrue(len(p) == 1 and "R2: stub file missing" in p[0], p)

    def test_only_new_problems_and_reached_rows(self):
        self.assertEqual(self.row_plan({"crates/a/src/three.rs": DONE + "\n"}), [])  # main already lacks it
        self.assertEqual(self.row_plan({"crates/b/src/lib.rs": "//! b2\n"}), [])

    def test_spec_edit_rows(self):
        p = self.row_plan({"spec/FUNCTIONS.md": FNS + fn_row("R6", "planned", "crates/a/src/six.rs")})
        self.assertTrue(len(p) == 1 and "R6: stub file missing" in p[0], p)
        p = self.row_plan({"spec/FUNCTIONS.md": FNS.replace("crates/a/src/two.rs |", "crates/a/src/three.rs |", 1)})
        self.assertTrue(len(p) == 1 and "R2: no stub in" in p[0], p)

    def test_every_validate_check_is_classified(self):
        import inspect
        import re
        pv = rl.qs_import("plan_validate")
        called = {c.split(".")[-1] for c in re.findall(r"\b((?:check_\w+|entry_match\.\w+_problems))\(",
                                                       inspect.getsource(pv.validate))}
        self.assertTrue(called)
        self.assertFalse(called - set(g.ROW_PLAN_CHECKS) - set(g.ROW_PLAN_LEFT))
        self.assertFalse(set(g.ROW_PLAN_CHECKS) & set(g.ROW_PLAN_LEFT))


class PlanModes(Base):
    def test_always_runs_on_a_code_only_tree(self):
        lib = "crates/a/src/lib.rs"
        g.plan_lines = lambda repo: [] if "todo!()" in (Path(repo) / lib).read_text(encoding="utf-8") \
            else ["spec/FUNCTIONS.md R7: no stub in crates/a/src/lib.rs: no body calls todo!()"]
        r = self.repo({"overhaul.toml": "[text]\n", lib: STUB})
        self.commit(r, {lib: DONE})
        self.assertEqual(g.problems(r, "main", self.st, self.tmp, "rust"), [])  # plan="spec": no spec edit, no run
        p = g.problems(r, "main", self.st, self.tmp, "rust", plan="always")
        self.assertTrue(len(p) == 1 and "R7: no stub" in p[0], p)
        self.assertEqual(g.problems(r, "main", self.st, self.tmp, "rust", plan="always", landing=["R7"]), [])
        with self.assertRaises(ValueError):
            g.problems(r, "main", self.st, self.tmp, "rust", plan=True)


ARCH3 = "# Architecture\n" + "".join(f"\n## Module {m}\nPaths: crates/{m}/\nDepends on: -\n" for m in "abc")
GAUGE = "pub enum Gauge {\n    Fine,\n    Coarse,\n}\n"
STEPS = ("    let rows = scan(src)?;\n    let keys = index(&rows, depth, label)?.keys;\n    let tally = weigh(&rows, depth)?;\n"
         "    let out = merge(&keys, &tally, label, src)?;\n    Ok(emit(out, rows, keys, tally, label, src))\n")
FIRST = "pub fn first(src: &str, depth: u32, label: &str) -> Out {\n" + STEPS + "}\n"
SECOND = "fn second(src: &str, depth: u32, label: &str) -> Out {\n" + STEPS.replace("emit(", "flush(") + "}\n"
SMALL = "fn second(src: &str) -> u8 {\n    7\n}\n"


class Dups(Base):
    """Exported items named like another module's, and functions grown into near copies of a sibling."""

    def dups(self, r, base="main"):
        return g.dup_problems(rl.RUST, g.TreeCode(rl.RUST, r), r, base, self.st, g.changed_files(r, "main"))

    def set_dups(self, names=None, clones=()):
        (self.st / g.DEBT).write_text(json.dumps({"rules": {}, "dups": {"names": names or {}, "clones": list(clones)}}),
                                      encoding="utf-8")

    def test_new_item_named_like_another_modules(self):
        r = self.repo({g.ARCH: ARCH3, "crates/a/src/lib.rs": GAUGE, "crates/b/src/lib.rs": "//! b\n"})
        self.commit(r, {"crates/b/src/lib.rs": "pub fn local() {}\n" + GAUGE})
        p = self.problems(r)
        self.assertTrue(len(p) == 1 and "`Gauge` added in b" in p[0] and "no dups debt" in p[0], p)

    def test_a_single_owner_moving_passes(self):
        r = self.repo({g.ARCH: ARCH3, "crates/a/src/lib.rs": GAUGE, "crates/b/src/lib.rs": "//! b\n"})
        self.commit(r, {"crates/a/src/lib.rs": "//! a\n", "crates/b/src/lib.rs": GAUGE})
        self.assertEqual(self.dups(r), [])

    def test_snapshot_reads_a_commit(self):
        r = self.repo({g.ARCH: ARCH3, "crates/a/src/lib.rs": GAUGE, "crates/a/src/empty.rs": ""})
        self.commit(r, {"crates/a/src/lib.rs": "//! a\n", "crates/b/src/lib.rs": GAUGE})
        was = g.Snapshot(r, "main")
        self.assertEqual(was.files(("crates",)), ["crates/a/src/empty.rs", "crates/a/src/lib.rs"])
        self.assertEqual(was.texts(["crates/a/src/empty.rs", "crates/b/src/lib.rs", "crates/a/src/lib.rs"]),
                         {"crates/a/src/empty.rs": "", "crates/b/src/lib.rs": None, "crates/a/src/lib.rs": GAUGE})
        self.assertEqual(was.grep({"Gauge"}, ("crates",)), ["crates/a/src/lib.rs"])
        self.assertEqual(was.arch.module_of("crates/c/src/x.rs"), "c")

    def test_a_move_passes_when_debt_counts_the_double(self):
        r = self.repo({g.ARCH: ARCH3, "crates/a/src/lib.rs": GAUGE, "crates/b/src/lib.rs": GAUGE,
                       "crates/c/src/lib.rs": "//! c\n"})
        self.commit(r, {"crates/b/src/lib.rs": "//! b\n", "crates/c/src/lib.rs": GAUGE})
        self.assertEqual(len(self.dups(r)), 1)  # an existing double, unlisted
        self.set_dups({"Gauge": 2})
        self.assertEqual(self.dups(r), [])
        self.commit(r, {"crates/b/src/lib.rs": GAUGE})
        p = self.dups(r)
        self.assertTrue(len(p) == 1 and "3 modules" in p[0] and "allowed 2" in p[0], p)

    def test_name_conventions_from_both_trees(self):
        conv = {"overhaul.toml": '[rules]\nname_conventions = ["Gauge"]\n'}
        r = self.repo({g.ARCH: ARCH3, "crates/a/src/lib.rs": GAUGE, "crates/b/src/lib.rs": "//! b\n", **conv})
        self.commit(r, {"crates/b/src/lib.rs": GAUGE})
        self.assertEqual(self.dups(r), [])
        self.tearDown(), self.setUp()
        r = self.repo({g.ARCH: ARCH3, "crates/a/src/lib.rs": GAUGE, "crates/b/src/lib.rs": "//! b\n"})
        self.commit(r, {"crates/b/src/lib.rs": GAUGE, **conv})  # a diff cannot excuse its own double
        self.assertEqual(len(self.dups(r)), 1)

    def test_methods_and_scoped_items_are_not_module_names(self):
        r = self.repo({g.ARCH: ARCH3, "crates/a/src/lib.rs": "pub struct A;\nimpl A { pub fn new() -> A { A } }\n"})
        self.commit(r, {"crates/b/src/lib.rs": "pub struct B;\nimpl B { pub fn new() -> B { B } }\n"
                                               "pub(crate) struct A;\n"})
        self.assertEqual(self.dups(r), [])

    def test_changed_fn_grown_into_a_near_copy(self):
        self.assertGreaterEqual(len(g.fn_bodies(rl.RUST, FIRST)["first"].toks), g.CLONE_MIN_TOKENS)
        r = self.repo({g.ARCH: ARCH3, "crates/a/src/lib.rs": FIRST + SMALL})
        self.commit(r, {"crates/a/src/lib.rs": FIRST + SECOND})
        p = self.dups(r)
        self.assertTrue(len(p) == 1 and "near copy: crates/a/src/lib.rs::first ~ crates/a/src/lib.rs::second" in p[0], p)
        self.set_dups(clones=["crates/a/src/lib.rs::first ~ crates/a/src/lib.rs::second"])
        self.assertEqual(self.dups(r), [])

    def test_pair_close_at_base_passes(self):
        r = self.repo({g.ARCH: ARCH3, "crates/a/src/lib.rs": FIRST + SECOND})
        self.assertEqual(len(g.close_pairs({"crates/a/src/lib.rs": g.fn_bodies(rl.RUST, FIRST + SECOND)},
                                           g.tree_arch(r))), 1)
        self.commit(r, {"crates/a/src/lib.rs": FIRST + SECOND.replace("flush(", "store(")})
        self.assertEqual(self.dups(r), [])

    def test_no_verdict_is_a_problem(self):
        r = self.repo({g.ARCH: ARCH3, "crates/a/src/lib.rs": GAUGE})
        self.commit(r, {"crates/a/src/lib.rs": GAUGE + "pub fn more() {}\n"})
        p = self.dups(r, base="no-such-ref")
        self.assertTrue(len(p) == 1 and "no verdict" in p[0], p)

    def test_node_exports(self):
        arch = "# Architecture\n\n## Module a\nPaths: src/a/\nDepends on: -\n\n## Module b\nPaths: src/b/\nDepends on: -\n"
        r = self.repo({"package.json": "{}", g.ARCH: arch, "src/a/x.js": "export class Gauge {}\n", "src/b/y.js": "//\n"})
        self.commit(r, {"src/b/y.js": "export function Gauge() { return 1; }\n"})
        p = g.problems(r, "main", self.st, self.tmp)
        self.assertTrue(len(p) == 1 and "`Gauge` added in b" in p[0], p)


class DupDebt(Base):
    def test_init_seeds_and_save_refuses_growth(self):
        r = self.repo({g.ARCH: ARCH3, "crates/a/src/lib.rs": GAUGE + FIRST + SECOND, "crates/b/src/lib.rs": GAUGE})
        new = g.init_debt(rl.RUST, self.st, r, "init")
        key = "crates/a/src/lib.rs::first ~ crates/a/src/lib.rs::second"
        self.assertEqual(new["dups"], {"names": {"Gauge": 2}, "clones": [key]})
        old = g.load_debt(self.st)
        for grown in ({"names": {"Gauge": 3}, "clones": [key]}, {"names": {"Gauge": 2, "Other": 2}, "clones": [key]},
                      {"names": {"Gauge": 2}, "clones": [key, "x.rs::a ~ x.rs::b"]}):
            with self.assertRaises(ValueError):
                g.save_debt(self.st, {**old, "dups": grown}, old)
        g.save_debt(self.st, {**old, "dups": {"names": {}, "clones": []}}, old)

    def test_tighten_lowers(self):
        r = self.repo({g.ARCH: ARCH3, "crates/a/src/lib.rs": GAUGE + FIRST + SMALL, "crates/b/src/lib.rs": GAUGE})
        key = "crates/a/src/lib.rs::first ~ crates/a/src/lib.rs::second"
        (self.st / g.DEBT).write_text(json.dumps({"rules": {}, "dups": {"names": {"Gauge": 3, "Gone": 2},
                                                                        "clones": [key]}}), encoding="utf-8")
        self.assertEqual(sorted(g.tighten(rl.RUST, self.st, r)),
                         ["dups:clones:" + key + " gone", "dups:names:Gauge 3->2", "dups:names:Gone 2->0"])
        self.assertEqual(g.load_debt(self.st)["dups"], {"names": {"Gauge": 2}, "clones": []})


if __name__ == "__main__":
    unittest.main()
