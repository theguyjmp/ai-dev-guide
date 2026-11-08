---
name: overhaul-plan
description: Plan a ground-up rebuild of an existing project as a new <name>-rebuild repo of checked contracts - spec/FUNCTIONS.md (one row per user-visible behaviour), an acyclic spec/ARCHITECTURE.md, acceptance tests written now that drive only the public entry and fail, and overhaul.toml with the real toolchain - done only when plan_validate.py prints PLAN OK. Use when the owner asks to overhaul, rewrite or re-plan a whole project, after overhaul-check returns OVERHAUL, or when overhaul-build's drive.py exits 3 with a needs-plan function to split or fix.
---

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
with `pythonpath = ["src", "tests/acceptance"]` and `addopts = "--import-mode=importlib"` (repeated test file names
never collide). Never copy old code into the rebuild.

## Step 3. spec/FUNCTIONS.md
plan_validate.py checks the table in exactly this form:
```
FUNCTIONS.md table: `| id | phase | module | deps | entry | acceptance | status |`
  phase = integer from 1; deps = comma-separated ids or `-`; entry = the public CLI command, route or UI action;
  acceptance = `tests/acceptance/<f>.py::<test>`, `;`-separated; status = planned|building|review|landing|proven|needs-plan.
  Optional `files` column before status: the function's own files, comma-separated, inside its module's Paths.
  Optional `needs`: `stub` (default: builds against the dep's stub at once), `behaviour`, or `F03:behaviour, F04:stub`.
```
- files: give each function its own file(s) (`src/shop/cart/add.py`); siblings build at once only when their files do
  not overlap. A file every function edits (an `__init__.py` export list, a registry) goes on the module's `Shared:`
  line, never in a files cell. Without the column a function locks its whole module.
- id: `F01`, `F02`, ... unique, never reused. Phase 1 is the smallest set that makes the product usable end to end
  (under 3k lines, use 1 or 2 phases).
- module: one from ARCHITECTURE. deps: each in the same or an earlier phase, and only the functions that build an API
  item this one's code calls; shared types, the store and errors come from the foundation, never from a sibling. Over
  the depth limit, move what siblings share into the foundation; never drop a real call.
- entry: the public way a user does it: `cli: shop cart add <sku> <qty>`, `http: POST /cart/items`,
  `ui: /cart, click Add`. Never an internal function.
- acceptance: `tests/acceptance/<file>.py::test_<id>_<what>`, the id in lower case so `-k` picks only it.
- status: write `planned`; only land.py writes `proven`, in its land commit.

A function is one user-visible behaviour a builder can finish in about 300 lines or fewer, unit tests included;
split bigger ones by sub-behaviour (`report --csv`, `report --pdf`). Helpers, refactors and infrastructure are never
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
- `lines() -> list[Line]` [F03]
- `register(sub)` adds the `cart` commands [F03, F04]
```
- One `## Policies` section [by plan_validate.py], each line `<owner module> - <policy>`: `Concurrency:` (server
  model, what locks), `Persistence:` (the ONE module that reads and writes stored state; every non-temp write, exports
  too, is atomic: unique temp file then replace), `Errors:` (typed errors, how the entry reports them, what is logged),
  `Untrusted input:` (HTTP framing: Content-Length missing, negative or malformed gives 400, over the body cap 413;
  files read from disk checked for encoding and schema, damage reported as corrupt; CSV and other exports neutralise
  formula injection), `Retries:` (bounded, with backoff). Then `Concurrent writers:` (each a `test_<f>_concurrent`), `Boundaries:`
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

## Step 5. tests/acceptance, written now, failing now
- `tests/acceptance/_entry.py` is the only door into the product: `run_cli(data, *args)` runs `sys.executable -m
  <pkg>`; `server(data)` starts `-m <pkg> serve --port <free port>`, yields the base URL once `/healthz` answers and
  fails at once if the process exits; UI tests drive that URL with Playwright. Rust or JS: the same, via its binary.
- One test per function, named as in its row; more cases are `@pytest.mark.parametrize` cases of that test.
- Shared fixtures live once in `tests/conftest.py` (copies fail shape_check.py). Each test uses its own `tmp_path` data dir, asserts what the user sees (output, exit code, HTTP body, page text)
  plus at least one error case, and reads results back only through its own entry or a dep's entry.
