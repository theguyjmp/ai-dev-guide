---
name: overhaul-plan
description: Plan a ground-up rebuild of a project as a new <name>-rebuild repo of checked contracts (FUNCTIONS, an acyclic ARCHITECTURE, failing acceptance tests through the public entry, overhaul.toml), done only at PLAN OK. Use when the owner asks to overhaul, rewrite or re-plan a whole project, after overhaul-check returns OVERHAUL, or when drive.py exits 3 with a needs-plan function.
---
> **RULE ZERO (the most important rule, ever):** the work must be valuable, well made, clean code that can be built
> out and extended. A very solid foundation beats functionality depth: missing depth is acceptable, a weak foundation
> is not. One owner per capability, typed data models, stable ids, clear extension seams, no second copy of anything;
> never trade code quality, tests or gates for speed or feature count. Every brief this skill writes for an agent
> quotes this rule at its top.


# overhaul-plan

The plan is four things that scripts check, not prose: FUNCTIONS (what to build), ARCHITECTURE (where it goes),
acceptance tests (when it is done) and overhaul.toml (how it is checked). overhaul-build counts a function only when
its acceptance test goes red to green through land.py, so every test exists before any code. Size follows the
project: one function per user-visible behaviour, one module per domain or data store; a 2k-line project plans in
under 45 minutes into 10 to 30 functions and 2 to 5 modules, never toward a target. plan_validate.py is the audit.

`<old>` is the old project, `<rebuild>` is `<old>-rebuild` beside it, `<pkg>` the product's package or command name,
`<overhaul-build>` the absolute path of the installed overhaul-build skill.

## Step 1. Measure the old project (read-only, about 10 minutes)
Reuse an overhaul-check report's sizes and duplication if one exists. Launch three read-only agents in ONE message
(Explore type, Sonnet is enough), each capped at 40 tool calls, returning at most 60 lines and never whole files:
1. Entries: every CLI command and flag, HTTP route (method and path) and UI action: `entry | file:line | what it does`.
2. Behaviours and data: per entry, inputs, output, errors and side effects; every data store (files, tables, env
   vars, config, outside services) with the entries that use it; user-visible fixes from `git log -i --grep=fix`.
3. Size and duplication: lines per top folder, duplicate blocks (pylint duplicate-code, jscpd), dead code, old tests.

## Step 2. Create the rebuild repo
`git init -b main <rebuild>`, same language and stack as `<old>` unless the owner says otherwise. Add `spec/`,
`src/<pkg>/__init__.py` (empty), `tests/acceptance/`, `tests/unit/test_smoke.py` importing `<pkg>`, a `.gitignore`
with the Python caches and `.overhaul/` (never committed), and `spec/OLD-ENTRIES.txt`, Step 1's entries one per line. `pyproject.toml` holds only `[tool.pytest.ini_options]`
with `pythonpath = ["src", "tests/acceptance"]` and `addopts = "--import-mode=importlib"` (repeated test file names never collide). Never copy old code into the rebuild.

## Step 3. spec/FUNCTIONS.md
plan_validate.py checks the table in exactly this form:
```
FUNCTIONS.md table: `| id | phase | module | deps | entry | acceptance | status | files | needs |`
  phase = integer from 1; deps = comma-separated ids or `-`; entry = the public CLI command, route or UI action;
  acceptance = `tests/acceptance/<f>.py::<test>` (Rust: `<f>.rs`), `;`-separated; status = planned|building|review|landing|proven|needs-plan.
  files = the function's own files (its stub), comma-separated, at most 3, inside its module's Paths. REQUIRED.
  needs = `stub` (default), `behaviour`, or `F03:behaviour, F04:stub` per dep. REQUIRED.
```
- files: give each function its own file(s) (`src/shop/cart/add.py`); siblings build at once only when their files do
  not overlap. A file every function edits (an `__init__.py` export list, a registry) goes on the module's `Shared:`
  line, never in a files cell. Each function's files hold its stub from the plan commit (Step 5).
