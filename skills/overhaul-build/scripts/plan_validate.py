"""plan_validate.py: the plan gate of overhaul v2. A plan is not done, and drive.py must not start, until this passes.

  py -3 plan_validate.py --repo R [--run] [--timeout 600] [--feasibility [ids]]

--feasibility (planners, on the plan branch before it lands): feasibility.py asks one cheap agent per planned row
whether it is buildable against the code at HEAD; a MISSING capability, a file outside its module and deps, a row too
big for one builder or a file its `files` cell lacks is a problem line. Answers are cached in .overhaul/feasibility/.

Reads R/overhaul.toml, spec/FUNCTIONS.md and spec/ARCHITECTURE.md (paths from [paths], else those defaults) and prints one
line per problem, then `PLAN OK n functions` (exit 0) or `PLAN FAIL k problems` (exit 1). Exit 2 is bad usage. It writes
nothing but the --feasibility cache.

FUNCTIONS.md table: `| id | phase | module | deps | entry | acceptance | status | files | needs |`
  phase = integer from 1; deps = comma-separated ids or `-`; entry = the public CLI command, route or UI action;
  acceptance = `tests/acceptance/<f>.py::<test>` (Rust: `<f>.rs`), `;`-separated; status = planned|building|review|landing|proven|needs-plan.
  files = the function's own files (its stub), comma-separated, inside its module's Paths. REQUIRED.
  needs = `stub` (default), `behaviour`, or `F03:behaviour, F04:stub` per dep. REQUIRED.
ARCHITECTURE.md: `## Module <name>` sections, each with `Paths:` and `Depends on:` (`-` or `none` = none), and one
  `## Policies` section: `Concurrency:`, `Persistence:`, `Errors:`, `Untrusted input:` and `Retries:` lines, each
  `<owner module> - <policy>`; a `Concurrent writers:` line naming every function that writes shared state reachable
  concurrently and a `Boundaries:` line naming every function that reads untrusted input (HTTP bodies, files, imports),
  both `-` for none; a `Multi-store writes:` line naming every function whose one operation saves more than once
  (two stores, or one store twice), `-` for none; and a `Domain rules:` line, `<rule> -> <module>.<function>` joined by `;` (`-` for none), so
  each calculation or validation rule has exactly one home. A module with a `Public:` line must list on it each
  domain rule home and each API bullet that names a module depending on it ("orders parses quantities with it").
overhaul.toml [commands] must hold test, acceptance (with a `{test}` placeholder), lint, dup, literals, shape, cycles
  and entry_lint. A ruff lint must select E501 (or E) and the strict set E W F I B UP SIM RUF ARG A ANN201 PLR0915,
  in the command or the ruff config (ruff.toml, .ruff.toml or pyproject.toml [tool.ruff]). Python acceptance tests
  need a shared tests/conftest.py. A clippy lint must hold `-D warnings` and `--all-targets` (or `--tests`).
Rust plans: each .rs acceptance or property file is a root Cargo.toml `[[test]]` whose name is the file stem
  (`{stem}` in the commands), a test is a `#[test] fn`, and a .rs stub calls `todo!()` or `unimplemented!()`.

Problems (each is one line):
  - a row is malformed (cells, id, phase, deps, acceptance, status, duplicate id);
  - an acceptance file or its `def <test>` is missing;
  - a function's acceptance tests never invoke the exact entry its row names (entry_match.py);
  - deps are unknown, point to a later phase, or form a cycle; a phase holds more than [plan] max_phase_size
    (default 40) functions;
  - the table lacks the required `files` or `needs` column; a malformed needs cell; an unproven row with no files
    cell, or whose stub files are missing, raise no NotImplementedError or do not import (every function lands an
    importable stub in the plan commit); the critical path counts only `behaviour` deps;
  - a module is unknown, ARCHITECTURE is missing, or its module graph is broken (no Paths/Depends on, unknown module, cycle);
  - a function has no entry;
  - an overhaul.toml command is missing, or a ruff lint misses the line-length rule or the strict set;
  - a policy line is missing, names an unknown owner module, or a concurrent writer has no acceptance test whose name
    holds `concurrent`; an HTTP write route (POST, PUT, PATCH, DELETE) not listed as a concurrent writer, unless the
    Concurrency policy says single-threaded or serial; a boundary function (or an HTTP route with a body) without a
    negative acceptance test (a name holding invalid, malformed, bad, reject, corrupt, oversize, too_large, missing,
    negative, empty or injection); a domain rule without one `<module>.<function>` home, or named twice;
  - tests/conftest.py is missing;
  - the cycles, entry_lint, literals or shape command exits non-zero (run from the repo root, through overhaul.toml);
  - the longest deps chain is over [plan] max_depth, default max(4, ceil(N/4)); depth and widest level print to stderr;
  - with --run: a non-proven function's acceptance test PASSES (it must be red before the build), or a proven one FAILS
    (a proven failure reruns once alone first; one that passes then is only a "flaky under load" note on stderr).
  - a Python plan (acceptance in .py files) without tests/property/test_<id>.py per function holding 1 to 3 Hypothesis
    tests (`def test_*` under `@given`); a Rust plan without tests/property/test_<id>.rs holding 1 to 3 `#[test]` fns
    inside `proptest!`; [probe] property_tests = false turns this off. With --run and a [commands].property template
    ({file} = the property file, {stem} its stem), a non-proven function's property file must FAIL and a proven one
    must PASS. Planned tests are anchors: land.py rejects a branch that modifies one (anchors.py).
  - optional `rules` column (P07, S18; X12/X14): a cited id not defined in the [paths] patterns or standards file; with
    [paths] patterns, a `ui:` row or a row whose entry names a [plan] interactive_verbs word that cites no pattern id,
    or a cited pattern id missing from its acceptance test's name and comments. [commands.acceptance_by] templates
    need {test}; [commands.extra] is name = command; [tiers] is name = [globs].
  - X16 reuse before new: a module name or `Capability:` matching another module's `Forbidden types:` stems (globs,
    word by word), a stem on two modules, a new module (a `Why new:` line, or absent at git ref [plan] owners_since)
    with no reason; spec/ROADMAP.md ([paths] roadmap, optional) `| phase | behaviour | owner | why new |` rows: a bad
    phase, an empty behaviour or owner, an owner not in ARCHITECTURE (new) with no why-new on any row or matching
    another owner's stems, a phase over max_phase_size rows.
Python standard library only.
"""
import argparse
import fnmatch
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import anchors  # beside this script
import drive  # beside this script: parse_needs, the one reading of the needs cell
import entry_match  # beside this script
import rust_src  # beside this script
os.environ.setdefault("PROPTEST_DISABLE_FAILURE_PERSISTENCE", "1")  # no .proptest-regressions files (vc1)
os.environ.setdefault("PROPTEST_RNG_SEED", "0")  # fixed seed: a property green at land stays green on main (vc1)

COLUMNS = ["id", "phase", "module", "deps", "entry", "acceptance", "status"]
STATUSES = {"planned", "building", "review", "landing", "proven", "needs-plan", "deferred"}
DEFERRED = "deferred"  # planned later (rolling plan): id, phase, module, deps and entry are checked, nothing else
COMMANDS = ["test", "acceptance", "lint", "dup", "literals", "shape", "cycles", "entry_lint"]
POLICIES = ("concurrency", "persistence", "errors", "untrusted input", "retries")
ID_LISTS = ("concurrent writers", "boundaries", "multi-store writes")
POLICY_LINE = re.compile(r"^[\s>*-]*\**\s*(concurrency|persistence|errors|untrusted input|retries|concurrent writers|multi-store writes"
                         r"|boundaries|domain rules|fault hook)\s*\**\s*:\s*(.*)$", re.I)
ANY_POLICY_LINE = re.compile(r"^[\s>*-]*\**\s*([A-Za-z][\w -]*?)\s*\**\s*:\s*(.*)$")  # [plan] policy_lists names
PLAN_CHECKS = ("ui_verb", "float_roundtrip")  # [plan] checks: opt-in plan checks
VERB_ENTRY = re.compile(r"^(cli|http)\s*:", re.I)
DEP_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")
RULE_HOME = re.compile(r"^(\S.*?)\s*->\s*`?([\w\-]+)\.([A-Za-z_]\w*)`?$")
NEGATIVE = re.compile(r"invalid|malformed|bad|reject|corrupt|oversize|too_large|missing|negative|empty|injection", re.I)
BODY_ROUTE = re.compile(r"\b(POST|PUT|PATCH)\b")
OWNER_TEXT = re.compile(r"^`?([\w.\-]+)`?\s+[-–—]\s+(\S.*)$")
WRITE_ROUTE = re.compile(r"\b(POST|PUT|PATCH|DELETE)\b")
SERIAL = re.compile(r"single[- ]threaded|serial", re.I)
RUFF_STRICT = ("E", "W", "F", "I", "B", "UP", "SIM", "RUF", "ARG", "A", "ANN201", "PLR0915")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ACCEPT = re.compile(r"^tests/acceptance/[\w./-]+\.(py|rs)(::[A-Za-z_]\w*)+$")
OWNER_LINE = re.compile(r"^[\s>*-]*\**\s*(forbidden[ _-]types|why[ _-]new|capability)\s*\**\s*:\s*(.*)$", re.I)
NO_ENTRY = {"", "-", "--", "none", "n/a", "na", "tbd", "todo", "?"}
SEPARATOR = re.compile(r"^:?-+:?$")
STUB_COLUMNS = ("files", "needs")  # required beside COLUMNS, in any order after them
OPTIONAL_COLUMNS = ("rules",)  # allowed beside them: the project rule ids (P07, S18) a row must hold
STUB_CHECK = """import importlib, sys
bad = []
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
    except BaseException as e:  # noqa: BLE001  (a module that calls sys.exit on import)
        bad.append(f"{name}: {type(e).__name__}: {e}")
print("\\n".join(bad))
"""
DEFAULT_PATHS = {"functions": "spec/FUNCTIONS.md", "architecture": "spec/ARCHITECTURE.md"}


