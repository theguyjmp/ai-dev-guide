# overhaul-build changelog

## Builders prove before DONE (throughput pass B), 2026-11-09
Measured 2026-11-09 on the pilot project (log.jsonl): 18 of 49 lands failed, mostly regressions of proven tests
(s15_occurrences, s21_cpu_culling) and "test failed on merged tree"; 37 of 118 reviews failed, almost all C05.
- land.py: `footprint_rows(arch, rows, mods, fid)`, the regate's footprint rule, now shared with drive.py.
- build-brief.md: a "Proven tests your change can break" section with the exact batched commands of every proven
  test in the function's footprint (over its allowed paths), and a C05 section (`git grep -n`, dup, literals); rule 6
  makes own acceptance, the footprint tests and the dup check pass before DONE; a proven test it cannot keep green
  inside its paths is a BLOCKED-BY.
- drive.py pre-review gate: after the function's own acceptance, the footprint of its real diff runs batched on its
  slot (event `footprint`); a red test fails the attempt `regression before review: proven <row> <test>` with
  `.overhaul/runs/<id>-footprint.out`; a [tiers] test reruns alone up to 2 more times; a test red on clean main
  too is skipped (`footprint_main_red`); a timeout is left to land. `[harness] pre_review_footprint` (default true).

## Live re-plan, 2026-11-09
Measured 2026-11-09 15:07 CST on the pilot project under supervise.py: verdict "rate 1.7/h of expected 1.8/h (slots 3, build
10.3m, 10 builds/land)". 7 functions sat needs-plan and most of 79 planned rows depended on them, so only 3 slots could
run; supervise.py ran a plan-fix round only when drive.py exited 3, and drive kept running while lands trickled.
- supervise.py: drive.py runs as a child; every 60 s, needs-plan functions with no plan fix in flight get a live round
  (same agents and integrator, ids L1, L2...): `plan_validate --run` on its branch, then under `.overhaul/lock` a rebase
  onto main, PLAN OK and a fast-forward, then `.overhaul/requests/reset-<id>` (or `reload-<round>`). One round at a
  time; one live try per function per reset count; a drive exit 3 waits for the live round, and one that landed reruns
  drive without using an Exit-3 round. `.overhaul/supervise/round.json` names the round in flight.
- drive.py: applies reset requests at the next cycle boundary like `--reset` (event `reset_applied`; a third is refused,
  `reset_refused`), re-reading the plan on main first (new and changed rows, no restart; `reload_refused` and no retry
  on the same sha if plan_validate static fails).
