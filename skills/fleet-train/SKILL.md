---
name: fleet-train
description: Run a fleet of short-lived Claude Code builder agents on a planned rebuild (spec/FUNCTIONS.md rows) with a batch land train - claim, build, submit, then merge everything queued, gate once, bisect only on red. Use when the owner wants many agents building rows fast and the old drive.py harness is stalling, or says "fleet-train", "start the train", "run the builders".
---

# fleet-train

Replaces drive.py/supervise.py for the build phase. Measured on the rebuild on 2026-11-11: the old harness proved
79 rows in 64 h (about 1.2/h). This one landed 141 rows in its first 3 h after the first land.

## Why the old harness stalled (do not rebuild these)
- Any build trouble parked a row in "needs-plan" for a separate plan-fix pipeline. 56 of the 61 buildable rows ended up
  there, so 2 rows could start and 16+ slots sat idle. Here the builder OWNS its row: it implements missing deps,
  splits work, fixes stubs. There is no needs-plan exit.
- Every function landed alone through a full gate (about 7.6 builds per land). Here one gate run covers a whole batch.
- A git reference-transaction hook ("overhaul guard") blocks every main update not made by drive.py. Rename it
  (`.git/hooks/reference-transaction` -> `*.disabled`) before the first train.
- Leftover worktrees and per-worktree target dirs filled the disk (433 worktrees, about 135 GB; old cargo caches 350 GB).

## Parts
- `scripts/harness.py` (no LLM): `claim --slot N --wait S` hands out the highest-impact buildable row and prepares a
  fixed per-slot worktree (warm incremental builds); `submit`, `release`; `train --loop` is the land train;
  `status`; `frontier`; `clean-start` / `clean-submit` for cleanup branches (CLEAN-* queue rows);
  `self-review --slot N` runs the train's own duplicate reviewer on a slot's branch (functions it adds vs main).
- `workflows/builders.js`: Claude Code Workflow, N worker loops, each spawning a FRESH builder agent per row
  (args: {slots, max, idle, home, slot_list}). Builders run only their row's tests and clippy on touched crates, then
  `self-review` and fix every duplicate it lists before `submit` (a duplicate caught here costs minutes; at land it
  costs a whole train).
- `workflows/cleanup.js`: one agent per crate group fixes dup blocks, repeated literals, long functions, strict clippy.
- `scripts/remote.py --slot 100`: a second PC as extra builders (claims here, runs `claude -p` there over ssh,
  fetches the branch back, submits).
- `scripts/cloud.py` + `cloud/` (kit templates, copied to `<repo>/tools/cloud/`): the cloud fleet, below.
- `scripts/dash.py`: static dashboard page from state/events.jsonl; publish it as an artifact.

## Train rules (each one came from a real stall)
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

## One harness for every project
- Every project runs `scripts/harness.py` from this skill (or a copy synced from it), never a fork. A fix lands here
  first, then each project's copy is synced. A forked harness misses every train fix (disjoint batches, blame by file,
  unowned-test blame, the health-check heartbeat): that happened on a second project within two days.
- Stack-specific steps (format, lint, strict lint, unit, unit-failure parse, acceptance build and run, failure parse,
  conflict check, function regexes for the reviewer, file-path regexes for blame, target-dir env) all come from one
  profile in `STACKS` at the top of harness.py. Pick it with `HARNESS_STACK=rust` (default) or `node`, or with
  `stack = "node"` in `<HARNESS_HOME>/harness.toml`; a `[profile]` table there overrides any key, for example
  `unit = "npm run test:unit --silent"`, `qa = ["dup_literals.py", "check_cycles.py"]`, `src_dirs = ["lib"]`.
- Rust: cargo test/clippy/rustfmt, crates/ layout, CARGO_TARGET_DIR per slot. Acceptance ids are
  `tests/acceptance/<stem>.rs::<test_fn>`.
- Node: `node --test --test-reporter=tap` (or `npm test` when package.json has a test script), prettier and eslint
  only when the project has them and only on the files a batch changed, NODE_PATH to the repo's node_modules.
  Acceptance ids are `<test file>::<test name>` (`::*` runs the whole file); each file runs once, failures re-run
  alone. The quality scripts are off by default (`qa = []`): turn them on once main passes them.