def load_toml(path):
    """overhaul.toml as a dict: tomllib when available (Python 3.11+), else a small reader for tables, strings and ints."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        import tomllib
    except ImportError:
        tomllib = None
    if tomllib:
        return tomllib.loads(text)
    import json
    data, table = {}, None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[([\w.-]+)\]\s*(#.*)?$", line)
        if m:
            table = data.setdefault(m.group(1), {})
            continue
        m = re.match(r"""^([\w-]+)\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*'|-?\d+)\s*(#.*)?$""", line)
        if not m or table is None:
            raise ValueError(f"line {n}: cannot read {line[:60]!r}")
        v = m.group(2)
        table[m.group(1)] = json.loads(v) if v[0] == '"' else (v[1:-1] if v[0] == "'" else int(v))
    return data


def run_cmd(cmd, cwd, timeout, full=False):
    """Run a shell command line from cwd; return (exit code, last output line), plus the whole output with `full`.
    Kills the whole tree on timeout (code 124)."""
    kw = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen(cmd, shell=True, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kw)
    try:
        out, _ = p.communicate(timeout=timeout)
        code = p.returncode
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGKILL)
        out, _ = p.communicate()
        code, out = 124, f"timed out after {timeout}s"
    lines = [x.strip() for x in (out or "").splitlines() if x.strip()]
    tail = lines[-1][:160] if lines else ""
    return (code, tail, out or "") if full else (code, tail)


def cells(line):
    """Cells of one markdown table row; `\\|` stays inside its cell."""
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", body)]


def bare(cell):
    return cell.strip().strip("`").strip()


def function_header(line):
    """The lower-cased column names when `line` is a FUNCTIONS.md table header, else None."""
    head = [bare(c).lower().strip("*") for c in cells(line)] if line.lstrip().startswith("|") else []
    if (head and [c for c in head if c not in STUB_COLUMNS + OPTIONAL_COLUMNS] == COLUMNS
            and all(head.count(c) <= 1 for c in STUB_COLUMNS + OPTIONAL_COLUMNS)):
        # `files` (the function's own files: its stub, drive.py's lock) and `needs` (stub or behaviour per dep)
        return head
    return None


def parse_functions(text, problems, label):
    """Rows of every FUNCTIONS.md table (one per phase is fine; drive.py reads them all) as dicts with `line`."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if function_header(line)]
    if not starts:
        problems.append(f"{label}: no table with the header | {' | '.join(COLUMNS + list(STUB_COLUMNS))} |")
        return []
    rows = []
    for i in starts:
        rows += parse_table(lines, i, function_header(lines[i]), problems, label)
    if not rows:
        problems.append(f"{label}: no function rows")
    return rows


def parse_table(lines, header_at, names, problems, label):
    """The rows of the one FUNCTIONS.md table whose header is on line index header_at."""
    rows = []
    lacking = [c for c in STUB_COLUMNS if c not in names]
    if lacking:
        problems.append(f"{label}:{header_at + 1}: the table lacks the required column(s) {', '.join(lacking)}: "
                        "`files` names each function's stub files, `needs` is stub (default) or behaviour per dep")
    i = header_at + 1
    if i < len(lines) and lines[i].lstrip().startswith("|") and all(SEPARATOR.match(c) for c in cells(lines[i])):
        i += 1
    for n in range(i, len(lines)):
        if not lines[n].lstrip().startswith("|"):
            break
        c = cells(lines[n])
        if len(c) != len(names):
            problems.append(f"{label}:{n + 1}: row has {len(c)} cells, want {len(names)}")
            continue
        row = dict(zip(names, c))
        row["line"] = n + 1
        rows.append(row)
    return rows


def parse_architecture(text):
    """{module: {"paths", "deps", "public": [...] or None, "line": n, "api": [(line, bullet)]}} from the
    `## Module <name>` sections."""
    mods, cur = {}, None
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^##\s+Module\s*:?\s+(\S+)", line, re.I)
        if m:
            cur = mods.setdefault(m.group(1).strip("`:*"), {"paths": None, "deps": None, "line": n,
                                                          "public": None, "api": [], "stems": [], "why": None,
                                                          "capability": ""})
            continue
        if re.match(r"^#{1,2}\s", line):
            cur = None
        if cur is None:
            continue
        if re.match(r"^\s*[-*]\s+`", line):
            cur["api"].append((n, line.strip()))
        m = OWNER_LINE.match(line)
        if m:  # X16: the type stems only this owner may define, why no existing owner fits, what it does
            key, rest = m.group(1).lower(), re.sub(r"^\*\*\s*", "", m.group(2).strip())  # `**X:** a`: drop the bold close
            if key.startswith("forbidden"):
                cur["stems"] += [x.strip("`\"' ") for x in re.split(r"[,;\s]+", rest.strip("[]")) if x.strip("`\"' ")]
            elif key.startswith("why"):
                cur["why"] = "" if rest.lower() in NO_ENTRY else rest
            else:
                cur["capability"] = rest
            continue
        m = re.match(r"^[\s>*-]*\**\s*(Paths|Depends on|Public)\s*:\s*\**\s*(.*)$", line, re.I)
        if m:
            items = [x.strip().strip("`").strip() for x in re.split(r"[,;]", m.group(2).strip().strip("*")) if x.strip().strip("`").strip()]
            if m.group(1).lower() == "paths":
                cur["paths"] = items
            elif m.group(1).lower() == "public":
                cur["public"] = items
            elif items:
                cur["deps"] = [] if [x.lower() for x in items] in (["-"], ["none"]) else items
    return mods


def find_cycles(graph):
    """Cycles of {node: [deps]} as node paths (one per back edge); iterative so a long chain cannot hit the recursion limit."""
    cycles, state = [], {}
    for start in graph:
        if state.get(start):
            continue
        state[start], path, stack = 1, [start], [(start, iter(graph[start]))]
        while stack:
            node, it = stack[-1]
            for nxt in it:
                if nxt not in graph:
                    continue
                if state.get(nxt) == 1:
                    cycles.append(path[path.index(nxt):] + [nxt])
                elif not state.get(nxt):
                    state[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, iter(graph[nxt])))
                    break
            else:
                state[node] = 2
                stack.pop()
                path.pop()
    return cycles


def rust_tokens(path):
    return rust_src.tokens(Path(path).read_text(encoding="utf-8", errors="replace"))


def rust_test_fns(path):
    """Names of the `#[test]` fns in a Rust file."""
    return {f["name"] for f in rust_src.functions(rust_tokens(path)) if f["test"]}


def rust_stubbed(path):
    """True when a Rust file calls `todo!` or `unimplemented!` (as code, not in a comment or string)."""
    toks = rust_tokens(path)
    return any(k == "ident" and t in ("todo", "unimplemented") and toks[i + 1][1] == "!"
               for i, (k, t, _) in enumerate(toks[:-1]))


def proptest_tests(path):
    """The number of `#[test]` fns inside `proptest!` invocations in a Rust file."""
    toks = rust_tokens(path)
    spans = rust_src.macro_spans(toks, "proptest")
    return sum(1 for f in rust_src.functions(toks) if f["test"] and any(a < f["start"] < b for a, b in spans))


def cargo_test_targets(repo):
    """{path: name} of the root Cargo.toml `[[test]]` targets."""
    import tomllib
    try:
        conf = tomllib.loads((Path(repo) / "Cargo.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(t.get("path", "")).replace("\\", "/").removeprefix("./"): str(t.get("name", ""))
            for t in conf.get("test") or [] if isinstance(t, dict)}


def cargo_target_problem(rel, targets):
    """A problem line when the Rust test file `rel` is not a [[test]] named after its stem, else ''."""
    stem = Path(rel).stem
    if targets.get(rel) == stem:
        return ""
    return f'{rel} is not a cargo test target: add [[test]] name = "{stem}" path = "{rel}" to the root Cargo.toml'


def check_cargo_targets(rows, repo, problems, flabel):
    """Every .rs acceptance file is a root Cargo.toml [[test]] named after its stem (the `{stem}` placeholder)."""
    targets, seen = cargo_test_targets(repo), set()
    for r in rows:
        for item in (bare(x) for x in r["acceptance"].split(";")):
            rel = item.split("::")[0]
            if rel.endswith(".rs") and ACCEPT.match(item) and rel not in seen and (repo / rel).is_file():
                seen.add(rel)
                bad = cargo_target_problem(rel, targets)
                if bad:
                    problems.append(f"{flabel}:{r['line']} {bare(r['id'])}: {bad}")


def check_rows(rows, mods, repo, problems, flabel):
    """Per-row checks. Returns {id: row} of the rows with a usable id, and each row's parsed `tests` [(file, def name)]."""
    by_id, phases = {}, {}
    for r in rows:
        rid = bare(r["id"])
        if not ID.match(rid):
            problems.append(f"{flabel}:{r['line']}: malformed id {r['id']!r}")
        elif rid in by_id:
            problems.append(f"{flabel}:{r['line']} {rid}: duplicate id (first on line {by_id[rid]['line']})")
        else:
            by_id[rid] = r
        r["rid"] = rid
        ph = bare(r["phase"])
        r["ph"] = int(ph) if re.fullmatch(r"[1-9]\d*", ph) else None
        if rid in by_id and by_id[rid] is r:
            phases[rid] = r["ph"]
    graph = {}
    for r in rows:
        rid, where = r["rid"], f"{flabel}:{r['line']} {r['rid']}"
        r["tests"] = []
        if r["ph"] is None:
            problems.append(f"{where}: malformed phase {r['phase']!r} (want an integer from 1)")
        status = bare(r["status"])
        r["st"] = status
        if status not in STATUSES:
            problems.append(f"{where}: malformed status {r['status']!r} (want one of {', '.join(sorted(STATUSES))})")
        module = bare(r["module"])
        if not module:
            problems.append(f"{where}: malformed module (empty)")
        elif mods is not None and module not in mods:
            problems.append(f"{where}: module {module!r} is not in ARCHITECTURE (has: {', '.join(sorted(mods)) or 'none'})")
        elif mods is not None and mods[module]["paths"] and "files" in r and status != DEFERRED:
            for f in (bare(x) for x in re.split(r"[,;]", r["files"])):
                if f not in ("", "-") and not any(f == p or (p.endswith("/") and f.startswith(p))
                                                  for p in mods[module]["paths"]):
                    problems.append(f"{where}: file {f!r} is outside module {module!r} Paths")
        if r["entry"].strip().strip("`").strip().lower() in NO_ENTRY:
            problems.append(f"{where}: no entry (the public CLI command, HTTP route or UI action)")
        deps_cell = bare(r["deps"])
        if deps_cell == "-":
            deps = []
        else:
            deps = [bare(d) for d in deps_cell.split(",")]
            if not deps_cell or not all(ID.match(d) for d in deps):
                problems.append(f"{where}: malformed deps {r['deps']!r} (want comma-separated ids or -)")
                deps = []
        for d in deps:
            if d not in phases:
                problems.append(f"{where}: dep {d} is unknown")
            elif r["ph"] is not None and phases[d] is not None and phases[d] > r["ph"]:
                problems.append(f"{where}: dep {d} is in phase {phases[d]}, a later phase than {r['ph']}")
            elif status != DEFERRED and bare(by_id[d]["status"]) == DEFERRED:
                problems.append(f"{where}: dep {d} is deferred; defer {rid} too, or plan {d} now")
        if rid in by_id and by_id[rid] is r:
            graph[rid] = [d for d in deps if d in phases]
        if status == DEFERRED:  # planned later: no files, stubs or tests yet
            continue
        items = [bare(x) for x in r["acceptance"].split(";")]
        if not any(items):
            problems.append(f"{where}: no acceptance (want tests/acceptance/<f>.py::<test>)")
        for item in filter(None, items):
            parts = item.split("::")
            if not ACCEPT.match(item) or ".." in parts[0].split("/"):
                problems.append(f"{where}: malformed acceptance {item!r} (want tests/acceptance/<f>.py::<test>)")
                continue
            src = repo / parts[0]
            if not src.is_file():
                problems.append(f"{where}: acceptance file {parts[0]} is missing")
            elif parts[0].endswith(".rs"):
                if parts[-1] in rust_test_fns(src):
                    r["tests"].append((item, parts[-1]))
                else:
                    problems.append(f"{where}: {parts[0]} has no #[test] fn {parts[-1]}")
            elif not re.search(rf"^[ \t]*(?:async[ \t]+)?def[ \t]+{re.escape(parts[-1])}[ \t]*\(",
                               src.read_text(encoding="utf-8", errors="replace"), re.M):
                problems.append(f"{where}: {parts[0]} has no def {parts[-1]}")
            else:
                r["tests"].append((item, parts[-1]))
    for cyc in find_cycles(graph):
        problems.append(f"{flabel}: dependency cycle {' -> '.join(cyc)}")