- needs: write `stub`: the dependent builds at once against the dep's stub. `behaviour` only for a dependent whose
  acceptance test needs the dep's real output; the chain of `behaviour` edges is capped by the depth limit.
- id: `F01`, `F02`, ... unique, never reused. Phase 1 is the smallest set that makes the product usable end to end (under 3k lines, use 1 or 2 phases).
- module: one from ARCHITECTURE. deps: each in the same or an earlier phase, and only the functions that build an API
  item this one's code calls; shared types, the store and errors come from the foundation, never from a sibling. Over
  the depth limit, move what siblings share into the foundation; never drop a real call.
- entry: the public way a user does it: `cli: shop cart add <sku> <qty>`, `http: POST /cart/items`,
  `ui: /cart, click Add`. Never an internal function.
- acceptance: `tests/acceptance/<file>.py::test_<id>_<what>`, the id in lower case so `-k` picks only it.
- status: write `planned`; only land.py writes `proven`, in its land commit.

A row is one function or one verb behaviour a builder finishes in about 50-150 lines in 1-3 files, unit tests
included; split bigger ones by sub-behaviour (`report --csv`, `report --pdf`). Every capability a row calls exists on main (its line in the generated `spec/index/<module>.md`) or
in a dep row; when it does not, add that row first, in the module that owns it. Helpers, refactors and infrastructure are never
rows of their own: F01, the smallest entry that proves the product starts (`shop --help` exits 0, or `GET /healthz`
answers 200), is the foundation and builds the shared types, store, errors and entry dispatch; if that is too big, the
first data behaviour (`shop product add`) carries the domain types as a second foundation. Every function reaches a
foundation through deps. Old entries that do the same thing become one function. Map each
OLD-ENTRIES.txt line with ` -> F04` (or ` -> F04,F05`), or ` -> D3` when decision D3 drops it, until
`grep -vE -- '-> [FD][0-9]+(,[FD][0-9]+)*$' spec/OLD-ENTRIES.txt` prints nothing.

## Step 4. spec/ARCHITECTURE.md
check_cycles.py states the format (`py -3 <overhaul-build>/scripts/check_cycles.py --help`):
```
ARCHITECTURE.md format: one section per module; any other line in it (public API, notes) is free text.
    ## Module <name>
    Paths: src/shop/core/, src/shop/money.py
    Depends on: base, util
    Public: shop.core, shop.money.cents
Paths: repo-relative, comma-separated; a trailing / is a directory; * ? [ make a glob; a file belongs to the module
whose matching path is the longest. Depends on: the modules it may import, comma-separated; - or none for none.
Public (optional, Python): the dotted modules and names other modules may import; a whole module listed allows every
name in it. Without it, a module's public face is the __init__.py of each Paths directory and each Paths file, and an
__init__.py with a literal __all__ exports only those names.
```
A section may add `Shared: src/shop/core/__init__.py`: files any function of the module may edit that never lock a
build (drive.py); a clash there goes to the conflict-fix pass.
For example (`Public:` is checked: other modules import only these names; Owns and the API bullets are free text):
```
## Module cart
Paths: src/shop/cart/
Depends on: app, catalog
Public: shop.cart.add_line, shop.cart.lines, shop.cart.register
Owns: the cart table.
- `add_line(sku: str, qty: int) -> Line` [F04]
  Packet: edit src/shop/cart/add.py after `class Line`; calls catalog.find_sku, money.cents; test py -3 -m pytest tests/acceptance/test_cart.py -k test_f04_cart_add
- `lines() -> list[Line]` [F03]
  Packet: edit src/shop/cart/show.py after `def _fmt`; calls -; test py -3 -m pytest tests/acceptance/test_cart.py -k test_f03_cart_show
- `register(sub)` adds the `cart` commands [F03, F04]
```
- Work packet [by plan_validate.py]: each row's API bullet (exact signature, `[<id>]`) has an indented `Packet:` line
  (`Packet F04:` on a shared bullet): `edit <file> after <symbol>` per file (only its `files` cell), `calls` the helpers
  it reuses as `spec/index/<module>.md` names them (`-` none), `test` its acceptance command. Proven rows only warn.
