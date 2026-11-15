---
name: fleet-train
description: Run many short-lived builder agents (local or cloud) on a planned rebuild's spec/FUNCTIONS.md rows with a batch land train that merges the queue, gates once and bisects only on red. Use when the owner says "fleet-train", "start the train", "run the builders", or wants rows built fast.
---
> **RULE ZERO (the most important rule, ever):** the work must be valuable, well made, clean code that can be built
> out and extended. A very solid foundation beats functionality depth: missing depth is acceptable, a weak foundation
> is not. One owner per capability, typed data models, stable ids, clear extension seams, no second copy of anything;
> never trade code quality, tests or gates for speed or feature count. Every brief this skill writes for an agent
> quotes this rule at its top.


# fleet-train

Replaces drive.py/supervise.py for the build phase. The builder OWNS its row (implements missing deps, splits work,
fixes stubs): there is no needs-plan exit, and one gate run covers a whole batch.

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

## Train rules
Each rule is enforced by the harness; the incident behind it is in CHANGELOG.md.

| Rule | Mechanism |
|---|---|
| A conflicted branch gets one short resolve pass; the rest go back to their builders with the reason | `resolve_conflict` (at most 4 files, no markers left, check clean), HARNESS_RESOLVE_MAX (12) |
| Start at HARNESS_MIN_BATCH rows (8), waiting up to HARNESS_BATCH_WAIT_BUSY (25 min) while claims are in flight; with none in flight, start at once | `train` |
| A batch holds at most HARNESS_BATCH_CAP rows (20), picked by trial merges so rows never conflict in the gate | `disjoint()` (git merge-tree, file-disjoint fallback) |
| Gate once per batch: format (auto-fixed in file batches), clippy -D warnings (skipped when the quality stage runs), unit, then acceptance of every proven row plus the batch, built once in release and run as parallel binaries | `gate()` |
| Rerun before blaming: a failed test reruns alone; perf-budget failures go to state/perf-debt.jsonl; stale fixture locks are cleared and rerun once; unit failures main already has are not blamed; up to 5 new unit failures rerun single-threaded | `gate()`, state/unit-baseline.json, `unit_flaky` |
| Blame by file, then probe, then bisect: files named in a failure eject only the branches touching them; an unowned failing test blames the branch touching a file its source names; an acceptance-only red bisects with only the failing tests first | blame, `probe_culprit` (probe_found / probe_none) |
| No train without the stack's required env | `[profile] require_env` (rust on Windows: ProgramFiles(x86), which Git Bash drops) |
| Quality has no switch. Every claim carries `must_also_before_commit` (search spec/index and the ARCHITECTURE owners first; run dup_blocks, dup_literals, shape_check, check_cycles, strict clippy). The gate runs those plus the owner guard (allowlist stays EMPTY: replace or rename, never grandfather) and the size gate; `batch_review` rejects a branch that duplicates an existing owner under any name; the pre-push hook refuses any push to main whose commit fails the quality check | claim JSON, `owner_copies`, `oversized_files` (HARNESS_FILE_LINES 500, tests 600; files already over are grandfathered until split), `batch_review`, `scripts/pre-push.hook` -> `<repo>/.git/hooks/pre-push` |
| The index builders search is never stale | spec/INDEX.md regenerated on every land |
| A codebase grown without these checks reaches 0 duplicates before any builder starts | workflows/cleanup.js, a semantic duplicate review, targeted refactors |
| At most 3 kernel rows and 6 rows per module in flight, no two with overlapping declared files; rows unblocking 8+ others skip the module cap; priority rows go first and skip caps | `claim`, state/priority.json |
| A row whose deps are queued (not landed) starts on main plus those branches | stacked starts in `claim` |
| The loop never dies on an exception, re-execs when harness.py changes and still parses, and reads events tolerantly | `train --loop`, `read_events` |
| Freeze a working harness: one tested change at a time, a dated backup, revert if the land rate drops | the manager |
| Land first: builders scale up only once the queue drains | fleet_beat FIRE cap (8 while 20+ queued, else 30), SUPPLY alarm |

## One harness for every project
- Every project runs `scripts/harness.py` from this skill (or a copy synced from it), never a fork. A fix lands here
  first, then each project's copy is synced. A fork misses every train fix.
- Stack-specific steps (format, lint, strict lint, unit, unit-failure parse, acceptance build and run, failure parse,
  conflict check, function regexes for the reviewer, file-path regexes for blame, target-dir env) all come from one
  profile in `STACKS` at the top of harness.py. Pick it with `HARNESS_STACK=rust` (default) or `node`, or with
  `stack = "node"` in `<HARNESS_HOME>/harness.toml`; a `[profile]` table there overrides any key, for example
  `unit = "npm run test:unit --silent"`, `qa = ["dup_literals.py", "check_cycles.py"]`, `src_dirs = ["lib"]`.