def row_deps(r):
    cell = bare(r["deps"])
    return [d for d in (bare(x) for x in cell.split(",")) if cell != "-" and d]


def waits_on_fn(rows):
    """id -> every row whose real behaviour that row's build waits for, through the needs cells, transitively."""
    direct = {}
    for r in rows:
        deps = row_deps(r)
        blocking = drive.parse_needs(r["needs"], deps) if "needs" in r else deps
        direct[bare(r["id"])] = set(blocking if blocking is not None else deps)
    def waits_on(fid):
        out, todo = set(), [bare(fid)]
        while todo:
            for d in direct.get(todo.pop(), ()):
                if d not in out:
                    out.add(d)
                    todo.append(d)
        return out
    return waits_on


def check_needs(rows, problems, flabel):
    """The required `needs` cell: `stub` (default), `behaviour`, or `F03:behaviour, F04:stub` per dep."""
    for r in rows:
        if "needs" in r and drive.parse_needs(r["needs"], row_deps(r)) is None:
            problems.append(f"{flabel}:{r['line']} {bare(r['id'])}: malformed needs {r['needs']!r}; want stub, "
                            "behaviour, or <dep id>:stub|behaviour for deps of this row, comma-separated")


def module_name(path):
    """`src/pkg/mod.py` -> `pkg.mod`; `pkg/__init__.py` -> `pkg`."""
    parts = Path(path).with_suffix("").parts
    parts = parts[1:] if parts and parts[0] == "src" else parts
    return ".".join(parts[:-1] if parts and parts[-1] == "__init__" else parts)


def check_stubs(rows, repo, problems, flabel, timeout=120):
    """The plan commit lands an interface stub for every function: each unproven row's files exist, its .py files
    raise NotImplementedError somewhere (the stub bodies) and import cleanly, its .rs files call todo!() or
    unimplemented!() (the clippy lint-green check compiles them)."""
    mods = []
    for r in rows:
        where = f"{flabel}:{r['line']} {bare(r['id'])}"
        if bare(r["status"]).lower() == "proven":
            continue
        if not bare(r.get("files", "")).strip("-"):
            problems.append(f"{where}: no files cell: name the files that hold this function's stub")
            continue
        files = [bare(x) for x in re.split(r"[,;]", r["files"]) if bare(x) and not any(c in bare(x) for c in "*?[")]
        missing = [f for f in files if not (repo / f).is_file()]
        if missing:
            problems.append(f"{where}: stub file missing: {', '.join(missing)}; the plan commit lands a stub for "
                            "every function (signatures, types, docstrings, bodies raising NotImplementedError)")
            continue
        py = [f for f in files if f.endswith(".py")]
        if py and not any("NotImplementedError" in (repo / f).read_text(encoding="utf-8", errors="replace")
                          for f in py):
            problems.append(f"{where}: no stub in {', '.join(py)}: no body raises NotImplementedError")
        rs = [f for f in files if f.endswith(".rs")]
        if rs and not any(rust_stubbed(repo / f) for f in rs):
            problems.append(f"{where}: no stub in {', '.join(rs)}: no body calls todo!() or unimplemented!()")
        mods +=[module_name(f) for f in py]
    if not mods:
        return
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(repo / d) for d in ("src", ".")))
    try:
        p = subprocess.run([sys.executable, "-c", STUB_CHECK, *dict.fromkeys(mods)], cwd=repo, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)
        bad = [ln for ln in p.stdout.splitlines() if ln.strip()]
    except subprocess.TimeoutExpired:
        bad = ["import of the stubs timed out"]
    for ln in bad[:10]:
        problems.append(f"{flabel}: stub does not import: {ln[:200]}")


MAX_PHASE_SIZE = 40


TEST_ITEM = re.compile(r"^\s*(?:#\[|@|(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:def|fn)\s)")