- One `## Policies` section [by plan_validate.py], each line `<owner module> - <policy>`: `Concurrency:` (server
  model, what locks), `Persistence:` (the ONE module that reads and writes stored state; every non-temp write, exports
  too, is atomic: unique temp file then replace), `Errors:` (typed errors, how the entry reports them, what is logged),
  `Untrusted input:` (HTTP framing: Content-Length missing, negative or malformed gives 400, over the body cap 413;
  files read from disk checked for encoding and schema, damage reported as corrupt; CSV and other exports neutralise
  formula injection), `Retries:` (bounded, with backoff). Then `Concurrent writers:` (each a `test_<f>_concurrent` with readers running alongside the writers), `Multi-store writes:` (every function whose one operation saves more than once; each a `test_<f>_crash...` that makes the second save fail, then checks the stored state is consistent and a retry applies the change once), `Boundaries:`
  (every function reading untrusted input; each a negative acceptance test, e.g. `test_<f>_rejects_malformed`) and
  `Domain rules: <rule> -> <module>.<function>; ...`: each calculation or validation rule (value = qty * price, qty must be positive) lives in that one function and all others call it.
  `Public:` lists each domain rule home and each API bullet naming a dependent module [by plan_validate.py].
- Policies opens with `Standard: overhaul-build/references/quality-standard.md`, the one bar every step checks by
  rule number. Decide the design rules here: stored invariants and atomic or recoverable multi-store writes (Q29-Q32)
  in Persistence, stale locks and network timeouts (Q34, Q35) in Concurrency, errors mapped by type (Q19, Q20).
- Unit tests for module m go in `tests/unit/m/`. Each data store has one owning module; the others use its API.
- Every module owns a function. The graph is acyclic: code two modules need lives in the lower one or the foundation.
- Entry wiring is discovered, never edited: the entry module (F01) finds each module's `register(sub)` or HTTP router
  by scanning the package, so builders stay in their Paths; its section gives the start command and env vars.

## Step 5. Scaffold, then stubs and tests in parallel (failing now)
Write the shared scaffold yourself, after Steps 3, 4 and 6: the package skeleton (`__init__.py`, `__main__.py`, entry
dispatch), `tests/conftest.py` holding every shared fixture once (copies fail shape_check.py), and:
- `tests/acceptance/_entry.py` is the only door into the product: `run_cli(data, *args)` runs `sys.executable -m
  <pkg>`; `server(data)` starts `-m <pkg> serve --port <free port>`, yields the base URL once `/healthz` answers and
  fails at once if the process exits; UI tests drive that URL with Playwright. Rust or JS: the same, via its binary.
Then fan out ONE subagent per module, all in a SINGLE message of parallel Agent calls (model sonnet), each briefed
from `templates/module-brief.md` (beside this file) filled with that module's rows only; it reads ARCHITECTURE and
writes the module's stubs (every function's files: signatures, types, docstrings, bodies raising NotImplementedError,
importable), acceptance tests (`templates/acceptance-example.md`) and 1 to 3 Hypothesis tests per function in
`tests/property/test_<id>.py`. Never write them serially yourself; the fan-out targets under 10 minutes. Then run Step 7
once and fix what fails. An outside service a test cannot reach: assert up to the product's boundary through a
public entry and add one DECISIONS line. Planned tests are anchors.

