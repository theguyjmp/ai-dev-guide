"""index_api.py: the capability INDEX, the semantic duplicate gate and the ranked INDEX lines in the briefs (pass E)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import drive  # noqa: E402
import index_api  # noqa: E402
import land  # noqa: E402
from test_drive import Toy, put  # noqa: E402

ARCH = """# Architecture

## Module core
Paths: crates/core/
Depends on: -

## Module store
Paths: crates/store/
Depends on: core

## Module doc
Paths: crates/doc/
Depends on: core

## Module samples
Paths: crates/samples/
Depends on: core

## Module tools
Paths: tools/
Depends on: -
"""

# Copies of the the pilot project helpers the 2026-11-10 manager review found duplicated (bae1ce5), plus distinct neighbours.
CORE = '''//! The shared refusal type.

/// Why a verb refused.
#[derive(Debug, Clone, PartialEq)]
pub struct Refusal {
    pub code: ErrorCode,
    pub message: String,
    pub field: Option<String>,
}

/// The refusal classes.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum ErrorCode {
    InvalidArgs,
    Corrupt,
}

impl Refusal {
    /// A refusal with `code` and `message`.
    pub fn new(code: ErrorCode, message: &str) -> Self {
        Self { code, message: message.to_owned(), field: None }
    }

    /// The same refusal naming `field`.
    #[must_use]
    pub fn with_field(mut self, field: &str) -> Self {
        self.field = Some(field.to_owned());
        self
    }
}

/// An `invalid-args` refusal naming `field`.
pub fn invalid(message: &str, field: &str) -> Refusal {
    Refusal::new(ErrorCode::InvalidArgs, message).with_field(field)
}

/// The corrupt-data refusal with `message`.
pub fn corrupt(message: String) -> Refusal {
    Refusal::new(ErrorCode::Corrupt, &message)
}

/// True when the relative path `p` stays inside the data directory.
pub fn stays_inside(p: &str) -> bool {
    !p.split('/').any(|c| c == "..")
}

/// The angle `deg` folded into 0..360 degrees.
pub fn fold_angle(deg: f64) -> f64 {
    deg.rem_euclid(360.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn helper_in_tests() -> u32 {
        1
    }

    #[test]
    fn invalid_names_field() {
        assert_eq!(invalid("m", "f").field.as_deref(), Some("f"));
    }
}
'''

MEMORY = '''use serde_json::Value;
use app_core::{invalid, Refusal};

/// Why `text` is not a valid note, or `None`.
fn text_problem(text: &str) -> Option<String> {
    if text.trim().is_empty() { Some("`text` must not be blank".to_owned()) } else { None }
}

/// The `text` param, validated.
fn text_param(params: &Value) -> Result<String, Refusal> {
    let text = params["text"]
        .as_str()
        .ok_or_else(|| invalid("`text` must be a string", "text"))?;
    match text_problem(text) {
        Some(why) => Err(invalid(&why, "text")),
        None => Ok(text.to_owned()),
    }
}

/// The optional `tags` param (absent means none), validated.
fn tags_param(params: &Value) -> Result<Vec<String>, Refusal> {
    match params.get("tags") {
        None => Ok(Vec::new()),
        Some(v) => v
            .as_array()
            .map(|a| a.iter().filter_map(|t| t.as_str().map(str::to_owned)).collect())
            .ok_or_else(|| invalid("`tags` must be a list", "tags")),
    }
}
'''

READ = '''use serde_json::Value;
use app_core::{invalid, Refusal};

/// A parsed read query.
struct Query {
    ids: Vec<u64>,
    exhaustive: bool,
}

/// Reads and checks `ids` and `exhaustive`.
fn parse_query(params: &Value) -> Result<Query, Refusal> {
    let ids = params["ids"].as_array().ok_or_else(|| invalid("`ids` must be a list", "ids"))?;
    let ids = ids.iter().filter_map(Value::as_u64).collect();
    let exhaustive = params["exhaustive"].as_bool().unwrap_or(false);
    Ok(Query { ids, exhaustive })
}
'''

GENERATE = '''use serde_json::Value;
use app_core::{invalid, stays_inside, Refusal};

/// Param `field` as a whole number from 0.
fn whole(params: &Value, field: &str, message: &str) -> Result<u64, Refusal> {
    params[field]
        .as_u64()
        .ok_or_else(|| invalid(message, field))
}

/// The `path` param: a non-blank relative path that stays inside the data directory.
fn path_param(params: &Value) -> Result<String, Refusal> {
    match params["path"].as_str() {
        Some(p) if !p.trim().is_empty() && stays_inside(p) => Ok(p.to_owned()),
        _ => Err(invalid("`path` must be a relative path", "path")),
    }
}

/// The triangle count of a box mesh with `n` cells per side.
fn box_triangles(n: u32) -> u64 {
    12 * u64::from(n) * u64::from(n)
}
'''

TOOLS = '''"""Build helpers."""


def load_rows(path: str) -> list[dict]:
    """The CSV rows of path as dicts."""
    return []


def _slug(text: str) -> str:
    """text lower-cased with dashes for spaces."""
    return text.lower().replace(" ", "-")


class Report:
    """One build report."""

    def add(self, line: str) -> None:
        """Append a line."""
        self.lines.append(line)
'''

BASE = {"crates/core/src/lib.rs": CORE, "crates/store/src/memory.rs": MEMORY,
        "crates/store/src/paged/read.rs": READ, "crates/samples/src/generate.rs": GENERATE, "tools/build.py": TOOLS}

# The 4 review pairs, each a branch adding the private copy (new path, its text, the helper to reuse)
COPY_READ = READ + '''
/// An `invalid-args` refusal naming `field`.
pub fn invalid(field: &str, message: &str) -> Refusal {
    Refusal::new(ErrorCode::InvalidArgs, message).with_field(field)
}
'''
BAD_PARAM = '''/// The `invalid-args` refusal on param `field` with `message`.
fn bad_param(field: &str, message: &str) -> app_core::Refusal {
    app_core::Refusal::new(app_core::ErrorCode::InvalidArgs, message).with_field(field)
}
'''
WHOLE = '''use serde_json::Value;
use app_core::{invalid, Refusal};

/// The optional whole-number param `field`, `default` when absent; `message` when it is not a u64.
pub fn whole(params: &Value, field: &str, default: u64, message: &str) -> Result<u64, Refusal> {
    match params.get(field) {
        None => Ok(default),
        Some(v) => v.as_u64().ok_or_else(|| invalid(message, field)),
    }
}
'''
TEXT_PARAM = '''/// The string param `field` when it passes `ok`.
///
/// # Errors
/// `invalid-args` with field `field` and `message` when the param is missing, not a string or
/// fails `ok`.
pub fn text_param<'p>(
    params: &'p serde_json::Value,
    field: &str,
    ok: fn(&str) -> bool,
    message: &str,
) -> Result<&'p str, app_core::Refusal> {
    params[field]
        .as_str()
        .filter(|s| ok(s))
        .ok_or_else(|| app_core::invalid(message, field))
}
'''
PAIRS = [("crates/store/src/paged/read.rs", COPY_READ, "crates/core/src/lib.rs::invalid"),
         ("crates/doc/src/lib.rs", BAD_PARAM, "crates/core/src/lib.rs::invalid"),
         ("crates/store/src/paged/children.rs", WHOLE, "crates/samples/src/generate.rs::whole"),
         ("crates/doc/src/lib.rs", TEXT_PARAM, "crates/store/src/memory.rs::text_param")]

DISTINCT = '''use serde_json::Value;
use app_core::{invalid, Refusal};

/// A parsed children query.
struct Query {
    id: u64,
    offset: u64,
}

/// Reads and checks `id` and `offset`.
fn parse_query(params: &Value) -> Result<Query, Refusal> {
    let id = params["id"].as_u64().ok_or_else(|| invalid("`id` must be a whole number", "id"))?;
    let offset = params["offset"].as_u64().unwrap_or(0);
    Ok(Query { id, offset })
}

/// The area of the triangle `a`, `b`, `c`.
fn triangle_area(a: [f64; 3], b: [f64; 3], c: [f64; 3]) -> f64 {
    let u = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
    let v = [c[0] - a[0], c[1] - a[1], c[2] - a[2]];
    let x = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
    0.5 * (x[0] * x[0] + x[1] * x[1] + x[2] * x[2]).sqrt()
}

/// The page of `ids` from `offset`, at most `limit` long.
fn page(ids: &[u64], offset: usize, limit: usize) -> Vec<u64> {
    ids.iter().skip(offset).take(limit).copied().collect()
}

/// Runs `store.children` for row R0102.
pub fn run(params: &Value) -> Result<Value, Refusal> {
    todo!("R0102")
}
'''


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        put(self.repo / "spec" / "ARCHITECTURE.md", ARCH)
        for p, text in BASE.items():
            put(self.repo / p, text)
        git(self.repo, "init", "-q", "-b", "main")
        for k, v in (("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false"),
                     ("core.autocrlf", "false")):
            git(self.repo, "config", k, v)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.mods = land.module_paths(ARCH)
        index_api._AT.clear()

    def branch(self, name, files):
        git(self.repo, "checkout", "-q", "-b", name, "main")
        for p, text in files.items():
            put(self.repo / p, text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", name)
        git(self.repo, "checkout", "-q", "main")
        return list(files)

    def problem(self, name, files):
        changed = self.branch(name, files)
        return index_api.semantic_problem(self.repo, "main", name, changed, self.mods)


class TestIndex(Repo):
    def test_index_lists_pub_and_private_items_of_rust_and_python(self):
        written = index_api.write(self.repo, self.mods)
        self.assertEqual(written, ["spec/INDEX.md", "spec/index/core.md", "spec/index/doc.md",
                                   "spec/index/samples.md", "spec/index/store.md", "spec/index/tools.md"])
        core = (self.repo / "spec/index/core.md").read_text(encoding="utf-8")
        self.assertIn("crates/core/src/lib.rs:33  pub fn invalid(message: &str, field: &str) -> Refusal  "
                      "// An `invalid-args` refusal naming `field`.  [purpose: ", core)
        self.assertIn("pub struct Refusal  // Why a verb refused.", core)
        self.assertIn("pub method Refusal::with_field(mut self, field: &str) -> Self", core)
        self.assertNotIn("helper_in_tests", core)  # cfg(test) items are not capabilities
        self.assertNotIn("invalid_names_field", core)
        gen = (self.repo / "spec/index/samples.md").read_text(encoding="utf-8")
        self.assertIn("crates/samples/src/generate.rs:5  fn whole(params: &Value, field: &str, message: &str) -> "
                      "Result<u64, Refusal>  // Param `field` as a whole number from 0.", gen)  # private helper
        tools = (self.repo / "spec/index/tools.md").read_text(encoding="utf-8")
        self.assertIn("tools/build.py:4  pub fn load_rows(path: str) -> list[dict]  // The CSV rows of path as dicts.",
                      tools)
        self.assertIn("tools/build.py:9  fn _slug(text: str) -> str", tools)
        self.assertIn("pub method Report::add(self, line: str) -> None", tools)
        summary = (self.repo / "spec/INDEX.md").read_text(encoding="utf-8")
        self.assertIn("| core | 8 |", summary)
        self.assertIn(index_api.HEADER, summary)
        purpose = core.split("pub fn invalid(", 1)[1].split("[purpose: ", 1)[1].split("]", 1)[0].split()
        for w in ("invalid", "arg", "refusal", "field", "message"):
            self.assertIn(w, purpose)

    def test_output_is_deterministic_and_check_sees_staleness(self):
        index_api.write(self.repo, self.mods)
        first = {p: (self.repo / p).read_text(encoding="utf-8") for p in ("spec/INDEX.md", "spec/index/core.md")}
        index_api.write(self.repo, self.mods)
        self.assertEqual(first, {p: (self.repo / p).read_text(encoding="utf-8") for p in first})
        self.assertEqual(index_api.main([str(self.repo), "--check"]), 0)
        put(self.repo / "crates/core/src/extra.rs", "/// More.\npub fn more() -> u8 {\n    1\n}\n")
        self.assertEqual(index_api.main([str(self.repo), "--check"]), 1)

    def test_land_stages_the_regenerated_index(self):
        put(self.repo / "crates/core/src/extra.rs", "/// More.\npub fn more() -> u8 {\n    1\n}\n")
        self.assertTrue(land.stage_index(str(self.repo), "spec/ARCHITECTURE.md"))
        staged = git(self.repo, "diff", "--cached", "--name-only").split()
        self.assertIn("spec/index/core.md", staged)
        self.assertIn("spec/INDEX.md", staged)
        self.assertIn("crates/core/src/extra.rs:2  pub fn more() -> u8  // More.",
                      (self.repo / "spec/index/core.md").read_text(encoding="utf-8"))


class TestSemanticGate(Repo):
    def test_catches_the_four_pilot_pairs(self):
        for k, (path, text, owner) in enumerate(PAIRS):
            with self.subTest(owner=owner, path=path):
                msg = self.problem(f"pair{k}", {path: text})
                self.assertIn(f"reuse {owner}", msg)
                self.assertIn("semantic duplicate (dup_semantic_min 0.7)", msg)

    def test_passes_distinct_helpers_a_move_and_plan_stubs(self):
        self.assertEqual(self.problem("distinct", {"crates/store/src/paged/children.rs": DISTINCT}), "")
        head, rest = MEMORY.split("/// Why `text`", 1)
        text_part, tags_part = rest.split("/// The optional `tags`", 1)
        moved = {"crates/store/src/memory.rs": head + "/// The optional `tags`" + tags_part,
                 "crates/store/src/notes.rs": head + "/// Why `text`" + text_part}
        self.assertEqual(self.problem("move", moved), "")

    def test_threshold_zero_is_off_and_gate_names_the_owner_line(self):
        changed = self.branch("copy", {PAIRS[0][0]: PAIRS[0][1]})
        self.assertEqual(index_api.semantic_problem(self.repo, "main", "copy", changed, self.mods, 2.0), "")
        msg = index_api.semantic_problem(self.repo, "main", "copy", changed, self.mods)
        self.assertIn("new `invalid` (crates/store/src/paged/read.rs:", msg)
        self.assertIn("(line 33)", msg)


class TestRanking(unittest.TestCase):
    def test_ranked_puts_purpose_matches_first(self):
        files = dict(BASE)
        items = [it for p, t in sorted(files.items()) for it in index_api.items_of(p, t)]
        lines = index_api.ranked(items, "Reads the `path` param: a relative path inside the data directory", top=3)
        self.assertEqual(len(lines), 3)
        self.assertIn("path_param", lines[0])
        self.assertTrue(all("[purpose: " in ln for ln in lines))


class TestBriefs(Toy):
    def test_build_and_review_briefs_list_ranked_index_lines(self):
        repo = self.make_repo()
        put(repo / "src" / "alpha" / "helpers.py", '"""Number helpers."""\n\n\n'
            'def add_numbers(a: int, b: int) -> int:\n    """The sum of two numbers a and b."""\n    return a + b\n\n\n'
            'def shout(text: str) -> str:\n    """text upper-cased."""\n    return text.upper()\n')
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "helpers")
        drv = drive.Driver(repo, drive.load_config(repo))
        drv.load()
        helpers = drv.helpers_text(drv.fns["F1"])  # F1 is `toy add 1 2`: add_numbers ranks first
        line = "src/alpha/helpers.py:4  pub fn add_numbers(a: int, b: int) -> int  // The sum of two numbers a and b."
        self.assertTrue(helpers.startswith("```\n" + line), helpers)
        build = drive.render("build-brief.md", {"helpers": helpers, "index_dir": index_api.INDEX_DIR})
        review = drive.render("review-brief.md", {"helpers": helpers, "index_dir": index_api.INDEX_DIR})
        for brief in (build, review):
            self.assertIn("## Existing helpers", brief)
            self.assertIn(line, brief)
        self.assertIn('git grep -n -i -E "<word>|<synonym>" -- spec/index/', build)
        self.assertIn("is a review FAIL (C05)", build)
        self.assertIn("cite the INDEX line you checked", review)
        self.assertLess(build.index("add_numbers"), build.index("## Duplication check"))


if __name__ == "__main__":
    unittest.main()