def test_block(text, name):
    """The source of test `name` (a Python def or a Rust fn) with the comment, decorator and attribute lines just
    above it, up to the next test item; '' if absent."""
    lines = text.splitlines()
    at = next((i for i, ln in enumerate(lines)
               if re.match(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:def|fn)\s+{re.escape(name)}\s*[(<]", ln)), None)
    if at is None:
        return ""
    start = at
    while start > 0 and re.match(r"^\s*(?:#|//|@)", lines[start - 1]):
        start -= 1
    end = next((i for i in range(at + 1, len(lines)) if TEST_ITEM.match(lines[i])), len(lines))
    while end < len(lines) and end - 1 > at and re.match(r"^\s*(?:#(?!\[)|//)", lines[end - 1]):
        end -= 1  # the comments just above the next item are that item's
    return "\n".join(lines[start:end])


def interactive_row(r, verbs):
    """A row the patterns bind: its entry is `ui:` or names one of [plan] interactive_verbs as a word."""
    entry = bare(r["entry"]).lower()
    return entry.startswith("ui:") or any(re.search(rf"(?<![\w-]){re.escape(v.lower())}(?![\w-])", entry)
                                          for v in verbs)


def check_project_rules(rows, cfg, paths, repo, problems, flabel):
    """The optional rules column (X12/X14): every cited id is defined in the [paths] patterns or standards file,
    every interactive row names at least one pattern id, and each pattern id it names appears in its acceptance
    test (name or comment). Off when neither the column nor [paths] patterns is set."""
    files = {k: paths[k] for k in drive.RULE_PATHS if paths.get(k)}
    if not files and not any(bare(r.get("rules", "")) not in ("", "-") for r in rows):
        return
    defs = {}
    for key, rel in files.items():
        f = repo / rel
        if not f.is_file():
            problems.append(f"overhaul.toml: [paths] {key} = {rel!r} is missing")
            continue
        defs[key] = drive.rule_defs(f.read_text(encoding="utf-8", errors="replace"))
    known = {k for d in defs.values() for k in d}
    pats = set(defs.get("patterns", {}))
    verbs = [str(v) for v in ((cfg.get("plan") or {}).get("interactive_verbs") or []) if str(v).strip()]
    for r in rows:
        where = f"{flabel}:{r['line']} {bare(r['id'])}"
        ids = list(dict.fromkeys(drive.RULE_ID.findall(r.get("rules", ""))))
        if ids and not files:
            problems.append(f"{where}: rules {', '.join(ids)} but overhaul.toml sets no [paths] patterns or standards")
            continue
        for rid in ids:
            if defs and rid not in known:
                problems.append(f"{where}: rule {rid} is not defined in {', '.join(files.values())}")
        if "patterns" in defs and interactive_row(r, verbs) and not any(i in pats for i in ids):
            problems.append(f"{where}: interactive row (entry {bare(r['entry'])!r}) names no pattern id in its "
                            f"rules column; cite the {files['patterns']} ids its acceptance proves")
        cited = [i for i in ids if i in pats]
        if not cited or not r.get("tests"):
            continue
        blocks = []
        for item, name in r["tests"]:
            src = repo / item.split("::")[0]
            blocks.append(test_block(src.read_text(encoding="utf-8", errors="replace"), name) if src.is_file() else "")
        text = "\n".join(blocks)
        for rid in cited:
            if not re.search(rf"(?<![A-Za-z0-9]){re.escape(rid)}(?![0-9])", text, re.I):
                problems.append(f"{where}: pattern {rid} does not appear in its acceptance test (name it in the "
                                "test's name or a comment on the test that proves it)")


def check_phase_size(rows, cfg, problems, flabel):
    """At most [plan] max_phase_size (default 40) functions per phase: drive.py's phase barrier holds phase N+1
    until every phase <=N function is proven, so a huge phase is one wide, late-failing batch."""
    limit = int((cfg.get("plan") or {}).get("max_phase_size") or MAX_PHASE_SIZE)
    sizes = {}
    for r in rows:
        if r.get("ph") is not None:
            sizes[r["ph"]] = sizes.get(r["ph"], 0) + 1
    for ph, n in sorted(sizes.items()):
        if n > limit:
            problems.append(f"{flabel}: phase {ph} has {n} functions > max_phase_size {limit}; split it into "
                            "more phases, or set [plan] max_phase_size in overhaul.toml")


def check_depth(rows, cfg, problems, flabel):
    """Critical path of the graph of blocking deps: every dep without a `needs` column, only the `behaviour` ones
    with it (a `stub` dep builds against the plan commit's stub at once). Returns (depth, widest level, longest
    chain); a chain longer than [plan] max_depth (default max(4, ceil(N/4))) is a problem, since each link lands
    one after another."""
    deps = {}
    for r in rows:
        rid = bare(r["id"])
        if ID.match(rid) and rid not in deps:
            d = row_deps(r)
            deps[rid] = d if "needs" not in r else (drive.parse_needs(r["needs"], d) or [])
    chain = {}

    def walk(n, seen):
        if n not in chain:
            chain[n] = [n]  # a cycle stops here; find_cycles reports it
            best = max(([walk(d, seen | {n}) for d in deps[n] if d in deps and d not in seen]), key=len, default=[])
            chain[n] = [*best, n]
        return chain[n]

    for n in deps:
        walk(n, frozenset())
    if not chain:
        return 0, 0, []
    longest = max(chain.values(), key=len)
    levels = {}
    for c in chain.values():
        levels[len(c)] = levels.get(len(c), 0) + 1
    limit = (cfg.get("plan") or {}).get("max_depth") or max(4, -(-len(deps) // 4))
    if len(longest) > limit:
        problems.append(f"{flabel}: dependency depth {len(longest)} > {limit} ({' -> '.join(longest)}); put shared "
                        "types, the store and errors in a foundation function, depend only on what a function's code "
                        "calls, or set [plan] max_depth in overhaul.toml")
    return len(longest), max(levels.values()), longest


def check_architecture(mods, problems, alabel):
    for name, m in mods.items():
        where = f"{alabel}:{m['line']} module {name}"
        if not m["paths"]:
            problems.append(f"{where}: no Paths:")
        if m["deps"] is None:
            problems.append(f"{where}: no Depends on: (write - for none)")
            continue
        for d in m["deps"]:
            if d not in mods:
                problems.append(f"{where}: Depends on unknown module {d}")
    for cyc in find_cycles({k: v["deps"] or [] for k, v in mods.items()}):
        problems.append(f"{alabel}: module cycle {' -> '.join(cyc)}")


def parse_policies(text, extra=()):
    """({policy: (owner or None, text, line)}, {id list: (line, [ids])}, (line, [rule entries]) or None) from the
    `## Policies` section; `extra` names more id lists ([plan] policy_lists)."""
    pol, lists, rules, inside = {}, {}, None, False
    for n, line in enumerate(text.splitlines(), 1):
        if re.match(r"^#{1,2}\s", line):
            inside = bool(re.match(r"^#{1,2}\s+Policies\b", line, re.I))
            continue
        m = POLICY_LINE.match(line) if inside else None
        if inside and not m and extra:
            m = ANY_POLICY_LINE.match(line)
            m = m if m and m.group(1).strip().lower() in extra else None
        if not m:
            continue
        key, rest = m.group(1).strip().lower(), m.group(2).strip()
        if key == "fault hook":
            pol[key] = (None, rest.strip("`* "), n)
        elif key in ID_LISTS or key in extra:
            ids = [x.strip("`") for x in re.split(r"[,;\s]+", rest)]
            lists[key] = (n, [x for x in ids if x and x.lower() not in ("-", "none")])
        elif key == "domain rules":
            rules = (n, [x.strip() for x in rest.split(";") if x.strip() and x.strip().lower() not in ("-", "none")])
        else:
            o = OWNER_TEXT.match(rest)
            pol[key] = (o.group(1), o.group(2), n) if o else (None, rest, n)
    return pol, lists, rules


def check_owners(pol, mods, problems, alabel):
    """Every cross-cutting policy line exists and names one owner module."""
    for key in POLICIES:
        if key not in pol:
            problems.append(f"{alabel}: ## Policies has no `{key.capitalize()}: <owner module> - <policy>` line")
            continue
        owner, _, n = pol[key]
        if not owner:
            problems.append(f"{alabel}:{n} policy {key}: write `<owner module> - <policy>`")
        elif mods is not None and owner not in mods:
            problems.append(f"{alabel}:{n} policy {key}: owner {owner} is no module")


def check_writers(pol, writers, rows, problems, alabel, deferred=frozenset()):
    """A concurrent acceptance test for every concurrent writer, and every threaded HTTP write listed."""
    by_id = {r.get("rid"): r for r in rows}
    for fid in writers[1]:
        r = by_id.get(fid)
        if fid in deferred:
            continue  # planned later: its tests come with its plan commit
        if r is None:
            problems.append(f"{alabel}:{writers[0]} Concurrent writers: unknown function {fid}")
        elif not any("concurrent" in name.lower() for _, name in r.get("tests", [])):
            problems.append(f"{alabel}:{writers[0]} {fid} writes shared state concurrently but has no acceptance test "
                            "whose name holds `concurrent` (parallel writers with readers running alongside, then "
                            "check every write was kept and no reader or writer failed)")
    if SERIAL.search(pol.get("concurrency", (None, "", 0))[1]):
        return
    for r in rows:
        if r.get("rid") and r["rid"] not in writers[1] and WRITE_ROUTE.search(r.get("entry", "")):
            problems.append(f"{alabel}:{writers[0]} {r['rid']} is an HTTP write ({r['entry']}) but not in "
                            "Concurrent writers; list it, or declare the Concurrency policy single-threaded")


def check_crash_safe(multi, rows, problems, alabel, deferred=frozenset(), hook="", repo=None):
    """Every operation that saves more than once has a crash test: fail between the saves, then the stored state is
    consistent and a retry applies the change once (trial 11: pay crashed between saves took the stock twice). With
    a `Fault hook: NAME` policy line, each crash test's body names NAME (the env var or hook that injects the fault)."""
    by_id = {r.get("rid"): r for r in rows}
    for fid in multi[1]:
        r = by_id.get(fid)
        crash = [(item, name) for item, name in (r or {}).get("tests", []) if "crash" in name.lower()]
        if fid in deferred:
            continue
        if r is None:
            problems.append(f"{alabel}:{multi[0]} Multi-store writes: unknown function {fid}")
        elif not crash:
            problems.append(f"{alabel}:{multi[0]} {fid} saves more than once per operation but has no acceptance "
                            "test whose name holds `crash` (make the second save fail, then check the stored state "
                            "is consistent and a retry applies the change exactly once)")
        for item, name in crash if hook and repo is not None else ():
            src = Path(repo) / item.split("::")[0]
            body = test_block(src.read_text(encoding="utf-8", errors="replace"), name) if src.is_file() else ""
            if hook not in body:
                problems.append(f"{alabel}:{multi[0]} {fid} crash test {item} does not name the fault hook {hook} "
                                "(inject the fault through it, so the crash is the product's, not the test's)")


def check_boundaries(bounds, rows, problems, alabel, deferred=frozenset()):
    """Every function that reads untrusted input, and every HTTP route with a body, has a negative acceptance test."""
    by_id = {r.get("rid"): r for r in rows}
    for fid in bounds[1]:
        if fid not in by_id and fid not in deferred:
            problems.append(f"{alabel}:{bounds[0]} Boundaries: unknown function {fid}")
    for r in rows:
        fid = r.get("rid")
        if not fid or not (fid in bounds[1] or BODY_ROUTE.search(r.get("entry", ""))):
            continue
        if fid not in bounds[1]:
            problems.append(f"{alabel}:{bounds[0]} {fid} takes an HTTP body ({r['entry']}) but is not in Boundaries")
        if not any(NEGATIVE.search(name) for _, name in r.get("tests", [])):
            problems.append(f"{alabel}:{bounds[0]} {fid} reads untrusted input but has no negative acceptance test "
                            "(a name holding invalid, malformed, oversize, corrupt, ...: bad input is rejected cleanly)")


def check_rules(rules, mods, problems, alabel):
    """Each domain rule names exactly one `<module>.<function>` home, and no rule is listed twice."""
    seen = set()
    for entry in rules[1]:
        m = RULE_HOME.match(entry)
        if not m:
            problems.append(f"{alabel}:{rules[0]} domain rule {entry!r}: write `<rule> -> <module>.<function>`")
            continue
        rule, mod = m.group(1).strip().lower(), m.group(2)
        if mods is not None and mod not in mods:
            problems.append(f"{alabel}:{rules[0]} domain rule {entry!r}: {mod} is no module")
        if rule in seen:
            problems.append(f"{alabel}:{rules[0]} domain rule {m.group(1)!r} is listed twice; give it one home")
        seen.add(rule)


def exported(public, target):
    """Whether a Public line covers the dotted `target` (`whole`, `stock.whole`): its name, or its submodule whole."""
    parts = target.split(".")
    for p in public:
        q = p.split(".")
        if q[-1] == parts[-1] or (len(parts) > 1 and q[-len(parts) + 1:] == parts[:-1]):
            return True
    return False


def check_reuse(mods, rules, problems, alabel):
    """A helper other modules are told to call is on its owner's Public line: each domain rule home, and each API
    bullet that names a module depending on its owner. Otherwise the caller may not import it, may not edit the
    owner, and the reviewer fails the copy it writes instead: a pass lost per attempt."""
    if not mods:
        return
    users = {m: [u for u, v in mods.items() if u != m and m in (v["deps"] or [])] for m in mods}
    for entry in (rules[1] if rules else []):
        m = RULE_HOME.match(entry)
        home = m and m.group(2) in mods and mods[m.group(2)]
        if home and home["public"] is not None and users[m.group(2)] and not exported(home["public"], m.group(3)):
            problems.append(f"{alabel}:{rules[0]} domain rule {m.group(1)!r}: {m.group(2)}.{m.group(3)} is not on "
                            f"module {m.group(2)}'s Public line, so {', '.join(users[m.group(2)])} cannot call it")
    for name, mod in mods.items():
        if mod["public"] is None:
            continue
        for n, bullet in mod["api"]:
            call = re.match(r"^[-*]\s+`([A-Za-z_][\w.]*)", bullet)
            rest = bullet[call.end():] if call else ""
            named = [u for u in users[name] if re.search(rf"(?<![\w.`]){re.escape(u)}(?![\w`(])", rest)]
            if named and not exported(mod["public"], call.group(1)):
                problems.append(f"{alabel}:{n} module {name}: {call.group(1)} is for {', '.join(named)} to call but "
                                f"is not on the Public line; add it there")


WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
ROADMAP_COLUMNS = ("phase", "behaviour", "owner", "why new")


def words(text):
    """Lower-case words of a name or sentence: snake_case, kebab-case, CamelCase and a::b all split."""
    return [w.lower() for w in WORD.findall(text or "")]


def stem_hits(stems, text, whole=False):
    """The stems that `text` matches, case and punctuation ignored, word by word: `*CommandStack*` matches the words
    `command stack`, `*Compar*` (trailing *) also `compare`, `Instance` only the word `instance`. With `whole` (an
    owner name) only its last `::`, `.` or `/` segment counts: `app-kernel::mesh` is a mesh owner."""
    ws = words(re.split(r"::|[./]", text or "")[-1] if whole else text)
    out = []
    for stem in stems:
        core, prefix = words(re.sub(r"[*?]", " ", stem)), stem.rstrip().endswith(("*", "?"))
        k = len(core)
        for i in range(len(ws) - k + 1) if core else ():
            run = ws[i:i + k]
            if run[:-1] == core[:-1] and (run[-1].startswith(core[-1]) if prefix else run[-1] == core[-1]):
                out.append(stem)
                break
    return out


def stem_problems(owners, name, texts, where):
    """X16: `name` and each capability text against every OTHER owner's forbidden type stems."""
    out = []
    for other, stems in owners.items():
        if other == name or not stems:
            continue
        hits = stem_hits(stems, name, whole=True)
        for t in texts:
            hits += [s for s in stem_hits(stems, t) if s not in hits]
        if hits:
            out.append(f"{where}: matches module {other}'s forbidden types {', '.join(hits)}; extend {other} "
                       "instead of a new owner (X16 reuse before new)")
    return out


def check_stems(mods, problems, alabel):
    """Forbidden types stems across all owners: each stem has one owner, and no module's name or Capability matches
    another owner's stems (a second home for the same thing)."""
    if not mods:
        return
    first = {}
    for name, m in mods.items():
        for stem in m.get("stems", []):
            key = stem.lower()
            if key in first and first[key] != name:
                problems.append(f"{alabel}:{m['line']} module {name}: forbidden type {stem} is also module "
                                f"{first[key]}'s; give each stem one owner")
            first.setdefault(key, name)
    owners = {k: v.get("stems", []) for k, v in mods.items()}
    for name, m in mods.items():
        problems += stem_problems(owners, name, [m.get("capability", "")], f"{alabel}:{m['line']} module {name}")


def baseline_modules(repo, ref, apath, problems):
    """The ARCHITECTURE modules at git `ref` ([plan] owners_since), or None when it cannot be read."""
    try:
        p = subprocess.run(["git", "show", f"{ref}:{apath}"], cwd=str(repo), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        problems.append(f"overhaul.toml: [plan] owners_since {ref!r}: git failed ({e})")
        return None
    if p.returncode:
        problems.append(f"overhaul.toml: [plan] owners_since {ref!r}: cannot read {apath} there "
                        f"({(p.stderr or '').strip()[:100]})")
        return None
    return parse_architecture(p.stdout)


def check_new_owners(repo, cfg, mods, problems, alabel):
    """X16: a new owner needs a one-line `Why new:` reason. A module is new when it has a `Why new:` line, or when
    [plan] owners_since names a git ref whose ARCHITECTURE lacks it."""
    if not mods:
        return
    ref = (cfg.get("plan") or {}).get("owners_since")
    old = baseline_modules(repo, str(ref), alabel, problems) if ref else None
    for name, m in mods.items():
        new = m.get("why") is not None or (old is not None and name not in old)
        if new and not m.get("why"):
            problems.append(f"{alabel}:{m['line']} module {name}: a new owner needs a `Why new: <one line>` reason no "
                            "existing owner fits (X16)")


def roadmap_header(line):
    """The ROADMAP column names when `line` is its table header, else None."""
    if not line.lstrip().startswith("|"):
        return None
    head = [re.sub(r"[_-]", " ", bare(c).lower().strip("*")).replace("behavior", "behaviour") for c in cells(line)]
    return head if all(c in head for c in ROADMAP_COLUMNS) else None


def owner_name(cell):
    """The owner module of a ROADMAP owner cell (`undo (C02)` -> undo)."""
    m = re.match(r"[\w.:\-]+", bare(cell))
    return m.group(0) if m else ""


def roadmap_rows(lines, rel, problems):
    """[(line, {column: cell})] of every ROADMAP table."""
    rows = []
    for i, line in enumerate(lines):
        head = roadmap_header(line)
        if not head:
            continue
        for n in range(i + 1, len(lines)):
            if not lines[n].lstrip().startswith("|"):
                break
            c = cells(lines[n])
            if all(SEPARATOR.match(x) for x in c):
                continue
            if len(c) != len(head):
                problems.append(f"{rel}:{n + 1}: row has {len(c)} cells, want {len(head)}")
                continue
            rows.append((n + 1, dict(zip(head, c))))
    return rows


def check_roadmap(repo, cfg, paths, mods, problems):
    """spec/ROADMAP.md (optional): the later-phase rows of a rolling plan, `| phase | behaviour | owner | why new |`.
    Each row names an owner; an owner not in ARCHITECTURE is new and needs a why-new reason on one of its rows, and
    its name and behaviours must not match another owner's forbidden type stems; each phase holds at most
    [plan] max_phase_size rows."""
    rel = paths.get("roadmap") or "spec/ROADMAP.md"
    f = Path(repo) / rel
    if not f.is_file():
        return
    rows = roadmap_rows(f.read_text(encoding="utf-8", errors="replace").splitlines(), rel, problems)
    if not rows:
        problems.append(f"{rel}: no table with the header | {' | '.join(ROADMAP_COLUMNS)} |")
        return
    mods = mods or {}
    owners = {k: v.get("stems", []) for k, v in mods.items()}
    new, sizes = {}, {}
    for n, r in rows:
        ph, owner = bare(r["phase"]), owner_name(r["owner"])
        if not re.fullmatch(r"[1-9]\d*", ph):
            problems.append(f"{rel}:{n}: malformed phase {r['phase']!r} (want an integer from 1)")
        else:
            sizes[int(ph)] = sizes.get(int(ph), 0) + 1
        if bare(r["behaviour"]).lower() in NO_ENTRY:
            problems.append(f"{rel}:{n}: empty behaviour")
        if not owner:
            problems.append(f"{rel}:{n}: empty owner; name the ARCHITECTURE owner it extends (X16)")
        elif owner not in mods:
            d = new.setdefault(owner, {"line": n, "why": False, "texts": []})
            d["why"] |= bare(r["why new"]).lower() not in NO_ENTRY
            d["texts"].append(bare(r["behaviour"]))
    for owner, d in new.items():
        where = f"{rel}:{d['line']} owner {owner}"
        if not d["why"]:
            problems.append(f"{where}: is not in ARCHITECTURE, so it is a new owner and needs a why-new reason no "
                            "existing owner fits (X16)")
        problems += stem_problems(owners, owner, d["texts"], where)
    limit = int((cfg.get("plan") or {}).get("max_phase_size") or MAX_PHASE_SIZE)
    for ph, k in sorted(sizes.items()):
        if k > limit:
            problems.append(f"{rel}: phase {ph} has {k} rows > max_phase_size {limit}; split it into more phases")


def policy_lists(cfg, problems):
    """[plan] policy_lists = {name = test-name regex}: more Policies id lists (`Cancellable:` -> `cancel`)."""
    raw = (cfg.get("plan") or {}).get("policy_lists")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append("overhaul.toml: [plan] policy_lists must be a table of name = test-name regex")
        return {}
    out = {}
    for name, rx in raw.items():
        try:
            out[str(name).strip().lower()] = re.compile(str(rx), re.I)
        except re.error as e:
            problems.append(f"overhaul.toml: [plan] policy_lists {name!r}: bad regex ({e})")
    return out


def check_policy_lists(extra, lists, rows, problems, alabel, deferred=frozenset()):
    """Each [plan] policy_lists line exists; each id on it is a function with an acceptance test the regex names."""
    by_id = {r.get("rid"): r for r in rows}
    for name, rx in extra.items():
        if name not in lists:
            problems.append(f"{alabel}: ## Policies has no `{name.capitalize()}:` line ([plan] policy_lists; "
                            "write - for none)")
            continue
        n, ids = lists[name]
        for fid in ids:
            r = by_id.get(fid)
            if fid in deferred:
                continue
            if r is None:
                problems.append(f"{alabel}:{n} {name.capitalize()}: unknown function {fid}")
            elif not any(rx.search(t) for _, t in r.get("tests", [])):
                problems.append(f"{alabel}:{n} {fid} is listed in {name.capitalize()} but has no acceptance test "
                                f"whose name matches `{rx.pattern}`")


def check_policies(text, mods, rows, problems, alabel, deferred=frozenset(), cfg=None, repo=None):
    """One owner per cross-cutting policy, tests for concurrent writers and boundaries, one home per domain rule.
    Ids of deferred rows on a list are skipped: their tests come with a later plan commit."""
    extra = policy_lists(cfg or {}, problems)
    pol, lists, rules = parse_policies(text, tuple(extra))
    check_owners(pol, mods, problems, alabel)
    if "concurrent writers" in lists:
        check_writers(pol, lists["concurrent writers"], rows, problems, alabel, deferred)
    else:
        problems.append(f"{alabel}: ## Policies has no `Concurrent writers:` line (write - for none)")
    hook = pol.get("fault hook", (None, "", 0))[1]
    if hook.lower() in ("-", "none"):
        hook = ""
    if "multi-store writes" in lists:
        check_crash_safe(lists["multi-store writes"], rows, problems, alabel, deferred, hook, repo)
    else:
        problems.append(f"{alabel}: ## Policies has no `Multi-store writes:` line (write - for none)")
    check_policy_lists(extra, lists, rows, problems, alabel, deferred)
    if "boundaries" in lists:
        check_boundaries(lists["boundaries"], rows, problems, alabel, deferred)
    else:
        problems.append(f"{alabel}: ## Policies has no `Boundaries:` line (write - for none)")
    if rules is None:
        problems.append(f"{alabel}: ## Policies has no `Domain rules: <rule> -> <module>.<function>; ...` line "
                        "(write - for none)")
    else:
        check_rules(rules, mods, problems, alabel)
    check_reuse(mods, rules, problems, alabel)


def plan_checks(cfg, problems):
    """The [plan] checks list (opt-in plan checks), unknown names reported."""
    raw = (cfg.get("plan") or {}).get("checks") or []
    if not isinstance(raw, list):
        problems.append(f"overhaul.toml: [plan] checks must be a list of {', '.join(PLAN_CHECKS)}")
        return set()
    for c in raw:
        if c not in PLAN_CHECKS:
            problems.append(f"overhaul.toml: [plan] checks {c!r} is unknown; known: {', '.join(PLAN_CHECKS)}")
    return {c for c in raw if c in PLAN_CHECKS}


def check_ui_verb(rows, problems, flabel):
    """Every `ui:` row depends on a `cli:` or `http:` row: each GUI action is a verb the harness tests apart (D5)."""
    by_id = {r.get("rid"): r for r in rows}
    for r in rows:
        if not bare(r["entry"]).lower().startswith("ui:"):
            continue
        if not any(VERB_ENTRY.match(bare(by_id[d]["entry"])) for d in row_deps(r) if d in by_id):
            problems.append(f"{flabel}:{r['line']} {r['rid']}: ui row depends on no cli: or http: row; add the "
                            "verb row it presents to its deps (behaviour in the verb, presentation in the ui row)")


def cargo_manifests(repo):
    """Every Cargo.toml of the repo outside target dirs and .overhaul."""
    skip = {"target", ".overhaul", ".git", "node_modules"}
    return sorted(p for p in Path(repo).rglob("Cargo.toml") if not skip & set(p.relative_to(repo).parts[:-1]))


def check_float_roundtrip(repo, problems):
    """A Rust project whose documents store floats parses them back exactly: every serde_json dependency (unless it
    only inherits the workspace's) enables `float_roundtrip` (slice F12: 0.059000000000000004 read back as 0.059)."""
    for path in cargo_manifests(repo):
        try:
            data = load_toml(path)
        except Exception:  # an unreadable manifest is cargo's error to report
            continue
        tables = [data.get(t) for t in DEP_TABLES] + [(data.get("workspace") or {}).get("dependencies")]
        tables += [(t or {}).get(k) for t in (data.get("target") or {}).values() if isinstance(t, dict) for k in DEP_TABLES]
        for deps in tables:
            spec = deps.get("serde_json") if isinstance(deps, dict) else None
            if spec is None or (isinstance(spec, dict) and spec.get("workspace") is True):
                continue
            feats = spec.get("features") or [] if isinstance(spec, dict) else []
            if "float_roundtrip" not in feats:
                problems.append(f"{path.relative_to(repo).as_posix()}: serde_json without the float_roundtrip "
                                "feature; documents that store floats must read them back exactly")


def check_traces(cfg, mods, every, repo, problems):
    """[plan] traces = [files]: each table row with a module and a test column (PERFORMANCE.md: technique, owner,
    test) names a module of ARCHITECTURE and a test node that a FUNCTIONS acceptance cell names."""
    traces = (cfg.get("plan") or {}).get("traces")
    if traces is None:
        return
    if not isinstance(traces, list) or not all(isinstance(t, str) for t in traces):
        problems.append("overhaul.toml: [plan] traces must be a list of file paths")
        return
    nodes = {bare(x) for r in every for x in r.get("acceptance", "").split(";") if bare(x)}
    for rel in traces:
        f = Path(repo) / rel
        if not f.is_file():
            problems.append(f"{rel}: missing ([plan] traces)")
            continue
        found, cols = False, None
        for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if not line.strip().startswith("|"):
                cols = None
                continue
            c = [bare(x).lower() for x in cells(line)]
            if cols is None:
                mod = next((i for i, x in enumerate(c) if x in ("module", "owner")), None)
                test = next((i for i, x in enumerate(c) if x in ("test", "tests", "acceptance")), None)
                cols = (mod, test) if mod is not None and test is not None else ("skip",)
                found = found or cols != ("skip",)
                continue
            if cols == ("skip",) or all(SEPARATOR.match(x) for x in c if x):
                continue
            raw = cells(line)
            if max(cols) >= len(raw):
                problems.append(f"{rel}:{n}: row has fewer cells than its header")
                continue
            module, tests = bare(raw[cols[0]]), [bare(x) for x in re.split(r"[;,]", raw[cols[1]]) if bare(x).lower() not in NO_ENTRY]
            if mods is not None and module not in mods:
                problems.append(f"{rel}:{n}: module {module!r} is no module of ARCHITECTURE")
            if not tests:
                problems.append(f"{rel}:{n}: names no test")
            for t in tests:
                if t not in nodes:
                    problems.append(f"{rel}:{n}: test {t} is in no FUNCTIONS acceptance cell")
        if not found:
            problems.append(f"{rel}: no table with a module (or owner) and a test column ([plan] traces)")


def ruff_selected(repo, cmd):
    """The rule codes a ruff lint selects: from --select/--extend-select in the command, else its config file."""
    codes = set()
    for m in re.finditer(r"--(?:extend-)?select[= ]([\w,]+)", cmd):
        codes.update(m.group(1).split(","))
    try:
        import tomllib
    except ImportError:
        return codes or None
    for name, table in (("ruff.toml", ()), (".ruff.toml", ()), ("pyproject.toml", ("tool", "ruff"))):
        path = repo / name
        if not path.is_file():
            continue
        try:
            conf = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        for key in table:
            conf = conf.get(key) or {}
        for sect in (conf, conf.get("lint") or {}):
            for key in ("select", "extend-select"):
                codes.update(c for c in sect.get(key) or [] if isinstance(c, str))
    return codes


def ruff_covers(codes, code):
    """True when a selected ruff code or prefix enables `code` (A selects flake8-builtins, not ANN; PL selects PLR)."""
    linter = re.match(r"[A-Z]+", code).group(0)
    return any(code.startswith(c) and (c.startswith(linter) or (linter.startswith("PL") and c == "PL"))
               for c in codes)


def check_ruff(repo, cmd, problems):
    """A ruff lint must enforce line length and the strict rule set before builders land."""
    if "ruff" not in cmd:
        return
    codes = ruff_selected(repo, cmd)
    if codes is None or "ALL" in codes:
        return
    missing = [c for c in RUFF_STRICT if not ruff_covers(codes, c)]
    if not ruff_covers(codes, "E501"):
        problems.append("overhaul.toml: [commands].lint (ruff) does not select E501; enable the line-length rule")
    if missing:
        problems.append(f"overhaul.toml: [commands].lint (ruff) misses the strict set: select {' '.join(missing)}")


def check_clippy(cmd, problems):
    """A clippy lint must deny warnings and check every target, so lint-green proves the stubs and tests compile."""
    if "clippy" not in cmd:
        return
    if not re.search(r"(?:-D|--deny)[= ]?\s*warnings\b", cmd):
        problems.append("overhaul.toml: [commands].lint (clippy) does not deny warnings: end it with `-- -D warnings`")
    if not re.search(r"--all-targets\b|--tests\b", cmd):
        problems.append("overhaul.toml: [commands].lint (clippy) does not check the tests: add --all-targets")


TEXT_SUFFIXES = (".py", ".md", ".toml", ".json", ".js", ".ts", ".rs", ".txt", ".cfg", ".ini", ".yaml", ".yml")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def check_control_chars(repo, problems):
    """No raw control characters in the contract (tests/, spec/, overhaul.toml): a `\\b` or `\\x..` that a
    writing tool turned into the byte itself makes a test no builder can pass (trial 9 F09: backspaces)."""
    files = [repo / "overhaul.toml"] + [p for d in ("tests", "spec") for p in sorted((repo / d).rglob("*"))
                                       if p.suffix in TEXT_SUFFIXES and "__pycache__" not in p.parts]
    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for n, ln in enumerate(text.splitlines(), 1):
            m = CONTROL.search(ln)
            if m:
                problems.append(f"{path.relative_to(repo).as_posix()}:{n}: raw control character "
                                f"{m.group()!r} (write the escape, e.g. r\"\\b\", not the byte)")


def check_lint_green(repo, cmd, problems, timeout=300):
    """The plan commit (and every manager plan fix) must keep the lint gate green: a red main fails every
    builder's pre-review gate and parks functions for a fault that is not theirs. A cargo lint gets at least 1200 s
    (a cold build of the dependencies)."""
    if re.search(r"\bcargo\b", cmd):
        timeout = max(timeout, 1200)
    try:
        p = subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        problems.append(f"overhaul.toml: [commands].lint timed out after {timeout}s on the plan tree")
        return
    missing = any(s in p.stderr for s in ("is not recognized as an internal or external command", "command not found",
                                          "no such command"))
    if p.returncode in (127, 9009) or missing:  # tool not installed here: the toolchain check owns that
        return
    if p.returncode != 0:
        tail = " / ".join([ln.strip() for ln in (p.stdout + p.stderr).splitlines() if ln.strip()][-4:])
        problems.append(f"lint is red on the plan tree (exit {p.returncode}): `{cmd}`: {tail}")


def given_tests(path):
    """The number of `def test_*` functions decorated with Hypothesis @given (any import form) in a file."""
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return -1
    def is_given(d):
        d = d.func if isinstance(d, ast.Call) else d
        return (isinstance(d, ast.Name) and d.id == "given") or (isinstance(d, ast.Attribute) and d.attr == "given")
    return sum(1 for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name.startswith("test_") and any(is_given(d) for d in n.decorator_list))


def check_property(rows, cfg, repo, problems, flabel):
    """Python plans: 1 to 3 Hypothesis property tests per function in tests/property/test_<id>.py, written now.
    Rust plans: 1 to 3 `#[test]` fns inside `proptest!` in tests/property/test_<id>.rs, a root Cargo.toml [[test]]."""
    if (cfg.get("probe") or {}).get("property_tests") is False:
        return
    files = [item.split("::")[0] for r in rows for item, _ in r.get("tests", [])]
    if not any(f.endswith(".py") for f in files):
        if any(f.endswith(".rs") for f in files):
            check_property_rust(rows, repo, problems, flabel)
        return
    for r in rows:
        if not ID.match(r["rid"]):
            continue
        rel, where = anchors.property_file(r["rid"]), f"{flabel}:{r['line']} {r['rid']}"
        n = given_tests(repo / rel) if (repo / rel).is_file() else None
        if n is None:
            problems.append(f"{where}: no property tests: write {rel} with 1 to 3 Hypothesis @given tests")
        elif n < 0:
            problems.append(f"{where}: {rel} does not parse")
        elif not 1 <= n <= 3:
            problems.append(f"{where}: {rel} has {n} @given tests, want 1 to 3")
        else:
            r["property"] = rel


def check_property_rust(rows, repo, problems, flabel):
    targets = cargo_test_targets(repo)
    for r in rows:
        if not ID.match(r["rid"]):
            continue
        rel, where = anchors.property_file(r["rid"], ".rs"), f"{flabel}:{r['line']} {r['rid']}"
        n = proptest_tests(repo / rel) if (repo / rel).is_file() else None
        if n is None:
            problems.append(f"{where}: no property tests: write {rel} with 1 to 3 #[test] fns inside proptest!")
        elif not 1 <= n <= 3:
            problems.append(f"{where}: {rel} has {n} #[test] fns inside proptest!, want 1 to 3")
        elif cargo_target_problem(rel, targets):
            problems.append(f"{where}: {cargo_target_problem(rel, targets)}")
        else:
            r["property"] = rel


def validate(repo, run=False, timeout=600, notes=None, lint=True):
    """Returns (problems, n functions). `repo` is the rebuild repo root."""
    repo, problems = Path(repo), []
    cfg, paths, toml_ok = {}, dict(DEFAULT_PATHS), False
    toml = repo / "overhaul.toml"
    if not toml.is_file():
        problems.append("overhaul.toml: missing")
    else:
        try:
            cfg, toml_ok = load_toml(toml), True
        except Exception as e:  # tomllib.TOMLDecodeError, ValueError
            problems.append(f"overhaul.toml: unreadable ({str(e)[:100]})")
    paths.update({k: v for k, v in (cfg.get("paths") or {}).items() if isinstance(v, str) and v})
    cmds = cfg.get("commands") if isinstance(cfg.get("commands"), dict) else {}
    cmd = {k: cmds[k].strip() for k in COMMANDS if isinstance(cmds.get(k), str) and cmds[k].strip()}
    if toml_ok:
        for k in COMMANDS:
            if k not in cmd:
                problems.append(f"overhaul.toml: [commands].{k} is missing")
    if "acceptance" in cmd and "{test}" not in cmd["acceptance"]:
        problems.append("overhaul.toml: [commands].acceptance has no {test} placeholder")
        cmd.pop("acceptance")
    by = cmds.get("acceptance_by")
    if by is not None and not isinstance(by, dict):
        problems.append("overhaul.toml: [commands].acceptance_by must be a table of glob = template")
    for glob, tmpl in (by.items() if isinstance(by, dict) else ()):
        if not isinstance(tmpl, str) or "{test}" not in tmpl:
            problems.append(f"overhaul.toml: [commands.acceptance_by] {glob!r} has no {{test}} placeholder")
    extra = cmds.get("extra")
    if extra is not None and not (isinstance(extra, dict) and all(isinstance(v, str) for v in extra.values())):
        problems.append("overhaul.toml: [commands.extra] must be a table of name = command")
    tiers = cfg.get("tiers")
    if tiers is not None and not (isinstance(tiers, dict)
                                  and all(isinstance(v, list) and all(isinstance(g, str) for g in v)
                                          for v in tiers.values())):
        problems.append("overhaul.toml: [tiers] must be a table of name = [test file globs]")

    arch_file = repo / paths["architecture"]
    mods = None
    if not arch_file.is_file():
        problems.append(f"{paths['architecture']}: missing (ARCHITECTURE)")
    else:
        mods = parse_architecture(arch_file.read_text(encoding="utf-8", errors="replace"))
        if not mods:
            problems.append(f"{paths['architecture']}: no `## Module <name>` sections")
        check_architecture(mods, problems, paths["architecture"])
        check_stems(mods, problems, paths["architecture"])
        check_new_owners(repo, cfg, mods, problems, paths["architecture"])
    check_roadmap(repo, cfg, paths, mods, problems)

    fun_file, rows, every = repo / paths["functions"], [], []
    checks = plan_checks(cfg, problems)
    if not fun_file.is_file():
        problems.append(f"{paths['functions']}: missing")
    else:
        rows = parse_functions(fun_file.read_text(encoding="utf-8", errors="replace"), problems, paths["functions"])
        check_rows(rows, mods, repo, problems, paths["functions"])
        every, rows = rows, [r for r in rows if r.get("st") != DEFERRED]  # deferred rows: check_rows only
        check_cargo_targets(rows, repo, problems, paths["functions"])
        check_phase_size(rows, cfg, problems, paths["functions"])
        check_project_rules(rows, cfg, paths, repo, problems, paths["functions"])
        problems += entry_match.entry_problems(
            repo, [r for r in rows if r["entry"].strip().strip("`").strip().lower() not in NO_ENTRY], paths["functions"])
        check_needs(rows, problems, paths["functions"])
        check_control_chars(repo, problems)
        problems += entry_match.cross_problems(repo, rows, paths["functions"], waits_on_fn(rows))
        check_stubs(rows, repo, problems, paths["functions"])
        check_property(rows, cfg, repo, problems, paths["functions"])
        if "ui_verb" in checks:
            check_ui_verb(rows, problems, paths["functions"])
        depth, width, _ = check_depth(rows, cfg, problems, paths["functions"])
        if notes is not None:
            notes.append(f"{paths['functions']}: critical path {depth} functions, widest level {width}"
                         + (f", {len(every) - len(rows)} deferred" if len(every) > len(rows) else ""))
    if arch_file.is_file():
        check_policies(arch_file.read_text(encoding="utf-8", errors="replace"), mods, rows, problems,
                       paths["architecture"], {r.get("rid") for r in every if r.get("st") == DEFERRED}, cfg, repo)
    if "float_roundtrip" in checks:
        check_float_roundtrip(repo, problems)
    check_traces(cfg, mods, every, repo, problems)
    if "lint" in cmd:
        before = len(problems)
        check_ruff(repo, cmd["lint"], problems)
        check_clippy(cmd["lint"], problems)
        if len(problems) == before and lint:  # a lax lint config is the problem to fix first
            check_lint_green(repo, cmd["lint"], problems)

    accepted = [bare(x).split("::")[0] for r in rows for x in r["acceptance"].split(";") if bare(x)]
    rust_only = bool(accepted) and all(f.endswith(".rs") for f in accepted)
    if (repo / "tests" / "acceptance").is_dir() and not rust_only and not (repo / "tests" / "conftest.py").is_file():
        problems.append("tests/conftest.py: missing; scaffold the shared fixtures there, once, for every test file")
    for k in ("cycles", "entry_lint", "literals", "shape"):
        if k in cmd:
            code, tail = run_cmd(cmd[k], repo, timeout)
            if code:
                problems.append(f"overhaul.toml: {k} command exited {code}" + (f": {tail}" if tail else ""))

    if run and "acceptance" in cmd:
        tests = [(r, item, f"{item.split('::')[0]}::{name}") for r in rows for item, name in r.get("tests", [])]
        # one command per test target ({test} = the bare test name, {stem} = the file stem), mapped back to ids
        acc = drive.land_mod.acceptance_template({**cmds, "acceptance": cmd["acceptance"]})
        runner = lambda c: run_cmd(c, repo, timeout, full=True)  # noqa: E731
        res = drive.land_mod.batch_results(acc, [node for _, _, node in tests], runner)
        proven = [node for r, _, node in tests if r["st"] == "proven"]
        for node in settle_flaky(acc, proven, res, runner):
            r, item = next((r, item) for r, item, n in tests if n == node)
            if notes is not None:
                notes.append(f"{paths['functions']}:{r['line']} {r['rid']}: flaky under load: its acceptance {item} "
                             "failed in the batch and passed alone")
        for r, item, node in tests:
            code, tail, _ = res[node]
            where = f"{paths['functions']}:{r['line']} {r['rid']}"
            if r["st"] == "proven" and code:
                problems.append(f"{where}: proven, but its acceptance {item} FAILS (exit {code})" + (f": {tail}" if tail else ""))
            elif r["st"] != "proven" and r["st"] in STATUSES and not code:
                problems.append(f"{where}: {r['st']}, but its acceptance {item} already PASSES (it must be red before the build)")
    prop = cmds.get("property") if isinstance(cmds.get("property"), str) else ""
    if run and prop:
        for r in rows:
            if not r.get("property"):
                continue
            code, tail = run_cmd(prop.replace("{file}", r["property"]).replace("{stem}", Path(r["property"]).stem),
                                 repo, timeout)
            where = f"{paths['functions']}:{r['line']} {r['rid']}"
            if r["st"] == "proven" and code:
                problems.append(f"{where}: proven, but its property tests {r['property']} FAIL (exit {code})"
                                + (f": {tail}" if tail else ""))
            elif r["st"] != "proven" and r["st"] in STATUSES and not code:
                problems.append(f"{where}: {r['st']}, but its property tests {r['property']} already PASS "
                                "(they must be red before the build)")
    return problems, len(rows)


def settle_flaky(template, proven, res, runner):
    """A proven acceptance that failed reruns ONCE alone, one after another, before it counts: timing tests failed
    only under load on 2026-11-09 (three planners and a build at once). A pass alone updates res and the node is
    returned as flaky under load; a second failure stands as the PLAN FAIL."""
    flaky = []
    for node in dict.fromkeys(proven):
        if not res.get(node) or res[node][0] == 0:
            continue
        (_, one), = drive.land_mod.acceptance_cmds(template, [node])
        code, tail, _ = runner(one)
        if code == 0:
            res[node] = (0, "", True)
            flaky.append(node)
        else:
            res[node] = (code, tail, True)
    return flaky


def feasibility_rows(repo, ids=None):
    """feasibility.Row for every planned row (or the ids given) of the plan in repo's working tree, the code at HEAD."""
    import feasibility  # beside this script; only --feasibility needs it
    cfg = drive.load_config(repo)
    frel, arel = cfg["paths"]["functions"], cfg["paths"]["architecture"]
    fns = drive.parse_functions((repo / frel).read_text(encoding="utf-8"))
    arch = (repo / arel).read_text(encoding="utf-8") if (repo / arel).is_file() else ""
    prot = {"tests/acceptance/", "overhaul.toml", frel, arel}
    prot |= {f"{Path(p).parent.as_posix()}/" for p in [frel, arel]}
    unit = drive.unit_dirs(cfg["commands"].get("test", "")) or ["tests/unit/"]
    plan = {f.id: (f.module, f.status, f.entry) for f in fns}
    rows = []
    for f in fns:
        if (ids and f.id not in ids) or (not ids and f.status != "planned"):
            continue
        section = drive.module_section(arch, f.module)
        own = drive.module_paths(section, f.module)
        cell = list(dict.fromkeys(f.files + drive.module_shared(section) if f.files else own))
        deps = dict.fromkeys(d for d in drive.module_deps(arch, f.module) if d != f.module)
        dep_paths = list(dict.fromkeys(p for d in deps for p in drive.module_paths(drive.module_section(arch, d), d)))
        files = list(dict.fromkeys(drive.acc_file(a) for a in f.acceptance))
        accept = "\n\n".join(f"`{x}`\n" + ((repo / x).read_text(encoding="utf-8", errors="replace")
                                            if (repo / x).is_file() else "(missing)") for x in files)
        table = f"{f.header}\n|{'---|' * len(drive.cells(f.header))}\n{f.row}"
        rows.append(feasibility.Row(f.id, f.module, list(f.deps), table, accept, section, cell, own, dep_paths,
                                    tuple(sorted(prot)), unit, list(dict.fromkeys(cell + files)), plan))
    return feasibility.plan_problems(repo, rows, cfg["harness"], "HEAD", drive.paths_overlap) if rows else []


def main(argv=None):
    ap = argparse.ArgumentParser(description="overhaul v2 plan validator")
    ap.add_argument("--repo", required=True, help="the rebuild repo root")
    ap.add_argument("--run", action="store_true", help="also run every acceptance test: non-proven must fail, proven must pass")
    ap.add_argument("--timeout", type=int, default=600, help="seconds allowed per command (default 600)")
<<<<<<< HEAD
    ap.add_argument("--feasibility", nargs="?", const="all", default="",
                    help="also ask feasibility.py whether each planned row (or the comma-separated ids) is buildable "
                         "against the code at HEAD: one cheap agent call per row, cached in .overhaul/feasibility/")
=======
    ap.add_argument("--no-lint", action="store_true", help="skip running the lint command (drive's live plan reload: "
                    "a whole-tree clippy timed out at 300 s under build load; lint gates every land and plan land)")
>>>>>>> origin/hotfix-now
    a = ap.parse_args(argv)
    if not Path(a.repo).is_dir():
        print(f"plan_validate: --repo {a.repo} is not a directory", file=sys.stderr)
        return 2
    notes = []
<<<<<<< HEAD
    problems, n = validate(a.repo, a.run, a.timeout, notes)
    if a.feasibility and not problems:  # a malformed plan first; the agent calls only on a plan that parses
        ids = set() if a.feasibility == "all" else {x.strip() for x in a.feasibility.split(",") if x.strip()}
        problems += feasibility_rows(Path(a.repo), ids)
=======
    problems, n = validate(a.repo, a.run, a.timeout, notes, lint=not a.no_lint)
>>>>>>> origin/hotfix-now
    for note in notes:
        print(note, file=sys.stderr)  # stderr: stdout stays problem lines plus the PLAN line
    for p in problems:
        print(p)
    print(f"PLAN FAIL {len(problems)} problems" if problems else f"PLAN OK {n} functions")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