- Generated files: `regen = ["<shell command>", ...]` in `[profile]` (optional, default none) runs each command in the
  train tree after the batch is merged and formatted, before the gate; changes are committed as "train: regen" so the
  land commit holds files regenerated from the merged batch (rows regenerating on their own base leave main stale). A
  non-zero exit or timeout (600 s) logs a `regen_error` event and fails the batch's quality stage. Example:
  `regen = ["py -3 tools/gen_index.py", "py -3 tools/gen_catalogue.py"]`.
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

## Clean code and brevity
- `scripts/rules_gate.py` runs in the quality stage, pre-push and cloud sessions: swallowed errors, expect/panic,
  silenced lints, a type named like another module's `Forbidden types:` stem, spec text over overhaul.toml `[text]`
  caps, new plan_validate problems on spec edits. Ratchet: no file may pass its count in state/rules-debt.json, which
  only shrinks (`tighten` after each land). New project: `py -3 rules_gate.py init` on main once. Languages: rules_langs.py.
- `scripts/brevity_check.py` (fleet_beat BREVITY alarm): word caps on STATE.md, briefs, hand-backs, rulings, notes/,
  code comments; override in `<HARNESS_HOME>/brevity_budget.json`, never raise.
- `harness.py note "<text>"` appends one line to notes/log/YYYY-MM.md without reading it; briefs end with it.
- A proven row's bullet and spec/packets line are deleted at land. Ids in state/held-rows.txt are never handed out.
- Builders find owners with `index_api.py --find "<words>"`, never a raw grep.

## Cloud fleet (builders in the cloud, only the train local)
- Never run local builders on the gate machine: they starve the gate. The local machine runs only the train and
  `cloud.py collect`.
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
- Only the manager chat can start cloud sessions (a routine run). It runs an hourly in-chat heartbeat:
  `py -3 scripts/fleet_beat.py` compares rows waiting for a session with sessions running and prints `FIRE n`; fire
  that many runs (capped per beat). Scale with several routines on staggered minutes, never by firing runs by hand.
- The heartbeat is a health check, not a liveness check. fleet_beat prints ALARMS (TRAIN-DOWN, TRAIN-PID,
  TRAIN-KILLABLE, TRAIN-ENV, STALL, LONG-BATCH, REPEAT-RED, MAIN-RED, REJECTS, REWORK, STALE-CLAIMS, OLD-QUEUE,
  TRAIN-ERROR, DISK, BEAT-TIMEOUT), each naming its fix; the manager fixes every one in the same beat. A failure mode
  found by hand gets a new check in fleet_beat.py.
- Train process: `train --loop` writes state/train.pid. TRAIN-DOWN restarts it detached through WMI (Win32_Process
  Create, HARNESS_* vars on its command line), never Start-Process or Popen from an agent shell (the child dies with
  the shell's job). Restart and kill scripts act only on their own state/train.pid tree (`taskkill /T /PID`), never on
  a machine-wide 'harness.py train' pattern; TRAIN-KILLABLE fires when this train's command line matches one. TRAIN-ENV:
  the last gate failed on the toolchain env (stack `env_fail_re`); a gate older than the running train or batch is
  ignored. state/train.off = stopped on purpose, no restart.
- Dashboard: 3 h or more with nothing landed and rows queued is RED ("Nothing landed in N h").
- Frontier: only rows being built (claims) count against the per-module cap and file-overlap filter, never queued
  rows. Check `harness.py frontier` against the count of rows whose deps are all proven or queued.
- NEVER attach a GitHub push webhook to a builder routine: builders push, so every push starts another builder. If one
  is attached by mistake, disable that routine at once.
- Rebaser routine (`cloud/REBASER.md`): cloud agents rebase queued rows that stopped merging onto main.

## Run
1. Set HARNESS_REPO, HARNESS_HOME (state dir parent), HARNESS_WT (fast drive), HARNESS_STACK (rust or node).
2. Rename an old overhaul guard hook (`.git/hooks/reference-transaction` -> `*.disabled`): it blocks every main update
   the train makes. `py -3 harness.py frontier` must show rows. Free disk first (keep 80 GB+; slot targets about 5-9 GB each, the
   train's release target about 30 GB).
3. Start the train from PowerShell or cmd (never Git Bash), detached from any agent shell: `py -3 scripts/fleet_beat.py`
   starts it when state/train.pid is set and dead; the first time, start it with the quoted full path,
   `"<python>" -u "<full path>\harness.py" train --loop >> state/train.log 2>&1`, through Win32_Process Create.
4. Workflow tool: run workflows/builders.js with args {"slots": 16, "max": 40, "idle": 8, "home": "<HARNESS_HOME>"}.
5. Optional: `py -3 -u remote.py --slot 100` (one per desktop slot), or the cloud fleet instead of steps 4-5.
6. Watch only `train_end`, `train_error` and disk with one Monitor; measure with `harness.py status`
   (landed counts unique rows). The number is rows landed per hour, never agents busy.
