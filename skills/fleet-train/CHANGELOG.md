# fleet-train changelog

- 2026-11-14: optional `regen` commands in harness.toml `[profile]`: run in the train tree after the formatter and committed as "train: regen", so generated files cannot drift on main; a failure fails the batch quality.
- 2026-11-11: train conflict resolver (`resolve_conflict`, HARNESS_RESOLVE_MAX): 16 of 18 rows in one train of the pilot project were rejected on one hot file.
- 2026-11-12: train start rule (HARNESS_BATCH_WAIT_BUSY): wait for a full batch while claims are in flight, start at once when none; a 1-row train had cost a full gate.
- 2026-11-12: `unit_flaky`: new unit failures are rerun alone, single-threaded, before the batch is blamed (a timing test failed two gates under load).
- 2026-11-12: `fmt_tree`: batched rustfmt replaces `cargo fmt --all`, which overflowed the Windows command line and failed silently.
- 2026-11-12: `read_events`: the event log reader skips blank or half-written lines (one blank line took status and the dashboard down).
- 2026-11-12: `self-review --slot N` and builders.js step 7: builders run the train's duplicate reviewer before submit; builders.js `slot_list` arg.
- 2026-11-12: the plain clippy stage is skipped when the quality stage runs (its strict clippy is a superset; saved about 10 min a train).
- 2026-11-12: size gate `oversized_files` (HARNESS_FILE_LINES / HARNESS_TEST_FILE_LINES): files added or grown past the cap are rejected, existing ones grandfathered until split.
- 2026-11-12: cloud fleet: `cloud.py` (assign / collect / status through the `harness-cloud` branch, atomic `cloud/claim-<row>` refs, `cloud-base` mirror of the train's main) and the `cloud/` kit templates; local builders off the gate machine.
- 2026-11-12: dash.py takes the planned-row total from `harness.py status`.
- 2026-11-13: one harness for every stack: every stack-specific command and pattern sits in one profile (`STACKS`, `HARNESS_STACK=rust|node`, `harness.toml` overrides); rust behaviour unchanged (checked call for call against the previous file); node runs `node --test` per test file with TAP failure parsing.
- 2026-11-13: fix: gate()'s quick-path return had been dedented out of `if quick:`, so every full gate crashed on `fails`.
- 2026-11-13: `disjoint()` picks a batch by trial merges (git merge-tree), file-disjoint as fallback: 39 of 106 queued rows instead of 23.
- 2026-11-13: fleet_beat: FIRE cap 8 while 20+ rows are queued, 30 once it drains; SUPPLY alarm when builders starve behind the queue.
- 2026-11-14: `probe_culprit`: an acceptance-only red in a multi-row batch is bisected with only the failing tests first (events probe_found / probe_none); found the bad row in 17 min where the full bisect took 4 h.
- 2026-11-14: HARNESS_BATCH_CAP (default 20) replaces the fixed 80-row cap: batches of 30 rows or fewer landed 20-38 rows/h, every 42-80-row batch landed 0.
- 2026-11-14: `train` refuses to start without the stack's `require_env` (rust on Windows: ProgramFiles(x86), dropped by Git Bash); one such train rejected 16 innocent rows.
- 2026-11-14: fleet_beat: train found by state/train.pid, not a machine-wide pattern; TRAIN-DOWN restarts it detached through WMI (a train started from an agent shell died with it twice); TRAIN-PID, TRAIN-KILLABLE (a pattern-killing restart script killed another fleet's train 3 times in a day), TRAIN-ENV (ignores a gate older than the running train) and BEAT-TIMEOUT; a timeout on every subprocess call.
- 2026-11-14: dash.py: 3 h or more with nothing landed and rows queued shows RED "Nothing landed in N h"; 7 h of 0 landed had shown only amber.

## Moved from SKILL.md in the brevity cut (verbatim; the rules stay in SKILL.md without their stories)

Replaces drive.py/supervise.py for the build phase. Measured on the rebuild on 2026-11-11: the old harness proved
79 rows in 64 h (about 1.2/h). This one landed 141 rows in its first 3 h after the first land.

### Why the old harness stalled (do not rebuild these)
- Any build trouble parked a row in "needs-plan" for a separate plan-fix pipeline. 56 of the 61 buildable rows ended up
  there, so 2 rows could start and 16+ slots sat idle. Here the builder OWNS its row: it implements missing deps,
  splits work, fixes stubs. There is no needs-plan exit.
- Every function landed alone through a full gate (about 7.6 builds per land). Here one gate run covers a whole batch.
- A git reference-transaction hook ("overhaul guard") blocks every main update not made by drive.py. Rename it
  (`.git/hooks/reference-transaction` -> `*.disabled`) before the first train.
- Leftover worktrees and per-worktree target dirs filled the disk (433 worktrees, about 135 GB; old cargo caches 350 GB).

### Train rules (each one came from a real stall)
- Batch up to 80 queued branches. A conflicted branch first gets `resolve_conflict` (a short headless Sonnet pass,
  at most 4 .rs files, accepted only with no markers left and `cargo check` clean; HARNESS_RESOLVE_MAX per train,
  default 12); the rest are rejected back to the builders with the reason.
- Start rule: wait for HARNESS_MIN_BATCH rows (default 8), up to HARNESS_BATCH_WAIT_BUSY (25 min) while builders
  still hold claims; with no claims in flight, start at once (nothing more is coming).
- Gate once: rustfmt (auto-fixed in batches of files: `cargo fmt --all` overflows the Windows command line on big
  workspaces and fails silently), clippy -D warnings (skipped when the quality stage runs: its strict clippy is a
  superset), unit tests, then acceptance tests of every proven row plus
  the batch's rows, built once in release (incremental) and run as parallel binaries.
- A failed test is re-run alone, serially. Perf-budget failures ("BENCH ... over budget/limit/floor") are written to
  state/perf-debt.jsonl, not blamed (they are noise on a loaded machine); check them later on a quiet machine.
- Stale fixture locks are cleared and the test re-run once.
- Blame before bisect: files named in the failure output (clippy paths, failing unit crates, failing rows' declared
  files) eject only the branches that touched them.
- Probe before bisect (`probe_culprit`): an acceptance-only red in a multi-row batch is bisected with ONLY the failing
  tests (a few minutes a step) to find the one row that reproduces it alone; events probe_found / probe_none, and
  probe_none (an interaction) falls back to the full bisect. Measured: 17 min where the full bisect took 4 h.
- Batches are capped at HARNESS_BATCH_CAP rows (default 20). Batches of 30 rows or fewer landed 20-38 rows/h; every
  42-80-row batch landed 0 (about one bad row or clash per 10 rows, and each bisect level is a full gate).
- `train` refuses to start when the stack's `require_env` is missing (rust on Windows: ProgramFiles(x86), which Git
  Bash drops; the MSVC toolchain is found through it). A train started without it failed every native test and
  rejected 16 innocent rows. Projects add their own vars in harness.toml `[profile] require_env`.
- Unit failures that main already has (state/unit-baseline.json, per main sha) are not blamed. Up to 5 new unit
  failures are rerun alone, single-threaded (`unit_flaky`); if all pass, it is load flakiness, not the batch.
  Retries re-run unit.
- Quality is NOT optional and has no switch (owner 2026-11-11, after a run without it put ~54 duplicate functions on
  main in 4 hours). Enforced in code, three layers:
  1. Every claim carries `must_also_before_commit`: search spec/index + ARCHITECTURE owners before writing any
     function, and run dup_blocks, dup_literals, shape_check, check_cycles and strict clippy.
  2. The train's gate always runs those checks plus the owner guard (`owner_copies`: no fn outside crates/core named
     like a core-owned math/param/refusal helper; the allowlist stays EMPTY - replace or rename, never grandfather),
     on full gates and on retries, and the size gate (`oversized_files`: a .rs file the batch adds or grows past
     HARNESS_FILE_LINES, default 500, tests 600; files already over are grandfathered until split). After the tests pass, `batch_review` runs a headless `claude -p` reviewer over the
     functions the batch adds and rejects branches that duplicate an existing owner (different names included).
  3. `scripts/pre-push.hook` -> `<repo>/.git/hooks/pre-push`: refuses any push to main whose exact commit fails the
     quality check, including manual pushes.
  The train also regenerates spec/INDEX.md on every land, so the index builders search is never stale.