## Step 6. overhaul.toml
The commands are the gates land.py runs on the whole tree. Python, with `<overhaul-build>` filled in:
```toml
[commands]
test       = "py -3 -m pytest -q tests/unit"
acceptance = "py -3 -m pytest -q tests/acceptance -k {test}"
lint       = "py -3 -m ruff check --line-length 88 --select E,W,F,I,B,UP,SIM,C4,RET,PIE,RUF,C90,ARG,A,ANN201,PLR0915 ."
dup        = "py -3 -m pylint --disable=all --enable=duplicate-code --min-similarity-lines=6 --recursive=y src"
literals   = "py -3 <overhaul-build>/scripts/dup_literals.py src"
shape      = "py -3 <overhaul-build>/scripts/shape_check.py src tests"
cycles     = "py -3 <overhaul-build>/scripts/check_cycles.py src"
entry_lint = "py -3 <overhaul-build>/scripts/entry_lint.py tests/acceptance"
[harness]
max_parallel = 6
[limits]
builder_max_calls = 60
brief_max_tokens = 40000
retries_per_function = 1
[probe]  # paths default to spec/FUNCTIONS.md, spec/ARCHITECTURE.md, .overhaul/state.json
mutation_floor = 0.9
max_mutants = 40
```
Optional keys (tiers, extra lints, a rules column, deferred rows, plan checks, ROADMAP) and the Rust and JS commands:
`templates/overhaul-toml.md`, read for the parts the project needs.
Install what is missing now (`py -3 -m pip install ruff pylint pytest`); every command but acceptance exits 0 on the skeleton.

## Step 7. Validate until PLAN OK
Run `py -3 <overhaul-build>/scripts/plan_validate.py --repo <rebuild> --run`, fix the first reported line, repeat.
Usual fixes: move a function to its dep's phase; move what two modules need into the lower one or the foundation;
drive through `_entry.py`; a test that passes now asserts nothing. Never loosen a check, skip or xfail a test to reach
PLAN OK. Same failure three times: split or merge the function, with a DECISIONS line.
## Step 8. Commit and report
`git add -A` and `git commit -m "plan: N functions, M modules, PLAN OK"` on main; report three lines: the `<rebuild>`
path, `PLAN OK, N functions, M modules`, and next: run overhaul-build (its dash.py writes `dash/index.html`).
## spec/DECISIONS.md
One numbered line per decision, no ceremony: `D3: drop export-xml; nothing reaches it.` One for every dropped old
entry, deliberate behaviour change, dependency added after the plan, and acceptance test changed after the plan.
## Re-plan a needs-plan function (drive.py exited 3)
Follow `templates/replan.md`. A row parked oversized comes back split, never tweaked; every row you add keeps the row size
and its work packet, and main moves only at PLAN LAND OK.

A good acceptance test, with what makes it good or bad: `templates/acceptance-example.md`.
## Rules
- The plan is done only at PLAN OK: every function has a public entry, an acceptance test that exists and fails now, and deps in the same or an earlier phase [by plan_validate.py --run].
- Every row is one function or one verb behaviour in at most 3 files, and its ARCHITECTURE bullet carries a work packet (files with the symbol to edit after, exact signature, helpers to call from spec/index, test command); every capability it calls exists on main or in a dep row [by plan_validate.py].
- Acceptance tests reach the product only through `_entry.py`: no product imports, mocks or fakes [by entry_lint.py].
- The module graph is acyclic and modules import each other only through `Public:` names [by check_cycles.py].
- Each cross-cutting policy has one owner, each domain rule one function, every concurrent writer a concurrent test and every boundary a negative test [by plan_validate.py].
- Lint enforces line length and the strict set, no 20+ char literal is written twice, functions stay within 30 lines, constants sit at the top and no fixture is copied [by plan_validate.py, dup_literals.py and shape_check.py].
- The plan commit lands a stub per function in its required `files` cell (signatures, types, docstrings, bodies raising NotImplementedError) that imports; the longest chain of blocking (`behaviour`) deps is at most max(4, ceil(N/4)), or `[plan] max_depth` [by plan_validate.py].
- No plan commit moves main of a project with proven rows until `plan_land.py --repo R --branch B` prints PLAN LAND OK: PLAN OK, dup and literals, and every proven acceptance test the commit's files can break, green on B merged with main [by plan_land.py, run by supervise.py and by the planner]. A planner moves main only with `plan_land.py --repo R --branch B --ff` (B rebased onto main), never `git merge --ff-only`, so the guard judges the ref before any file changes [by plan_land.py].
- A function counts only when its test is red on main, green after the merge, and every proven test stays green [by land.py].
- Builders never edit tests/acceptance or leave their module's Paths [by drive.py's brief and the blind review].