- verdict.py: the line shows `needs-plan N (round in flight: ids)`.
## Failure router, 2026-11-09
Measured on the pilot project: 7 lands since 8 AM (0.85/h) against a 10/h floor; 32 failed attempts since 12:34 PM: 8 review
failed, 7 acceptance red after merging main (several really files-cell gaps or oversized 1M rows), 6 timing/flaky tests
on main, 3 builder made no change, 2 outside files cell, 2 builder timed out, 1 contract defect, 1 plan gap. The verdict
showed an expected rate and a bottleneck stage, but nothing classified WHY attempts failed; the systemic detector needs
one normalized reason 3 times and never fired; the manager classified by hand, hours late.
- router.py (new): every failed attempt (drive.fail and drive.park: needs_plan, land_fail, review_fail, builder and
  land timeouts, failed dependency fixes) gets one class from its reason and, for a thin reason ("no committed
  change", "acceptance red"), from the builder's last output: files_gap, missing_dep, contract_defect, main_flake,
  load_flake, oversized, review_quality, other. Event `routed` (`class=<c> -> <route>: <evidence>`); rolling 60-min
  counts in `.overhaul/router/router.json`. `py -3 router.py --repo R` prints the last hour's classes.
- Routed at once, not at Exit 3: a plan class parks the function needs-plan with the class and evidence in its
  reason and writes `.overhaul/router/routes/<id>.json`; supervise.py, while drive runs, gives each routed function
  a plan-fix agent (class and evidence in the brief's mode line; oversized means split), one route per function, at
  most `[harness] plan_fix_parallel` at once, the integrator when more than one fixed, lands the plan under
  `.overhaul/lock`, and writes `.overhaul/requests/reset-<id>` (live-replan's request file). Until drive consumes
  requests itself (live-replan), supervise applies them with `drive --reset` when drive exits 3, then drives again
  without an Exit-3 round. With live-replan's live rounds present the router's round steps aside and the live round
  reads the class from the reason. main_flake: drive's existing fix packet to the owner module, plus lands of queued
  rows whose footprint reruns the test (same module, or a changed file in its module or the test file) are held
  until that fix settles. load_flake: no attempt charged, the land requeued (at most 2 per function).
- verdict.py: `failures` in verdict.json (counts per class, top class, a human line), ` failures N top <class>` on
  the VERDICT line, and a red when one class is over 30% of at least 3 failures in the last hour, in words: "most
  failures: builders cannot edit a needed file (5 of 9)". An older log without `routed` events is classified from its
  raw failure events, one per function per 2 min. dash.py shows the line.
- On the pilot project's log for 12:34-16:20 (raw): 30 failures: other 7 (regressions of proven rows the branch caused, empty
  builder output), review_quality 6, oversized 5, files_gap 4, missing_dep 3, main_flake 3, contract_defect 2.
## Single drive, 2026-11-09
Measured on the pilot project: two supervise.py + drive.py pairs ran on the same repo at once (a relaunch while the old one was
alive). They overwrote each other's .overhaul/state.json (manager resets "did not stick") and handed the same
.overhaul/target/slot-<k> to two worktrees, so a test ran a app.exe built from another worktree and reported a false
"acceptance red" (R0422). Builders started by an earlier drive kept running after that drive was killed.
- drivelock.py (new): `.overhaul/drive.lock` with pid + process start time, created exclusively and checked against the
  live process table (a dead or reused pid is stale and taken over). supervise.py and drive.py take it first; a second
  start exits 1 at once naming the running pid. drive.py started by supervise.py runs under its lock
  (OVERHAUL_DRIVE_LOCK; builders and plan-fix agents never get it). --reset/--mark-proven/--repolish refuse while
  another drive holds it, pointing at `.overhaul/requests/reset-<id>` when live re-plan is in drive.py, else at the pid
  to stop.
- No orphans: memcap.cap_self(gb, kill_on_close=True) makes one Job Object with both limits (memory cap and
  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE), so killing supervise or drive kills its builders, reviewers, lands and their
  cargo/test processes. At start drive kills leftover processes whose command line names this repo's .overhaul/wt or
  .overhaul/target (by pid, taskkill /T; event orphan_killed).
- Slots: drive persists the fid -> slot map in state.json (`slots`), checks it at start (a slot given twice or held by
  another live drive is dropped, event slot_dropped), and marks each slot dir (.overhaul-slot: drive pid/start and job).
  A slot another live drive holds is never handed out. Before a job's acceptance runs, a slot last built by another job
  is rebuilt (`[commands] build` when set); a red whose slot changed hands during the run, or whose cargo `Running`
  binary is outside the job's slot dir, is rebuilt and run once more, never reported red (event slot_rebuild).
- tests/test_single_drive.py: fake process table, stale and reused-pid locks, double start of drive/--reset/supervise
  as real processes, orphan matching, a real Job Object kill, slot persistence, reuse and the rebuild-not-red cases.

## Supervisor, 2026-11-09
Measured on the pilot project vc5: the build sat at 44 proven for 4+ hours. drive.py was fine; everything around it was run by
hand and failed: a bash launcher ran ONE serial headless manager per exit 3 (25+ min, one function at a time);
planners and managers ran `plan_validate --run` and 1M-fixture tests outside drive's memory cap (30 GB processes,
three Claude Code low-memory reaps); `memcap.py 12 -- bash` picked WSL bash and `-- claude` was not found, so
relaunches silently ran nothing; `plan_validate --run` failed on timing tests that pass alone. The parallel
plan-fix + integrator flow done by hand that day fixed 9 functions in ~6 min.
- supervise.py (new): the one launcher. It calls memcap.cap_self first, so every child is capped at
  `[limits] proc_mem_gb`; runs drive.py in the foreground; exit 0 runs the blind proof-check (only when every
  non-deferred row is proven) and stops; exit 3 runs one Exit-3 round and drives again (`--max-rounds`, default 12);
  any other exit runs drive once more, then stops with the error line. One line per step in
  `.overhaul/supervise.log`; it writes nothing outside `.overhaul/`. Exit 0 done, 4 proof not all REAL, 3 rounds
  used up, 1 error.
- Exit-3 round, parallel: one `claude -p --model opus` plan-fix agent per needs-plan function at once
  (`[harness] plan_fix_parallel`, default 12; `templates/plan-fix-brief.md`), each in its own worktree on
  `plan/fix-<id>` with 5 new ids reserved after the highest id, so splits never collide. One integrator
  (`templates/plan-fix-merge.md`, `[harness] plan_merge_model`, default sonnet; skipped for a single fix)
  cherry-picks the branches into `plan/fixes-r<round>` with the phase cap kept. supervise.py checks PLAN OK on it,
  fast-forwards main, runs `plan_validate --run` once and `drive.py --reset` for each fixed id still a row. A function
  with two plan fixes is never reset again: its agent must replace it with new ids (supervise stops if it did not).
- plan_validate.py --run: a proven acceptance that fails reruns ONCE alone, serially, before it counts; a pass then
  is a `flaky under load` note on stderr, only a second failure is a PLAN FAIL (`settle_flaky`).
- memcap.py: `which`/`resolve` find child programs like a shell: on Windows `bash` is Git Bash (from
  `git --exec-path`, else Program Files), never System32 or WindowsApps bash.exe (WSL); a bare name tries .exe, .cmd,
  .bat, so `claude` is the npm shim or claude.exe. `memcap.py GB -- cmd` exits 127 with `memcap: error: cannot find`
  instead of running nothing.
- Every headless claude supervise.py starts (and drive.py's, through the environment) gets
  `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0`.
- tests: test_supervise.py (fake drive exit codes, fake claude, fake plan_validate; two needs-plan rows that would
  split into the same id).

## Main red, systemic failures, throughput tripwire, 2026-11-09
- Measured on the pilot project 2026-11-09: from 11:30 the phase 4-5 plan commit left main failing dup_blocks (three kernel stubs repeating one Params struct). Every build then failed "dup failed in the worktree after merging main, before review", each counted as that function's attempt, functions went needs-plan and nothing landed for hours. Before that, depfix passes kept ending "builder left no committed change" (53 dep_fix events). drive.py and verdict.py never said "main is red" or "the same failure is hitting every function"; the manager found it by hand hours later.
- Main red (scripts/mainred.py, hooked into drive.py fail/launch/pump_land/depfix_settled/blocked_by/finish): a whole-tree check (test, lint, dup, literals, shape, cycles, entry_lint, extras) failing in a worktree after merging main, or at land, is rerun on clean main (cached per main sha). Red there too: event `main_red`, the attempt is not counted, the function waits (before review, then goes to review; or at landing, then lands), new builds and plain lands hold, and main gets one fix: exit 3 `needs-plan: main red in plan stubs: <check> <files>` when every failing file was only touched by plan/m0 commits, else one depfix-main-<check> to the module owning the first failing file. Green again: event `main_green`, everything resumes.
- Systemic: 3 different functions failing one normalized reason (ids, paths after the first, numbers and backticked commands stripped) within [harness] systemic_window_min (default 30, 0 = off) log `systemic`; drive checks main for that check and, if main is green, exits 3 `needs-plan: systemic <reason>`.
- Dead depfix: a second "builder left no committed change" depfix for one owner module in the window parks the waiting function at once as `plan gap: <blocked-by text>` without spending attempts, and later BLOCKED-BY lines on that module park the same way without a depfix.
- verdict.py: `main red: <check>` is the top red with its action; `systemic: <n> functions ...` while in the window; the line shows `rate N/h of expected E/h (slots S, build Bm, B builds/land)`, where expected = min(slots x 60 / median build minutes / rolling builds per land, 60 / median land minutes) and slots is max_parallel capped by the ready functions that can build at once (module rule, files cells). `rate red` when the last 60 min landed under half the expected rate or under [harness] min_rate_per_hour (default 0 = off) while builders ran, naming the stage that costs most (idle slots, needs-plan, blocked on deps, rework builds per land, landing queue). Tests: tests/test_main_red.py.

## Land priority and tier reruns, 2026-11-09
- Measured 2026-11-09 13:00 CST on the pilot project with 16 builders running: S123 passed review, then failed at land with
  `regression: proven S02 tests/acceptance/s02_bench.rs::test_s02_stats_and_bench_record fails`, a timing benchmark
  that passes alone in 2.7 s; plan_validate --run failed the same way on S02/S06/S12/S15 under load. Timing tests in
  a [tiers] glob rerun at land when the footprint touches their module, so under load they rejected most lands.
- priority.py (new; Windows creationflags and ctypes SetPriorityClass, a no-op elsewhere): drive.py starts builders,
  reviewers, fix agents and probes (slot_env work) at BELOW_NORMAL, and their checks inherit it; land.py, the
  pre-review gate (whole-tree commands, cycles, the function's acceptance) and periodic gates run at NORMAL; land.py
  sets itself NORMAL and runs its acceptance batch, reruns and main-flake reruns at ABOVE_NORMAL.
- land.py: a proven test inside the footprint that matches a [tiers] glob and fails reruns alone, serially after the
  batch, up to 2 more times (TIER_RERUNS) before it counts as a regression; a recovery appends `tier rerun passed:
  ...` to land.out and prints it (rerun output in land-tier.out). Non-tier proven tests keep the old rule.
  Tests: scripts/tests/test_priority.py (fake Popen creationflags, a fake flaky tier test, an end-to-end land).

## Phase audit, 2026-11-09
- Owner requirement 2026-11-09: after each phase, a reality check that the whole platform still follows best coding
  practice and the architecture stays clean, organized, with no slop and no duplicate functionality in different
  areas. the pilot project ran with `phase_barrier = false` for throughput, which also turned off phase_polish, so no per-phase
  audit ran.
- `[harness] phase_audit` (default true, independent of phase_barrier): when every row of phase N is proven, drive.py
  runs phase N's polish (plan gaps, probes of the modules it finished, checklist rounds scoped to its modules, fixes
  landed by land.py --polish) beside the later builds. The modules it covers (state `phase_polish[N].locked`) and every
  module a queued or running polish or arch fix edits start no new build until that work lands; a fix waits for builds
  in flight in its modules; a re-check round audits only the changed files of its modules; a raised count is not
  reverted beside builds (a revert would drop their lands). With the barrier and phase_polish on, nothing changes.
- templates/arch-audit.md: one blind Opus reviewer (audit_model) beside polish round 1 (phase and final) reads
  ARCHITECTURE.md, every module's public API and the code in scope and answers A01 same capability in two modules (by
  behaviour), A02 reaching around the owner's API, A03 [P0] layer violation, A04 shape limits and god files, A05 slop,
  each NO with both locations. Its blocking NOs join round 1's checklist NOs (state `arch`), are verified the same way
  and go to one fix per module; A01/A02 route like C03/C05: the caller gets the dedup, the owner's own fix first.
- Every `[harness] arch_audit_every_lands` (default 25, 0 = off) proven functions, the arch audit runs over the whole
  tree mid-phase (state `arch_sweep`, files `.overhaul/polish/arch-sweep-<n>*.json`, fixes `arch-r<n>-<module>`), never
  beside a phase or final polish.
- verdict.py: `phase N audit: V open` on the line (dashboard); an ended phase audit with verified P0/P1 left, or a failed
  arch audit, is a `phase-audit` red. Tests: tests/test_phase_audit.py.

## Main guard, 2026-11-09
- Per commit, not per range: a commit whose subject starts with `plan` or `m0` may also add code (new files,
  added lines: the stubs and module skeletons overhaul-plan writes), never change or delete an existing line.
- guard/reference-transaction, installed by drive.py into the repo's git hooks at every start (shared by all
  worktrees): a ref update to main outside the harness is refused unless main moves forward and changes only spec/,
  tests/acceptance/, tests/property/, overhaul.toml, .github/workflows/ or dash/. Commits, ff-merges, `push .`,
  update-ref, reset, `branch -f` and deleting main are all covered. drive.py and land.py set OVERHAUL_HARNESS;
  slot_env drops it, so builders, polish agents and their checks cannot move main either. Reason: on 2026-11-09 a
  coordinator side agent fast-forwarded code onto main outside land.py. Tests: tests/test_main_guard.py.