- Before the first run on a codebase that grew without these checks, run workflows/cleanup.js, then a semantic
  duplicate review, then targeted refactors until the reviewer reports 0 duplicates; only then start builders.
- Claim limits: at most 3 kernel rows / 6 rows per module in flight, no two rows with overlapping declared files;
  rows that unblock 8+ others skip the module cap; state/priority.json rows go first and skip caps.
- Stacked starts: a row whose deps are queued (not yet landed) starts on main + those branches.
- The loop never dies on an exception and re-execs itself when harness.py changes and still parses.
- events.jsonl is read tolerantly (`read_events` skips blank or half-written lines): one bad line once took status
  and the dashboard down.
- Freeze a working harness: one tested change at a time, keep a dated backup, revert if the land rate drops.
- Batch pick by trial merges (git merge-tree): 39 of 106 queued rows vs 23 by file-disjoint; still no resolves.
- Land first: builders starve when the queue backs up (their deps are queued, not landed); the heartbeat scales
  builders up only once the queue drains (fleet_beat FIRE cap 8 -> 30 under 20 queued; SUPPLY alarms when fewer than
  8 rows are open to builders while 40+ wait in the queue).

### Manager loop
- Laptop scripts cannot start cloud sessions; only the manager chat can (a routine run). So the manager runs an hourly
  heartbeat (an in-chat cron): `py -3 scripts/fleet_beat.py` compares rows waiting for a session with sessions running
  and prints `FIRE n`; fire that many runs (capped per beat). Without it, rows wait for hours on a handful of sessions.
