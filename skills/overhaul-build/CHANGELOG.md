# overhaul-build changelog

Why each rule exists, newest first. Full v1 history: `git log -- skills/overhaul-build` (71 commits, 2026-11-05 to 2026-11-07).

## v2 speed: a file lock instead of a module lock, 2026-11-07
Trial 4 built 21 functions in 41.7 min wall for 51.9 builder-minutes, 1.24 builders at once (runs/speed-fix-4.md).
The critical path of dependencies (build + review + land per function) was 29.8 min; the module lock added about 12:
functions spent 30.2 function-minutes ready but waiting on a busy module, against 251.5 waiting on dependencies.
- drive.py locks a function's files, not its module: two functions build at once unless their file sets overlap.
  The files come from an optional FUNCTIONS.md `files` column (plan_validate.py accepts it and checks each file is
  inside the module's Paths); without it a function claims its module's Paths, as before. A module's `Shared:` line
  (an `__init__.py` export list, a registry) never locks; a clash there goes to the existing conflict-fix pass.
- Polish fix passes start in parallel under the same rule (module Paths plus Shared must not overlap).
- A land's heartbeat (verdict.py, dash.py) runs after the loop launches the functions the land unblocked, not before:
  3 to 7 s per land on the critical path in trial 4.
- `[harness] max_parallel` defaults to 6 (`[limits] max_parallel` is still read when `[harness]` has none).

## v2 polish phase, 2026-11-07
Every gate passed in trial 3 and its blind audit still found 14 issues: the gates check what a rule names, and an auditor finds what no rule named. drive.py now ends with the same blind audit inside the harness (findings as JSON, file:line, module, rule and evidence), one fix builder per module, each reviewed and landed by `land.py --polish` (whole-tree gates, every proven row green, no FUNCTIONS row), then a re-audit, for `[limits] polish_rounds` rounds (default 2). verdict.py prints the final issue count and VERDICT GOOD requires 0.
## v2 waste fixes from trial 3's pass count, 2026-11-07
Trial 3 spent 29 builder passes on 23 functions; 6 were waste (runs/waste-3.md): F10 and F15 twice each on a review fix
outside their allowed paths, F09 once on a cycles failure first seen at land.
- plan_validate.py fails a plan whose reuse target is not on its owner's `Public:` line: each domain rule home, and each
  API bullet naming a module that depends on the owner ("orders parses quantities with it").
- drive.py sends a review FAIL straight to needs-plan, without another builder pass, when the reviewer writes a
  `PLAN-GAP:` line or says "outside allowed paths" about a diff that stayed inside them. The review brief asks for
  in-scope fixes only and a `PLAN-GAP:` line otherwise.
- drive.py runs the `cycles` command in the worktree before review (the builder gate); a failure is a failed attempt
  with the check's first lines in the retry brief, so a cycle or a missing Public name never reaches land.

## v2 quality fixes 2 from trial 3's blind audit, 2026-11-07
Trial 3 scored 8 6 9 9 6 8 6 with 14 issues. New gates: the ruff strict set adds ARG A ANN201 PLR0915; shape_check.py (the `shape` command, run by land.py and plan_validate) fails functions over 30 lines, UPPER_CASE constants after the first def or class (unless built from the defs above), and a pytest fixture copied into two test files; check_cycles.py fails a Public entry that reaches past its package face's `__all__` (trial 3's `tinyshop.inventory.stock.whole`) or names a _private item. The polish phase (in-harness blind audit and fix passes) is not built yet.

## v2 quality fixes from trial 1's blind audit, 2026-11-07
Trial 1's rebuild scored 8/10: an unlocked load-modify-save with a fixed temp-file name under a ThreadingHTTPServer (lost orders and 500s, while every acceptance test was sequential), two stores with different error handling, three imports past a module's public API, one message written 3 times and a re-implemented helper, and 76 lines over 88 columns. Each now has a mechanism:
- check_cycles.py fails a Python import into another module that misses its `Public:` names (or, without that line, its `__init__.py` face and `__all__`).
- dup_literals.py (new `literals` gate in land.py) fails a 20+ char string literal written in 2+ places outside tests.
- plan_validate.py requires `literals`, a ruff lint selecting E501 and the strict set E W F I B UP SIM RUF, a `## Policies` section (Concurrency, Persistence and Errors, each with one owner module) and a `Concurrent writers:` line whose functions each have a `concurrent` acceptance test; an HTTP write route must be listed unless the server is single-threaded.
- entry_lint.py allows threading and concurrent.futures, so acceptance tests can drive parallel clients.
- drive.py puts the Policies section in the build and review briefs; the review brief is a 7-item checklist (scope, real behavior, concurrency, errors, public API, no re-implementation after a search, readability).