- No mocks, fakes, monkeypatching or product imports. For an outside service the test cannot reach, assert up to
  the product's boundary through a public entry and add one DECISIONS line.
- It fails because the product is missing, never a broken test: `pytest --collect-only -q tests` exits 0. Each function
  also gets 1 to 3 Hypothesis `@given` tests in `tests/property/test_<id>.py`, red now; planned tests are anchors.

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
Rust: `cargo clippy -- -D warnings`, `cargo test`, `cargo metadata`; JS: `npx eslint .`, `npx jscpd src`, `node --test`.
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
Read only its row, `.overhaul/reviews/<id>.txt` and its `.overhaul/log.jsonl` lines, never the diff. Do one, add one
DECISIONS line, pass Step 7, make a plan commit on main touching only spec/, tests/acceptance/ and
overhaul.toml, then run `drive.py --repo <rebuild> --reset <id>` and restart drive.py:
1. Split it: new ids with one new failing test each; update deps and OLD-ENTRIES lines naming it; drop its row and test.
2. Fix its acceptance test, only where it contradicts OLD-ENTRIES or DECISIONS, never to make it easier.
3. Fix the plan: add the missing API item or dependency, or move the function to the module that owns the code.
Acceptance already green before the change: strengthen the test in a plan commit or, if a dep truly delivered
it, have a blind reviewer confirm and run `drive.py --repo <rebuild> --mark-proven <id> --evidence <review file>`.

## What good looks like (a CLI app; F02 is `shop product add`, F03 is `shop cart show`)
`| F04 | 1 | cart | F02,F03 | cli: shop cart add <sku> <qty> | tests/acceptance/test_cart.py::test_f04_cart_add | planned |`
```python
# tests/acceptance/_entry.py: the only door from a test into the product
import os
import subprocess
import sys
from pathlib import Path
SRC = Path(__file__).resolve().parents[2] / "src"
def run_cli(data, *args):
    env = {**os.environ, "PYTHONPATH": str(SRC), "SHOP_DATA": str(data)}
    return subprocess.run([sys.executable, "-m", "shop", *args], cwd=data, env=env,
                          capture_output=True, text=True, timeout=60, check=False)
# tests/acceptance/test_cart.py
from _entry import run_cli
def test_f04_cart_add(tmp_path):
    assert run_cli(tmp_path, "product", "add", "MUG1", "Mug", "4.50").returncode == 0
    added = run_cli(tmp_path, "cart", "add", "MUG1", "2")
    assert added.returncode == 0, added.stderr
    cart = run_cli(tmp_path, "cart", "show").stdout  # read back through dep F03
    assert "MUG1" in cart and "9.00" in cart
    bad = run_cli(tmp_path, "cart", "add", "NOPE", "1")
    assert bad.returncode != 0 and "unknown sku" in bad.stderr.lower()
```
Good: only public commands of F04 and its deps, its own data dir, what the user sees plus an error case, red today
(`shop` does not exist), and `-k test_f04_cart_add` runs exactly it. Bad: importing `shop.cart`, a mocked store, or
checking only the exit code.
## Rules
- The plan is done only at PLAN OK: every function has a public entry, an acceptance test that exists and fails now, and deps in the same or an earlier phase [by plan_validate.py --run].
- Acceptance tests reach the product only through `_entry.py`: no product imports, mocks or fakes [by entry_lint.py].
- The module graph is acyclic and modules import each other only through `Public:` names [by check_cycles.py].
- Each cross-cutting policy has one owner, each domain rule one function, every concurrent writer a concurrent test and every boundary a negative test [by plan_validate.py].
- Lint enforces line length and the strict set, no 20+ char literal is written twice, functions stay within 30 lines, constants sit at the top and no fixture is copied [by plan_validate.py, dup_literals.py and shape_check.py].
- With `needs`, the plan commit lands a stub per function in its files (signatures, types, docstrings, bodies raising NotImplementedError) that imports; the longest chain of blocking (`behaviour`) deps is at most max(4, ceil(N/4)), or `[plan] max_depth` [by plan_validate.py].
- A function counts only when its test is red on main, green after the merge, and every proven test stays green [by land.py].
- Builders never edit tests/acceptance or leave their module's Paths [by drive.py's brief and the blind review].