- The heartbeat is a health check, not a liveness check. A train that was alive but landed 0 rows for 6 h passed a
  liveness-only beat every hour. fleet_beat prints ALARMS (TRAIN-DOWN, TRAIN-PID, TRAIN-KILLABLE, TRAIN-ENV, STALL,
  LONG-BATCH, REPEAT-RED, MAIN-RED, REJECTS, REWORK, STALE-CLAIMS, OLD-QUEUE, TRAIN-ERROR, DISK, BEAT-TIMEOUT), each
  naming its fix, and the manager fixes every one in the same beat,
  never only reports it. A new failure mode found by hand gets a new check in fleet_beat.py.
- Train process: `train --loop` writes state/train.pid. TRAIN-DOWN (that pid is gone) restarts the train detached
  through WMI (Win32_Process Create, HARNESS_* vars on its command line). Never Start-Process or Popen it from an agent
  shell: the child stays in the shell's job and a train died twice that way, silently. Restart and kill scripts act
  only on their own state/train.pid tree (`taskkill /T /PID`), never on a machine-wide 'harness.py train' pattern: one
  fleet's pattern-killing restart script killed another fleet's train 3 times in a day. TRAIN-KILLABLE fires when this
  train's command line matches that pattern; the beat's restart uses the quoted full path, which does not.
  TRAIN-ENV: the last gate failed on the toolchain env (stack `env_fail_re`); a gate older than the running train or its
  current batch (train.pid / train.lock) is ignored, as that restart already handled it. state/train.off = stopped on
  purpose, no restart.
- Dashboard: no landing for 3 h or more with rows queued is RED ("Nothing landed in N h"), not amber; 7 h of 0 landed
  showed only "below the bar" and nobody acted.
- Frontier: only rows being built (claims) count against the per-module cap and file-overlap filter, never
  queued rows. Counting the land queue starved every builder (0 of 93 ready rows assignable) whenever landing
  fell behind. Check `harness.py frontier` against the count of rows whose deps are all proven or queued.
- The train's blame: a failing acceptance test that no batch row owns (a quality or regression test) blames the batch
  branch that touches a repo file the test's source names; with disjoint batches that is one branch, ejected at once.
  Before this the failure went to bisect at one full gate (~18 min) per level.
- Each routine fire is one session. Scale with several routines on staggered minutes, not by firing runs by hand
  (each manual run's response costs the manager context).
- NEVER attach a GitHub push webhook to a builder routine: it has no ref filter, builders push, and every push starts
  another builder (a runaway loop). If one is attached by mistake, disable that routine at once.
- Rebaser routine (`cloud/REBASER.md`): cloud agents rebase queued rows that stopped merging onto main. The train also
  picks a batch of rows that trial-merge cleanly together (`disjoint()`), so row-vs-row conflicts never cost gate time.
