# overhaul-plan changelog

## Rust plans, 2026-11-08
Step 6 gives the Rust command set and layout (acceptance in tests/acceptance/<f>.rs as root `[[test]]` targets
named by stem, `{stem}` in the commands, `todo!()` stubs, in-crate unit tests, proptest property files, no
conftest.py); the FUNCTIONS format accepts `<f>.rs`; module-brief.md carries the Rust stub, test and proptest wording.

## v3 parallel plan: stubs and needs required, 2026-11-07
Trial 6: one chat wrote 23 functions' tests serially (22 minutes), and left out the optional `needs`/`files`
columns, so no stubs landed and every dep blocked (3 of 6 builders ran). Now `files` and `needs` are required
columns and every unproven row needs an importable stub; plan_validate fails without them (needs defaults to
`stub`; `behaviour` only when the acceptance test needs the dep's real output). Step 5 writes the shared scaffold
(package skeleton, conftest.py, _entry.py) in the main chat, then fans out one sonnet subagent per module in one
message from templates/module-brief.md for that module's stubs, acceptance and property tests; target under 10
minutes for about 20 functions.
## v3 speed: stubs and needs, 2026-11-07
The plan commit lands an interface stub for every function (signatures, types, docstrings, bodies raising
NotImplementedError) in its own files. Optional FUNCTIONS `needs` column: `stub` (default, the dependent builds
against the stub at once), `behaviour` (wait for the dep to land), or per dep `F03:behaviour, F04:stub`.
plan_validate checks the cell, that each unproven row's stub files exist, raise NotImplementedError and import, and
computes the critical path over blocking (behaviour) edges only. Plans without the column are unchanged.
## v3 probe, 2026-11-07
Each function gets 1 to 3 Hypothesis property tests in tests/property/test_<id>.py, written and red at plan time
beside its acceptance tests. plan_validate.py fails a Python plan without them ([probe] property_tests = false turns
it off) and, with --run and a [commands].property template, checks they are red before the build. Planned tests are
anchors. overhaul.toml gains [probe] mutation_floor and max_mutants.

## v2 one quality standard, 2026-11-07
The Policies section opens with a `Standard:` line citing overhaul-build's quality-standard.md, and the design
rules trial 4's judge found missing (stored invariants, atomic multi-store writes, stale locks, network timeouts,
errors mapped by type) are decided in Persistence, Concurrency and Errors. plan_validate.py now fails a row whose
acceptance tests do not invoke the exact entry it names (trial 4's F15).
## v2 depth, 2026-11-07
plan_validate prints the critical path (longest deps chain) and the widest level, and fails a chain longer than
max(4, ceil(N/4)) unless overhaul.toml sets `[plan] max_depth`. SKILL: F01 is the foundation (shared types, store,
errors, entry dispatch; a second foundation may carry the domain types), deps name only functions whose API items the
code calls, and each function gets its own files. Trial 4's plan was 7 deep (floor 29.8 of 41.7 min); with the
foundation pattern the same 21 functions plus one foundation estimate 4 deep, widest level 16.

## v2 speed, 2026-11-07
FUNCTIONS.md may carry a `files` column (each function's own files) and a module a `Shared:` line, so drive.py builds
one module's functions at once; in trial 4 the one-function-per-module lock cost about 12 of 42 minutes. overhaul.toml
sets `[harness] max_parallel = 6`.

## v2 waste fixes from trial 3's pass count, 2026-11-07
A module with a `Public:` line lists on it every helper other modules are told to reuse (each domain rule home and each
API bullet naming a dependent module); plan_validate fails it otherwise. In trial 3 the missing `inventory.whole` and
stock total cost F10 and F15 two builder passes each.

## v2 quality fixes 2 from trial 3's blind audit, 2026-11-07
The Policies section now also requires `Untrusted input:` (HTTP framing 400/413 with a body cap, files checked for encoding and schema, export formula injection neutralised, atomic non-temp writes), `Retries:` (bounded, with backoff), `Boundaries:` (each listed function and each HTTP route with a body needs a negative acceptance test) and `Domain rules: <rule> -> <module>.<function>` (one home per rule). tests/conftest.py is required and scaffolded; the toml template adds the `shape` command and the wider ruff set.

## v2 quality fixes from trial 1's blind audit, 2026-11-07
Trial 1's rebuild scored 8/10 for concurrency-unsafe persistence, two error styles, imports past a module's API, repeated message literals and 76 long lines. The plan now declares a `## Policies` section (Concurrency, Persistence, Errors, one owner module each; Concurrent writers with a `test_<f>_concurrent` acceptance test), a `Public:` line per module (enforced by check_cycles.py), a strict ruff lint with line length 88, and the `literals` gate (dup_literals.py); plan_validate.py checks all of it.


v1 (2026-11-05 to 2026-11-07) planned a rebuild in prose: inputs and permissions first, measurement of the old project and the old fleet, history turned into data, a quarantine of old code, a spec of fixed files capped at 5,200 words, auditor templates, a published progress dashboard, Opus audit, Sonnet verify and bottleneck rounds, a scripted consistency check, and plan_validate.py with `--counting` and an opt-in `--layers` rule. On the pilot project the plan itself held up, but progress was not tied to functions: proven stayed at 4 for about 8 hours while about 60 packets merged, because finished functions were not counted until their tests were named, and each caller or overlap grant cost a protected batch and a ruling (about 20 batches and 48 rulings in one day). v2 replaces the prose with four checked artifacts (a FUNCTIONS table, an acyclic ARCHITECTURE, failing acceptance tests written at plan time, overhaul.toml) and ends at plan_validate.py's PLAN OK; the dashboard publishing, audit rounds and rulings are gone, decisions are one line each in spec/DECISIONS.md, later fixes are plan commits, and the ownership model stays.
