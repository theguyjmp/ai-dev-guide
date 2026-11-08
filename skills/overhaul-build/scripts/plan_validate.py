"""plan_validate.py: the plan gate of overhaul v2. A plan is not done, and drive.py must not start, until this passes.

  py -3 plan_validate.py --repo R [--run] [--timeout 600]

Reads R/overhaul.toml, spec/FUNCTIONS.md and spec/ARCHITECTURE.md (paths from [paths], else those defaults) and prints one
line per problem, then `PLAN OK n functions` (exit 0) or `PLAN FAIL k problems` (exit 1). Exit 2 is bad usage. It writes nothing.

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
  - deps are unknown, point to a later phase, or form a cycle;
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
  - with --run: a non-proven function's acceptance test PASSES (it must be red before the build), or a proven one FAILS.
  - a Python plan (acceptance in .py files) without tests/property/test_<id>.py per function holding 1 to 3 Hypothesis
    tests (`def test_*` under `@given`); a Rust plan without tests/property/test_<id>.rs holding 1 to 3 `#[test]` fns
    inside `proptest!`; [probe] property_tests = false turns this off. With --run and a [commands].property template
    ({file} = the property file, {stem} its stem), a non-proven function's property file must FAIL and a proven one
    must PASS. Planned tests are anchors: land.py rejects a branch that modifies one (anchors.py).
Python standard library only.
"""
import argparse
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

COLUMNS = ["id", "phase", "module", "deps", "entry", "acceptance", "status"]
STATUSES = {"planned", "building", "review", "landing", "proven", "needs-plan"}
COMMANDS = ["test", "acceptance", "lint", "dup", "literals", "shape", "cycles", "entry_lint"]
POLICIES = ("concurrency", "persistence", "errors", "untrusted input", "retries")
ID_LISTS = ("concurrent writers", "boundaries", "multi-store writes")
POLICY_LINE = re.compile(r"^[\s>*-]*\**\s*(concurrency|persistence|errors|untrusted input|retries|concurrent writers|multi-store writes"
                         r"|boundaries|domain rules)\s*\**\s*:\s*(.*)$", re.I)
RULE_HOME = re.compile(r"^(\S.*?)\s*->\s*`?([\w\-]+)\.([A-Za-z_]\w*)`?$")
NEGATIVE = re.compile(r"invalid|malformed|bad|reject|corrupt|oversize|too_large|missing|negative|empty|injection", re.I)
BODY_ROUTE = re.compile(r"\b(POST|PUT|PATCH)\b")
OWNER_TEXT = re.compile(r"^`?([\w.\-]+)`?\s+[-–—]\s+(\S.*)$")
WRITE_ROUTE = re.compile(r"\b(POST|PUT|PATCH|DELETE)\b")
SERIAL = re.compile(r"single[- ]threaded|serial", re.I)
RUFF_STRICT = ("E", "W", "F", "I", "B", "UP", "SIM", "RUF", "ARG", "A", "ANN201", "PLR0915")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ACCEPT = re.compile(r"^tests/acceptance/[\w./-]+\.(py|rs)(::[A-Za-z_]\w*)+$")
NO_ENTRY = {"", "-", "--", "none", "n/a", "na", "tbd", "todo", "?"}
SEPARATOR = re.compile(r"^:?-+:?$")
STUB_COLUMNS = ("files", "needs")  # required beside COLUMNS, in any order after them
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


def run_cmd(cmd, cwd, timeout):
    """Run a shell command line from cwd; return (exit code, last output line). Kills the whole tree on timeout (code 124)."""
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
    return code, (lines[-1][:160] if lines else "")


def cells(line):
    """Cells of one markdown table row; `\\|` stays inside its cell."""
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", body)]


def bare(cell):
    return cell.strip().strip("`").strip()


