# overhaul-build changelog

Why each rule exists, newest first. Full v1 history: `git log -- skills/overhaul-build` (71 commits, 2026-11-05 to 2026-11-07).

## v2, 2026-11-07: the harness
Rewritten around a deterministic harness: drive.py builds, blind-reviews and lands one function at a time, land.py alone changes main, and the manager only starts it, fixes needs-plan functions through plan_validate, runs a blind proof-check and reports. Design and contracts: `skills/OVERHAUL-V2-DESIGN.md`. Evidence: VOIDCAD-fleet `notes/failure-analysis-2026-11-07.md` (F1-F10, R1-R12 and its Confidence section), `proof-check-2026-11-07.md`, `code-quality-2026-11-07.md`, `build-vs-buy-2026-11-07.md`.

Measured on the VOIDCAD v1 run, and what each changed:
- Proven sat at 4 for about 8 h while about 60 packets merged; 31 PASS PRs waited at 6 landings/h; one batch blamed 6 PASS PRs for 2 culprits. Now one function per builder, and a sequential land.py whose failure blames only its own branch.
- About 20 protected batches in one day, most for plan DATA, and 48 rulings. Now plan data changes only as a plan fix that passes plan_validate; there are no rulings or protected batches.
- Of 10 functions counted proven, 2 were REAL, 7 PARTIAL (real library code that nothing in the product called) and 1 a STAND-IN: "proven" meant a named test passed, not that the product does it. Now acceptance drives the public entry, red before and green after, and a blind proof-check closes the run.
- Code quality 6.5/10: lint saw only changed files (148 whole-tree findings on main), 18 of 41 sampled merges tested a stand-in, 5 owner pairs imported each other. Now standard tools check lint, duplication and cycles on the whole tree at every landing, and entry_lint bans test doubles in acceptance tests.
- The 12-hourly auditor never produced a result; builders hit the 130k context guard and left their brief's scope. Now there are no scheduled agents, and drive.py generates every brief under brief_max_tokens with its allowed paths.
- The master broke its own rules: inline prompts, hand-edited DISPATCH rows, a duplicate ruling and duplicate builds, 250 to 350 turns/h, 650k context, and times written ahead of the clock. Now the manager launches nothing but the proof-check, waits on one command, copies the VERDICT line and reads every time from the clock.

Corrections from the Confidence section stand: the VOIDCAD plan was sound and its ownership model is kept; the stall was a counting gap, not unfinished functions; F6's stale-worker cause is unproven.
Kept from the unlanded draft `wip/overhaul-build-2a`: every rule names its mechanism (`scripts/skill_lint.py`); the manager is the main chat, never a subagent; a launch prompt is only "Read and do <brief>."; status is four lines copied from measurements; the owner is asked nothing routine.
Removed: the six v1 references (`heartbeat.md`, `heartbeat-brief.md`, `loop.md`, `m0.md`, `red-fix-brief.md`, `rulings.md`), still at 86668be. Added: `references/proof-check.md`, `references/voidcad-legacy.md`.

## v1, 2026-11-05 to 2026-11-07: the master-run fleet
A master agent ran builder, review, ruling and heartbeat agents, after an M0 that built the fleet's own machinery (health.py, a gate bot, a check CLI, tick checks, guards, wait.sh, a dashboard generator).
- 2026-11-05: created with overhaul-check and overhaul-plan; ownership guards, the AI-slop lint, the look-alike lint and the `find` lookup; no deadlines for agents (call caps, exit checks, progress gates); the master must be the main chat; a work-style block in every brief; run-4 and run-5 fixes (no reshaping code to dodge a lint, hook first, supersede); a bug and feedback button in M0; the bottleneck watch.
- 2026-11-06: never stuck on the owner; a daily skills check; the fleet stays full (FLEET-TARGET, the H21 idle red); a heartbeat every 30, then 20 minutes, running every check; the token burn tripwire and launch brake (burn.py, burn-guard.py), then "waste, not spend"; the permissions paste; one dashboard template page, never stale, with history; the master loop enforced by script (master-status.py, master-stop-guard.py) and SKILL.md cut from 40.9 KB to 16.4 KB; the outcome brake; the proof path (a full main gate on the bot's clock, deploy only fully gated shas, host parity); the MANAGING verdict and the stall alarm.
- 2026-11-07: the phase gate's Acceptance gets owning packets; the gate bot runs current code (gate-fresh.py, scheduled pulls); MANAGING GOOD needs proven to rise; every phase function's Acceptance named before its build; the backlog brake; code quality as a standing measure; then "mechanisms first" (verdict, tick, launch guard, packet and plan validators, skill lint), which led to v2.