## v2 trial 1 fixes, 2026-11-07
Trial 1 took 26 builds for 20 functions. 5 landings failed with "merge conflict merging main into fn/<id>" between parallel builders in one module, and F05 and F08 spent 3 passes each on it, because every conflict cost a whole attempt (a rebuild from main). Now:
- A function never launches while another function of its module (the FUNCTIONS `module` column) is in flight, so parallelism comes from different modules.
- A merge conflict at landing is not an attempt: a conflict-fix builder pass (`templates/conflict-brief.md`) merges main into the same worktree and branch, then the function is reviewed and landed again. `[limits] conflict_retries` (default 2, per attempt) bounds it; after that the failure counts as an attempt, and so does a conflict pass that itself fails.
- `[harness] builder_model` (default opus) and `review_model` (default sonnet) become `claude -p --model`; build, conflict and review events in `.overhaul/log.jsonl` name the model.

## v2, 2026-11-07: the harness
Rewritten around a deterministic harness: drive.py builds, blind-reviews and lands one function at a time, land.py alone changes main, and the manager only starts it, fixes needs-plan functions through plan_validate, runs a blind proof-check and reports. Design and contracts: `skills/OVERHAUL-V2-DESIGN.md`. Evidence: harness `notes/failure-analysis-2026-11-07.md` (F1-F10, R1-R12 and its Confidence section), `proof-check-2026-11-07.md`, `code-quality-2026-11-07.md`, `build-vs-buy-2026-11-07.md`.

Measured on the the pilot project v1 run, and what each changed:
- Proven sat at 4 for about 8 h while about 60 packets merged; 31 PASS PRs waited at 6 landings/h; one batch blamed 6 PASS PRs for 2 culprits. Now one function per builder, and a sequential land.py whose failure blames only its own branch.
- About 20 protected batches in one day, most for plan DATA, and 48 rulings. Now plan data changes only as a plan fix that passes plan_validate; there are no rulings or protected batches.
- Of 10 functions counted proven, 2 were REAL, 7 PARTIAL (real library code that nothing in the product called) and 1 a STAND-IN: "proven" meant a named test passed, not that the product does it. Now acceptance drives the public entry, red before and green after, and a blind proof-check closes the run.
- Code quality 6.5/10: lint saw only changed files (148 whole-tree findings on main), 18 of 41 sampled merges tested a stand-in, 5 owner pairs imported each other. Now standard tools check lint, duplication and cycles on the whole tree at every landing, and entry_lint bans test doubles in acceptance tests.
- The 12-hourly auditor never produced a result; builders hit the 130k context guard and left their brief's scope. Now there are no scheduled agents, and drive.py generates every brief under brief_max_tokens with its allowed paths.
- The master broke its own rules: inline prompts, hand-edited DISPATCH rows, a duplicate ruling and duplicate builds, 250 to 350 turns/h, 650k context, and times written ahead of the clock. Now the manager launches nothing but the proof-check, waits on one command, copies the VERDICT line and reads every time from the clock.

Corrections from the Confidence section stand: the the pilot project plan was sound and its ownership model is kept; the stall was a counting gap, not unfinished functions; F6's stale-worker cause is unproven.
Kept from the unlanded draft `wip/overhaul-build-2a`: every rule names its mechanism (`scripts/skill_lint.py`); the manager is the main chat, never a subagent; a launch prompt is only "Read and do <brief>."; status is four lines copied from measurements; the owner is asked nothing routine.
Removed: the six v1 references (`heartbeat.md`, `heartbeat-brief.md`, `loop.md`, `m0.md`, `red-fix-brief.md`, `rulings.md`), still at 86668be. Added: `references/proof-check.md`, `references/legacy.md`.

## v1, 2026-11-05 to 2026-11-07: the master-run fleet
A master agent ran builder, review, ruling and heartbeat agents, after an M0 that built the fleet's own machinery (health.py, a gate bot, a check CLI, tick checks, guards, wait.sh, a dashboard generator).
- 2026-11-05: created with overhaul-check and overhaul-plan; ownership guards, the AI-slop lint, the look-alike lint and the `find` lookup; no deadlines for agents (call caps, exit checks, progress gates); the master must be the main chat; a work-style block in every brief; run-4 and run-5 fixes (no reshaping code to dodge a lint, hook first, supersede); a bug and feedback button in M0; the bottleneck watch.
- 2026-11-06: never stuck on the owner; a daily skills check; the fleet stays full (FLEET-TARGET, the H21 idle red); a heartbeat every 30, then 20 minutes, running every check; the token burn tripwire and launch brake (burn.py, burn-guard.py), then "waste, not spend"; the permissions paste; one dashboard template page, never stale, with history; the master loop enforced by script (master-status.py, master-stop-guard.py) and SKILL.md cut from 40.9 KB to 16.4 KB; the outcome brake; the proof path (a full main gate on the bot's clock, deploy only fully gated shas, host parity); the MANAGING verdict and the stall alarm.
- 2026-11-07: the phase gate's Acceptance gets owning packets; the gate bot runs current code (gate-fresh.py, scheduled pulls); MANAGING GOOD needs proven to rise; every phase function's Acceptance named before its build; the backlog brake; code quality as a standing measure; then "mechanisms first" (verdict, tick, launch guard, packet and plan validators, skill lint), which led to v2.