def parse_functions(text, problems, label):
    """Rows of the FUNCTIONS.md table as dicts (with `line`); malformed cell counts are reported and dropped."""
    rows, header_at, lines, names = [], None, text.splitlines(), COLUMNS
    for i, line in enumerate(lines):
        head = [bare(c).lower().strip("*") for c in cells(line)] if line.lstrip().startswith("|") else []
        if (head and [c for c in head if c not in STUB_COLUMNS] == COLUMNS
                and all(head.count(c) <= 1 for c in STUB_COLUMNS)):
            # `files` (the function's own files: its stub, drive.py's lock) and `needs` (stub or behaviour per dep)
            header_at, names = i, head
            break
    if header_at is None:
        problems.append(f"{label}: no table with the header | {' | '.join(COLUMNS + list(STUB_COLUMNS))} |")
        return rows
    lacking = [c for c in STUB_COLUMNS if c not in names]
    if lacking:
        problems.append(f"{label}:{header_at + 1}: the table lacks the required column(s) {', '.join(lacking)}: "
                        "`files` names each function's stub files, `needs` is stub (default) or behaviour per dep")
    i = header_at + 1
    if i < len(lines) and lines[i].lstrip().startswith("|") and all(SEPARATOR.match(c) for c in cells(lines[i])):
        i += 1
    if i >= len(lines) or not lines[i].lstrip().startswith("|"):
        problems.append(f"{label}: no function rows")
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
                                                          "public": None, "api": []})
            continue
        if re.match(r"^#{1,2}\s", line):
            cur = None
        if cur is None:
            continue
        if re.match(r"^\s*[-*]\s+`", line):
            cur["api"].append((n, line.strip()))
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
        elif mods is not None and mods[module]["paths"] and "files" in r:
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
        if rid in by_id and by_id[rid] is r:
            graph[rid] = [d for d in deps if d in phases]
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


def parse_policies(text):
    """({policy: (owner or None, text, line)}, {id list: (line, [ids])}, (line, [rule entries]) or None) from the
    `## Policies` section."""
    pol, lists, rules, inside = {}, {}, None, False
    for n, line in enumerate(text.splitlines(), 1):
        if re.match(r"^#{1,2}\s", line):
            inside = bool(re.match(r"^#{1,2}\s+Policies\b", line, re.I))
            continue
        m = POLICY_LINE.match(line) if inside else None
        if not m:
            continue
        key, rest = m.group(1).lower(), m.group(2).strip()
        if key in ID_LISTS:
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


def check_writers(pol, writers, rows, problems, alabel):
    """A concurrent acceptance test for every concurrent writer, and every threaded HTTP write listed."""
    by_id = {r.get("rid"): r for r in rows}
    for fid in writers[1]:
        r = by_id.get(fid)
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


def check_crash_safe(multi, rows, problems, alabel):
    """Every operation that saves more than once has a crash test: fail between the saves, then the stored state is
    consistent and a retry applies the change once (trial 11: pay crashed between saves took the stock twice)."""
    by_id = {r.get("rid"): r for r in rows}
    for fid in multi[1]:
        r = by_id.get(fid)
        if r is None:
            problems.append(f"{alabel}:{multi[0]} Multi-store writes: unknown function {fid}")
        elif not any("crash" in name.lower() for _, name in r.get("tests", [])):
            problems.append(f"{alabel}:{multi[0]} {fid} saves more than once per operation but has no acceptance "
                            "test whose name holds `crash` (make the second save fail, then check the stored state "
                            "is consistent and a retry applies the change exactly once)")


def check_boundaries(bounds, rows, problems, alabel):
    """Every function that reads untrusted input, and every HTTP route with a body, has a negative acceptance test."""
    by_id = {r.get("rid"): r for r in rows}
    for fid in bounds[1]:
        if fid not in by_id:
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


