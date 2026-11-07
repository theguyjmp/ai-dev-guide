"""plan_validate.py: the plan gate of overhaul v2. A plan is not done, and drive.py must not start, until this passes.

  py -3 plan_validate.py --repo R [--run] [--timeout 600]

Reads R/overhaul.toml, spec/FUNCTIONS.md and spec/ARCHITECTURE.md (paths from [paths], else those defaults) and prints one
line per problem, then `PLAN OK n functions` (exit 0) or `PLAN FAIL k problems` (exit 1). Exit 2 is bad usage. It writes nothing.

FUNCTIONS.md table: `| id | phase | module | deps | entry | acceptance | status |`
  phase = integer from 1; deps = comma-separated ids or `-`; entry = the public CLI command, route or UI action;
  acceptance = `tests/acceptance/<f>.py::<test>`, `;`-separated; status = planned|building|review|landing|proven|needs-plan.
ARCHITECTURE.md: `## Module <name>` sections, each with `Paths:` and `Depends on:` (`-` or `none` = none).
overhaul.toml [commands] must hold test, acceptance (with a `{test}` placeholder), lint, dup, cycles and entry_lint.

Problems (each is one line):
  - a row is malformed (cells, id, phase, deps, acceptance, status, duplicate id);
  - an acceptance file or its `def <test>` is missing;
  - deps are unknown, point to a later phase, or form a cycle;
  - a module is unknown, ARCHITECTURE is missing, or its module graph is broken (no Paths/Depends on, unknown module, cycle);
  - a function has no entry;
  - an overhaul.toml command is missing;
  - the cycles or entry_lint command exits non-zero (run from the repo root, through overhaul.toml);
  - with --run: a non-proven function's acceptance test PASSES (it must be red before the build), or a proven one FAILS.
Python standard library only.
"""
import argparse
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

COLUMNS = ["id", "phase", "module", "deps", "entry", "acceptance", "status"]
STATUSES = {"planned", "building", "review", "landing", "proven", "needs-plan"}
COMMANDS = ["test", "acceptance", "lint", "dup", "cycles", "entry_lint"]
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ACCEPT = re.compile(r"^tests/acceptance/[\w./-]+\.py(::[A-Za-z_]\w*)+$")
NO_ENTRY = {"", "-", "--", "none", "n/a", "na", "tbd", "todo", "?"}
SEPARATOR = re.compile(r"^:?-+:?$")
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
    rows, header_at, lines = [], None, text.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|") and [bare(c).lower().strip("*") for c in cells(line)] == COLUMNS:
            header_at = i
            break
    if header_at is None:
        problems.append(f"{label}: no table with the header | {' | '.join(COLUMNS)} |")
        return rows
    i = header_at + 1
    if i < len(lines) and lines[i].lstrip().startswith("|") and all(SEPARATOR.match(c) for c in cells(lines[i])):
        i += 1
    if i >= len(lines) or not lines[i].lstrip().startswith("|"):
        problems.append(f"{label}: no function rows")
    for n in range(i, len(lines)):
        if not lines[n].lstrip().startswith("|"):
            break
        c = cells(lines[n])
        if len(c) != len(COLUMNS):
            problems.append(f"{label}:{n + 1}: row has {len(c)} cells, want {len(COLUMNS)}")
            continue
        row = dict(zip(COLUMNS, c))
        row["line"] = n + 1
        rows.append(row)
    return rows


def parse_architecture(text):
    """{module: {"paths": [...] or None, "deps": [...] or None, "line": n}} from the `## Module <name>` sections."""
    mods, cur = {}, None
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^##\s+Module\s*:?\s+(\S+)", line, re.I)
        if m:
            cur = mods.setdefault(m.group(1).strip("`:*"), {"paths": None, "deps": None, "line": n})
            continue
        if re.match(r"^#{1,2}\s", line):
            cur = None
        if cur is None:
            continue
        m = re.match(r"^[\s>*-]*\**\s*(Paths|Depends on)\s*:\s*\**\s*(.*)$", line, re.I)
        if m:
            items = [x.strip().strip("`").strip() for x in re.split(r"[,;]", m.group(2).strip().strip("*")) if x.strip().strip("`").strip()]
            if m.group(1).lower() == "paths":
                cur["paths"] = items
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
            elif not re.search(rf"^[ \t]*(?:async[ \t]+)?def[ \t]+{re.escape(parts[-1])}[ \t]*\(",
                               src.read_text(encoding="utf-8", errors="replace"), re.M):
                problems.append(f"{where}: {parts[0]} has no def {parts[-1]}")
            else:
                r["tests"].append((item, parts[-1]))
    for cyc in find_cycles(graph):
        problems.append(f"{flabel}: dependency cycle {' -> '.join(cyc)}")


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


def validate(repo, run=False, timeout=600):
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

    for k in ("cycles", "entry_lint"):
        if k in cmd:
            code, tail = run_cmd(cmd[k], repo, timeout)
            if code:
                problems.append(f"overhaul.toml: {k} command exited {code}" + (f": {tail}" if tail else ""))

    if run and "acceptance" in cmd:
        for r in rows:
            for item, name in r.get("tests", []):
                code, tail = run_cmd(cmd["acceptance"].replace("{test}", name), repo, timeout)
                where = f"{paths['functions']}:{r['line']} {r['rid']}"
                if r["st"] == "proven" and code:
                    problems.append(f"{where}: proven, but its acceptance {item} FAILS (exit {code})" + (f": {tail}" if tail else ""))
                elif r["st"] != "proven" and r["st"] in STATUSES and not code:
                    problems.append(f"{where}: {r['st']}, but its acceptance {item} already PASSES (it must be red before the build)")
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
    problems, n = validate(a.repo, a.run, a.timeout)
    for p in problems:
        print(p)
    print(f"PLAN FAIL {len(problems)} problems" if problems else f"PLAN OK {n} functions")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
