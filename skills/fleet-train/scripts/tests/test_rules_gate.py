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
        self.assertEqual(rl.js_items(rl.NODE.tokens("export default class WidgetBox {}\nclass Local {}\n")),
                         [("WidgetBox", 1)])

    def test_gate_on_a_node_tree(self):
        r = self.repo({"package.json": "{}", "src/app.js": "export const a = 1;\n"})
        self.commit(r, {"src/app.js": "export const a = 1;\ntry { f(); } catch (e) {}\n"})
        self.assertEqual(rl.detect(r), "node")
        out = g.problems(r, "main", self.st, self.tmp)
        self.assertTrue(out and "src/app.js: 1 x dropped error" in out[0], out)


if __name__ == "__main__":
    unittest.main()