def check_policies(text, mods, rows, problems, alabel):
    """One owner per cross-cutting policy, tests for concurrent writers and boundaries, one home per domain rule."""
    pol, lists, rules = parse_policies(text)
    check_owners(pol, mods, problems, alabel)
    if "concurrent writers" in lists:
        check_writers(pol, lists["concurrent writers"], rows, problems, alabel)
    else:
        problems.append(f"{alabel}: ## Policies has no `Concurrent writers:` line (write - for none)")
    if "multi-store writes" in lists:
        check_crash_safe(lists["multi-store writes"], rows, problems, alabel)
    else:
        problems.append(f"{alabel}: ## Policies has no `Multi-store writes:` line (write - for none)")
    if "boundaries" in lists:
        check_boundaries(lists["boundaries"], rows, problems, alabel)
    else:
        problems.append(f"{alabel}: ## Policies has no `Boundaries:` line (write - for none)")
    if rules is None:
        problems.append(f"{alabel}: ## Policies has no `Domain rules: <rule> -> <module>.<function>; ...` line "
                        "(write - for none)")
    else:
        check_rules(rules, mods, problems, alabel)
    check_reuse(mods, rules, problems, alabel)


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


def validate(repo, run=False, timeout=600, notes=None):
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

    arch_file = repo / paths["architecture"]
    mods = None
    if not arch_file.is_file():
        problems.append(f"{paths['architecture']}: missing (ARCHITECTURE)")
    else:
        mods = parse_architecture(arch_file.read_text(encoding="utf-8", errors="replace"))
        if not mods:
            problems.append(f"{paths['architecture']}: no `## Module <name>` sections")
        check_architecture(mods, problems, paths["architecture"])

    fun_file, rows = repo / paths["functions"], []
    if not fun_file.is_file():
        problems.append(f"{paths['functions']}: missing")
    else:
        rows = parse_functions(fun_file.read_text(encoding="utf-8", errors="replace"), problems, paths["functions"])
        check_rows(rows, mods, repo, problems, paths["functions"])
        check_cargo_targets(rows, repo, problems, paths["functions"])
        problems += entry_match.entry_problems(
            repo, [r for r in rows if r["entry"].strip().strip("`").strip().lower() not in NO_ENTRY], paths["functions"])
        check_needs(rows, problems, paths["functions"])
        check_control_chars(repo, problems)
        problems += entry_match.cross_problems(repo, rows, paths["functions"], waits_on_fn(rows))
        check_stubs(rows, repo, problems, paths["functions"])
        check_property(rows, cfg, repo, problems, paths["functions"])
        depth, width, _ = check_depth(rows, cfg, problems, paths["functions"])
        if notes is not None:
            notes.append(f"{paths['functions']}: critical path {depth} functions, widest level {width}")
    if arch_file.is_file():
        check_policies(arch_file.read_text(encoding="utf-8", errors="replace"), mods, rows, problems,
                       paths["architecture"])
    if "lint" in cmd:
        before = len(problems)
        check_ruff(repo, cmd["lint"], problems)
        check_clippy(cmd["lint"], problems)
        if len(problems) == before:  # a lax lint config is the problem to fix first
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
        for r in rows:
            for item, name in r.get("tests", []):
                node = f"{item.split('::')[0]}::{name}"  # {test} = the bare test name, {stem} = the file stem
                code, tail = run_cmd(drive.land_mod.acceptance_cmds(cmd["acceptance"], [node])[0][1], repo, timeout)
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="overhaul v2 plan validator")
    ap.add_argument("--repo", required=True, help="the rebuild repo root")
    ap.add_argument("--run", action="store_true", help="also run every acceptance test: non-proven must fail, proven must pass")
    ap.add_argument("--timeout", type=int, default=600, help="seconds allowed per command (default 600)")
    a = ap.parse_args(argv)
    if not Path(a.repo).is_dir():
        print(f"plan_validate: --repo {a.repo} is not a directory", file=sys.stderr)
        return 2
    notes = []
    problems, n = validate(a.repo, a.run, a.timeout, notes)
    for note in notes:
        print(note, file=sys.stderr)  # stderr: stdout stays problem lines plus the PLAN line
    for p in problems:
        print(p)
    print(f"PLAN FAIL {len(problems)} problems" if problems else f"PLAN OK {n} functions")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