- Row <-> test mapping is the same on every stack: the spec row's acceptance column lists its test ids (`;`-separated),
  its files column the files it owns (used for blame). Builder briefs (workflows/builders.js, cloud/BUILDER.md) quote
  cargo commands: on another stack, change only their "Done" and lint lines to the profile's commands.
- The heartbeat is `scripts/fleet_beat.py` on every project.

## Cloud fleet (builders in the cloud, only the train local)
- Never run local builders on the gate machine: 16 local builders starved the gate (clippy 13 min, quality 20 min,
  acceptance 28 min; 1 land in 3 h with 81 queued). The local machine runs only the train and `cloud.py collect`.
- Cloud routines (claude.ai/code, one environment, a scheduled prompt "read and do tools/cloud/BUILDER.md") run the
  builders in sandboxes. `cloud.py assign --n N` claims frontier rows under slots 1000+ and publishes
  `assign/<row>.json` on the orphan branch `harness-cloud`; a session claims a row by pushing the new ref
  `cloud/claim-<row>` (atomic: a second push is refused), builds from `origin/cloud-base` (= the train's local main,
  pushed by `collect`; origin/main can lag when a push is refused), self-reviews and pushes `cloud/<row>`.
- `cloud.py collect` (every 5 min) queues new `cloud/<row>` heads for the train, releases `cloud/release-<row>`
  (blocker in REASON; WIP kept as `cloud/prev-<row>`), expires claims silent for HARNESS_CLOUD_TTL (2 h) and tops
  assignments up to HARNESS_CLOUD_TARGET (40). `cloud.py status` shows assigned/claimed/pushed/landed.
- Builders and planners are both routines. A builder claims one assigned row (`BUILDER.md`); a planner claims one phase
  from `plan-tasks.txt` on `harness-cloud` and turns up to 15 roadmap rows into planned rows (`PLANNER.md`). Every claim
  is a UNIQUE commit pushed to a new ref (an identical commit pushes as a no-op "success", so two sessions would both win).
  Everything bases on `cloud-base`, never main. Sessions push `cloud/wip-<row>` + `HANDOFF-<row>.md` often so another
  session or account resumes (`cloud/HANDOFF-ACCOUNT.md`).
- The real caps on agent count are the frontier (ready rows: deps proven, not claimed) and the landing rate of the
  train, not slots. More builders than frontier rows idle; more than the train lands pile up rejects. Run planners to
  grow the frontier, and size builders to min(frontier, what the train lands).
- Setup: copy `cloud/` to `<repo>/tools/cloud/`, fill the "extra system deps" hook in setup.sh, probe one session
  (setup prints SETUP OK), then step 20 -> 50 -> 100 sessions while rejects stay under 30% and the train keeps up.
- Private mirror (not in cloud.py): when cloud sessions cannot reach the real repo, a project can hand rows out on a
  private mirror whose main carries the real main's tree, and land each cloud branch on the real repo as a patch
  (`git diff base..cloud/<row>` applied with `git apply --cached --3way`, then queued like any branch). Keep that as
  a thin wrapper that only moves branches between the mirror and the real repo; the train itself stays this harness.

## Manager loop
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

## Run
1. Set HARNESS_REPO, HARNESS_HOME (state dir parent), HARNESS_WT (fast drive), HARNESS_STACK (rust or node).
2. `py -3 harness.py frontier` must show rows. Free disk first (keep 80 GB+; slot targets about 5-9 GB each, the
   train's release target about 30 GB).
3. Start the train from PowerShell or cmd (never Git Bash), detached from any agent shell: `py -3 scripts/fleet_beat.py`
   starts it when state/train.pid is set and dead; the first time, start it with the quoted full path,
   `"<python>" -u "<full path>\harness.py" train --loop >> state/train.log 2>&1`, through Win32_Process Create.
4. Workflow tool: run workflows/builders.js with args {"slots": 16, "max": 40, "idle": 8, "home": "<HARNESS_HOME>"}.
5. Optional: `py -3 -u remote.py --slot 100` (one per desktop slot), or the cloud fleet instead of steps 4-5.
6. Watch only `train_end`, `train_error` and disk with one Monitor; measure with `harness.py status`
   (landed counts unique rows). The number is rows landed per hour, never agents busy.
