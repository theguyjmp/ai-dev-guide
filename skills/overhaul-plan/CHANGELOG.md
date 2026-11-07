# overhaul-plan changelog

## v2 one quality standard, 2026-11-07
The Policies section opens with a `Standard:` line citing overhaul-build's quality-standard.md, and the design
rules trial 4's judge found missing (stored invariants, atomic multi-store writes, stale locks, network timeouts,
errors mapped by type) are decided in Persistence, Concurrency and Errors. plan_validate.py now fails a row whose
acceptance tests do not invoke the exact entry it names (trial 4's F15).

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