## Gates and memory cap, 2026-11-09
- periodic.py: each gate keeps its own green (state `green: {gate: sha}`; an older state seeds it from last_green
  and from each gate's latest green run in log.jsonl). A red gate bisects over the lands since its own green, gates
  one after another, so one gate's red (or a never-green gate) no longer blocks another's bisect. A gate still red
  keeps the culprit found before instead of bisecting again every run.
- periodic.py ci: when the run failed and no job ran a step, the job annotations (`gh run view`) are read; jobs
  GitHub never started (billing, spending limit, unsupported runner) print `ci: unavailable: <reason>` and exit 75.
  periodic records such a gate under `unavailable`: never bisected, no depfix, not green. verdict.py shows it as an
  owner line (`parity gate unavailable: ...`). No threshold changed and no runnable gate is skipped.
- periodic.py: per-gate cadence. `every_lands` and `every_min` may be tables (`default` plus gate names), e.g.
  `[gates.periodic.every_min] parity = 10080` for a weekly paid CI gate. A gate with its own cadence runs only on the
  keys it names, and at the end once, only when every function is proven (once per plan size, state `finish`).
  Lands and run times are counted per gate (state `since`, `ran`); last_green moves only on a run of every gate.
- memcap.py (new): `[limits] proc_mem_gb` (default 12, 0 = off). drive.py (and land.py run by hand) put themselves
  in a Windows Job Object with JOB_OBJECT_LIMIT_PROCESS_MEMORY, so every process they start is capped; a process over
  the cap fails its allocation instead of starving the machine. Failure reasons with an allocation failure in the
  output (builder exit, pre-review gate and acceptance, land commands, periodic gates) start
  `process memory cap N GB hit`. ctypes only; nothing on other systems.
- tests: test_gates_memcap.py.

## Pass E: polish converges, 2026-11-08
- drive.py: visibility-only edits (pub / pub(crate) / export / a re-export of an existing item) are also allowed in
  the modules the fixing module depends on (ARCHITECTURE.md `Depends on:`), for builders and polish fixes alike;
  drive.py refuses any other change there before review.
- drive.py: a cross-module duplication issue (C03/C05) goes, as one fix, to the module that depends on every other
  one it names (the caller), with a note to export the owner's copy and import it; when the owner has its own fix
  that round, the owner's fix starts and lands first (`polish_order` log line).
- drive.py: a round fix that fails its review carries its verified NOs into the next round's fix brief for that
  module (state `failed_nos`).
- drive.py: polish fix rounds default and cap 3 (was 2); `[limits] fix_retries` (default 2, was a fixed 1).

## Pass C4: per-phase polish and visibility edits, 2026-11-08
- drive.py: deferred rows are out of the current plan; polish, probes and the verdict no longer wait for them (C3
  made polish wait, so a rolling plan would never polish).
- drive.py: per-phase polish (`[harness] phase_polish`, default true, with the barrier): once every function of
  phase N (not the last) is proven, phase N's plan gaps, the probes of the modules it finished and up to
  polish_rounds audit/fix rounds scoped to its modules run before phase N+1 starts; state `phase_polish.N`, files
  `.overhaul/polish/phase-N/`, briefs `polish-audit-pN-<round>.md`. The final whole-tree polish still runs and takes
  only the gaps no phase pass took.
- drive.py: a builder may make visibility-only edits (pub / pub(crate) / export / a re-export of an existing item)
  in other files of its own module; the briefs say so, drive.py refuses any other change there before review
  (visibility_only on the `git diff -U0`) and the review checks it.
- tests: test_harness_c4.py; the TestBudgets timeout test has an 18 s timeout and 30 s kill waits under load.


## Rust plan and land path, 2026-11-08
- rust_src.py (new): a stdlib Rust lexer (comments dropped, literals as one token) with fn spans, #[test] flags,
  calls, `#[cfg(test)]` and `proptest!` spans.
- entry_match.py: Rust acceptance tests are matched against cli/http entries; new `lib: <crate>::<path>::<fn>` entry;
  a CLI row whose .rs tests never run the built binary (`CARGO_BIN_EXE_` / `cargo_bin(`) is a problem.
- plan_validate.py: acceptance may be `tests/acceptance/<f>.rs::<test>` (a `#[test] fn`), each .rs file a root
  Cargo.toml `[[test]]` named by its stem; .rs stubs call `todo!()`/`unimplemented!()`; a clippy lint needs
  `-D warnings` and `--all-targets`; cargo lint-green gets 1200 s; no conftest.py for an all-Rust plan; property tests
  are 1 to 3 `#[test]` fns in `proptest!` in tests/property/test_<id>.rs; --run fills `{stem}`.
- land.py/drive.py: the acceptance template takes `{stem}`; the build brief shows the gate's exact commands; the Rust
  probe hands mutate.py the module's `#[cfg(test)]` files, `<path>/tests` dirs and .rs property files.
- anchors.py: every `tests/**.rs` file is an anchor; `property_file(fid, ".rs")`. Python behaviour unchanged.

## Rust probe, 2026-11-08
- mutate.py: when every --paths entry is Rust, the probe runs cargo-mutants on its own temp copy (never --in-place),
  samples --max-mutants of the listed mutants with --seed and runs only those; --tests map to cargo test targets
  (test files and dirs, src/ adds --lib, other words are name filters). Same JSON as Python plus "unviable" (mutants
  that do not compile, left out of the score). A red baseline reads proptest "minimal failing input" and panic lines.
  CARGO_TARGET_DIR is kept per repo under the system temp so reruns reuse compiled dependencies; the workspace's own
  packages are cleaned first, since the copy keeps source mtimes and cargo would reuse the last mutated build.
  Exit 3 when cargo-mutants is missing. Python paths are unchanged.

## v3 trials 7-9, 2026-11-08
Trials 7 and 8 stalled at 3/21: F02, which 16 functions waited on, parked over faults no F02 builder could fix.
- plan_validate: an acceptance test that drives another row's entry must wait on that row's behaviour (F02's test ran
  F05's `stock price`); lint must be green on the plan tree, so a manager plan fix cannot turn main red.
- drive: the pre-review gate runs the function's own acceptance (saved to runs/<id>-acceptance.out), so a red test
  no longer costs a review and a land; retries grow by one per 5 waiting functions (max +2); dedup-only verified NOs
  on the last attempt land and go to plan-gaps.
- drive: BLOCKED-BY (build brief rule 9). Trial 9's F02/F08/F09 failed on a core store-lock and money bug outside
  their paths; the owning module now gets one dep fix and the function rebuilds free.
- drive: a module below its mutation floor is mutated again after each landed probe fix (2 passes max); trial 7's
  verdict read the 72% from before the fix.

## v3 gap, 2026-11-07
Pass 2: a verified blocking NO of a dedup item (DEDUP_IDS: C03, C05, C06, C07, C08) tied to a PLAN-GAP passes the function and goes to plan-gaps.jsonl; only a behaviour NO parks it. The probe and polish fix retry count is kept in state.json.
Trial 6 lost 5 functions to needs-plan over PLAN-GAP lines about shared constants (F06's had 0 blocking NOs and
said "Optional"), and threw away probe-core's fix of 31 mutation survivors over one verified P1. Now a PLAN-GAP goes
to needs-plan only when it is tied to a verified blocking NO (its id or file named in the gap, or the NO itself says
PLAN-GAP or outside the allowed paths) on a diff that stayed inside them; otherwise the function passes or retries as
usual and the gap goes to .overhaul/plan-gaps.jsonl (event plan_gap). After the build and the probes, the gaps run as
ONE cross-module fix pass (polish-gaps, event plan_gaps) that may touch every module they name, alone, before polish
round 1. A probe or polish fix whose review has verified blocking NOs gets one retry pass on its branch carrying the
NO list, reviewed under the round rule, before it fails.

## v3 probe, 2026-11-07
mutate.py: a stdlib AST mutation tester (comparisons, boundaries, arithmetic, booleans, returns, raises), one
subprocess per mutant on a temp copy, at most [probe] max_mutants (40). drive.py runs it once a module's functions are
all proven (event probe_mutation); below [probe] mutation_floor (0.9), or with a property counterexample, the
survivors go to ONE fix pass for that module, reviewed and landed like a polish fix (event probe_fix); polish waits
for every probe. land.py rejects a branch that modifies, renames or deletes a test file from the plan commit
(anchors.py). Builder worktrees get a .claude/settings.json PostToolUse hook (lint_hook.py) that lints each edited
file and returns failures to the builder.

Why each rule exists, newest first. Full v1 history: `git log -- skills/overhaul-build` (71 commits, 2026-11-05 to 2026-11-07).

## v3 speed and conflicts, 2026-11-07
Trial 4's floor was the deps chain (29.8 of 41.7 min) and serial lands (48 s each). drive.py now starts a function
once its `behaviour` deps land; `stub` deps never block (the stub is on main). The file-footprint lock stays. After a
build (and after a conflict pass) drive.py merges main into fn/<id> and reruns the gate ([harness] pre_review, default
land.py's whole-tree commands) before review; a merge conflict there goes to the conflict-fix pass. The build brief
tells the builder to do the same before handing back. Lands whose footprints (claimed plus changed files) do not
overlap run at once ([harness] max_lands, default 3): land.py --stack gates outside the lock on main + the in-flight
branches + its own, waits for them, then commits under the lock with no second gate when exactly the stacked
functions landed; otherwise it regates on the new main, and a failed stacked gate retries as a plain land. main only
fast-forwards, and only to a tree the gate passed.
## v3 a fixed checklist with verified NOs is the bar, 2026-11-07
"A blind open audit scores 10/10" has no fixed point: each pass finds new things, and no source uses it as an exit
condition. The bar is now measurable (V3-DESIGN.md, "The bar" 4 and 5).
- `references/checklist.md` v1: K1-K4 contract items plus C01-C29 from the quality standard, each yes/no with a
  severity (P0, P1, nit) and how to decide it.
- Review answers the checklist as JSON and lists every NO at once; a verifier (`verify-brief.md`) must reproduce each
  blocking NO with a failing test or an exact line and input, or it does not block (review_unverified). The retry
  fixes the whole verified list. Round rule: a re-review blocks only on a still-NO item or a regression in changed
  code (review_overreach otherwise). Nits are capped at 5, logged and never dispatched.
- Polish is the checklist over the whole tree plus a dedup role, verified the same way, at most 2 fix rounds.
- verdict.py: GOOD = all proven, gates green, 0 verified P0/P1 (every blocking item YES), probe floors when
  probe_mutation events exist. Nits and the open-audit trend are reported (`nits J`, `trend`) and never gate.
- The final open audit (AUDIT-BRIEF) is a trend report: 7 scores plus NEW-RULE candidates for the next checklist.

## v2 one quality standard for every step, 2026-11-07
Trial 4's polish audit (sonnet, a condensed brief) ended at 4 issues while the final judge (opus, AUDIT-BRIEF)
found 18: polish optimised against a weaker, open-ended bar. The new issues were design-level (a two-store pay
that is not atomic, no stale-lock recovery, no socket timeout, unmapped OSError and RecursionError, unchecked
stored invariants, HTTP status chosen by message text). Proof marked F15 PARTIAL: its test drove `report sales`
while its row named `report [--kind <kind>]`.
- `references/quality-standard.md`: 36 numbered, checkable rules in the audit's 7 areas, covering every issue in
  trials 1, 3 and 4. Each says how to check it. New defects become NEW-RULE candidates and grow the file.
- Build and review briefs list the rules relevant to the function (a base set plus keyword triggers on its row,
  module section and acceptance test); the reviewer fails a change that breaks one, citing the number.
- Polish audits against the standard with the judge's model and method: `audit_model` defaults to opus, the
  template points at the standard and `templates/audit-brief.md` (the judge's brief), findings cite rule numbers.
  `polish_rounds` defaults to 3. A fix round that raises the issue count is reverted (polish_revert): main goes
  back to the best audited tree and the next round works from that audit's list.
- plan_validate.py (entry_match.py) fails a row whose acceptance tests never invoke its exact entry: a CLI call
  must match the entry's words, placeholders and optional groups; an HTTP call must hit the route and method.
## v2 depth: a critical-path check in plan_validate, 2026-11-07
plan_validate.py `check_depth` prints `critical path N functions, widest level W` and fails a deps chain longer than
max(4, ceil(N/4)) unless `[plan] max_depth` is set: each link lands one after another (about 2 min each), so the chain
is the build's floor. Trial 4: 7 deep, floor 29.8 of 41.7 min. Tests: DepthCheck.

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
