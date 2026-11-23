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
  `status`; `frontier`; `clean-start` / `clean-submit` for cleanup branches (CLEAN-* queue rows;
  `--absorbs a,b` for an integration row that lands other rows' commits on purpose: verified by git, never held for them,
  each absorbed spec row proven by its own tests in the same gate);
  `self-review --slot N` runs the train's own duplicate reviewer on a slot's branch (functions it adds vs main).
  `review-prompt --dir D --base B` prints that reviewer's exact prompt for D's added functions, for a reviewer the
  harness does not start itself (a cloud session's fresh subagent); nothing when D adds no function.
- `workflows/builders.js`: Claude Code Workflow, N worker loops, each spawning a FRESH builder agent per row
  (args: {slots, max, idle, home, slot_list}). Builders run only their row's tests and clippy on touched crates, then
  `self-review` and fix every duplicate it lists before `submit` (a duplicate caught here costs minutes; at land it
  costs a whole train).
- `workflows/cleanup.js`: one agent per crate group fixes dup blocks, repeated literals, long functions, strict clippy.
- `scripts/remote.py --slot 100`: a second PC as extra builders (claims here, runs `claude -p` there over ssh,
  fetches the branch back, submits).
- `scripts/cloud.py` + `cloud/` (kit templates, copied to `<repo>/tools/cloud/`): the cloud fleet, below.
- `scripts/dash.py`: static dashboard page from state/events.jsonl; publish it as an artifact.
- `scripts/finish.py --slot N --name X [--front] [--tests ...]`: a fix lane's one pre-submit step (Lanes, below).
- `scripts/priority_keeper.ps1`: on a Windows machine shared by the train and lanes, keeps the train ahead (Lanes).

## Train rules
Each rule is enforced by the harness; the incident behind it is in CHANGELOG.md.

| Rule | Mechanism |
|---|---|
| A conflicted branch gets one short resolve pass; the rest go back to their builders with the reason | `resolve_conflict` (at most 4 files, no markers left, check clean), HARNESS_RESOLVE_MAX (12) |
| Start at HARNESS_MIN_BATCH rows (8), waiting up to HARNESS_BATCH_WAIT_BUSY (25 min) while claims are in flight; with none in flight, start at once | `train` |
| Rows that each add or edit their own spec table rows, bullets or Cargo `[[test]]` entries never conflict: those files merge unit by unit (rows by id, bullets with their continuation lines, `[[test]]` by name); one key changed differently on both sides stays a conflict for that key only and no key is ever held twice. Line-set .txt files merge as sets; their counted `<key> <count>` entries merge three-way by count (two sides each lowering one key add up; a count that grows conflicts) | `keyed_merge.py` (`merge=keyed` in .gitattributes; `lineset_merge.py`, `merge=lineset`, for line-set .txt files), registered by `ensure_merge_drivers`; another clone runs `git config merge.keyed.driver "py -3 <scripts>/keyed_merge.py %O %A %B %L %P"` once |
| Shrink-only lists only shrink. The product owns them: `path count` lines in tracked .txt files (merge=lineset), one assert helper every such gate test calls (real count <= listed, unlisted 0; never `==`, which a merged tree breaks), each test named `*only_shrink*` and printing its list tightened to the real counts between `--- tightened <repo path> ---` and `--- end tightened ---`. Git takes two identical edits without the driver, so two rows lowering one line alike leave it stale-high: after each land the train runs `[profile] shrink_cmd` on the proven tree and lowers every printed tracked list, never raising a count or adding a line | `tighten_shrink_lists` in `finish_spec` (events shrink_lists_tightened / shrink_lists_error; off while `shrink_cmd` is ""; rust example `cargo test -q --workspace --lib --bins --no-fail-fast -- only_shrink --nocapture`) |
| A batch holds at most HARNESS_BATCH_CAP rows (20), picked by trial merges so rows never conflict in the gate | `disjoint()` (git merge-tree, file-disjoint fallback) |
| Under HARNESS_FILL_MIN clean picks (5) while queued rows are out for cloud rebase, hold up to HARNESS_FILL_WAIT (10 min, 0 = off) for them; a CLEAN-* pick never waits. Tuned for a ~25-min gate cycle; a ~45-min one wants about 8 and 20 min | `fill_wait()` (train_idle "filling: ...") |
| Gate once per batch: format (auto-fixed in file batches), clippy -D warnings (skipped when the quality stage runs), unit, then acceptance of every proven row plus the batch, built once in release and run as parallel binaries | `gate()` |
| Rerun before blaming: a failed test reruns alone; perf-budget failures go to state/perf-debt.jsonl; stale fixture locks are cleared and rerun once; unit failures main already has are not blamed; up to 5 new unit failures rerun single-threaded; one parser reads every cargo format and keeps a test binary that crashed naming no test by its selector (rerun alone, never an empty set) | `gate()`, state/unit-baseline.json, `unit_flaky`, scripts/cargo_failures.py |
| Main-red tests are nobody's fault: a failing test no batch row owns runs alone on main first; red there, it is quarantined (state/accept-known-red.json + a fix request in notes/requests/), the gate skips it and the same batch is re-gated; over 3 at once the train pauses until main moves; it returns to the gate once it passes alone on main | `quarantine.py` (quarantine / quarantine_cap / unquarantine) |
| Blame by entry, by file, then probe, then bisect. A branch's own change is what its train merge adds over the rows merged ahead of it (a stacked row never owns its parent's). The red unit log carries each failing test's whole captured block (its assert sides survive however far from the tail it printed). An assert diff's differing entries eject, in ONE step with one shared reason, every row whose own change includes one (the entry's file, or its line in a list the failing test includes: profile `include_re`); the rest gates as one run, rows lowering other lines of that list stay. Else file blame: an assert diff names only its entries, else every file the failure names; exactly one suspect is ejected, more are bisected; an unowned failing test blames the branch touching a file its source names; an acceptance-only red bisects with only the failing tests first | `blame_entries` (blame_entries), `unit_red_log` (`cargo_failures.captured`), `blame_by_file` (blame_by_file / blame_suspects), `probe_culprit` (probe_found / probe_none) |
| Acceptance on two machines (cargo runner, opt-in with state/desk-on, HARNESS_DESK=0/1 overrides): units are placed by measured throughput (state/unit-rates.json: per machine and unit kind, the median of the last 3 wall-seconds-per-cost-second samples, learned from each gate; this machine's age out after 12 h): this one takes a unit only when it is not slower at it and its expected finish is no later than the desk's with every unit, else the desk (HARNESS_REMOTE_HOST) runs every unit in order; idle, this one steals an unstarted unit it is not slower at. No fresh history here: cold start, this one runs the parallel unit unless over 70% CPU at gate start (`BUSY_LOAD`). A desk unit with no verdict (error, late, other head or tests, `env_fail_re`) runs here and drops the desk for the gate; desk down: all here. Under 60 tests: all here | `desk.py` (`desk.run`, kit and bundle sent over ssh/scp, event `gate_stage` acceptance-desk) |
| Every test run on the desk (any runner but cargo; on with `[desk] host` in harness.toml or state/desk-on, HARNESS_DESK=0/1 overrides): the gate's unit (full and quick), the main unit baseline, unit_flaky reruns, acceptance, probe and run_alone go to the desk at the train tree's HEAD as one job each; this machine keeps the review, regen, format, quality, every push and every credential. No verdict (down, late, an error, another head, `env_fail_re`) is never a pass, a fail or a run here: the train defers the batch, rows stay queued | `desk.job` (ops tests, alone, cmd), `unit_runs`, events `desk_job` / `desk_error` / `desk_deferred` |
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
  caps, new plan_validate problems (the gate: every batch; the pick check: check_stubs on the rows a row's diff
  reaches, so a row spending another planned row's stub is rejected before a batch). The pick check also runs the profile's `pick_build`
  (rust: the unit stage's `cargo check --profile test`, own warm target) on main + the row, once per main: a row whose base
  went stale under a landed row is rejected before it turns a gate red. Pick acceptance (`harness.py pick-accept`,
  scheduled; on with state/pick-accept-on): the desk runs each queued row's owner tests (proven rows owning a changed
  file) on the code the next pick sees, outside acceptance (the gate kills it); a red verdict is rejected at pick. Ratchet: no file may pass its count in state/rules-debt.json, which
  only shrinks (`tighten` after each land; the product's own shrink-only lists: `tighten_shrink_lists`). New project:
  `py -3 rules_gate.py init` on main once. Languages: rules_langs.py.
- `scripts/brevity_check.py` (fleet_beat BREVITY alarm): word caps on STATE.md, briefs, hand-backs, rulings, notes/,
  code comments; override in `<HARNESS_HOME>/brevity_budget.json`, never raise.
- `harness.py note "<text>"` appends one line to notes/log/YYYY-MM.md without reading it; briefs end with it.
- A proven row's bullet and spec/packets line are deleted at land. Ids in state/held-rows.txt are never handed out.
- Builders find owners with `index_api.py --find "<words>"`, never a raw grep.

## Lanes (fix lanes on the train's machine)
- A lane finishes with `finish.py` and nothing else: the train's checks on its committed branch, cheapest first
  (clean tree, merge-tree against a fresh origin/main, self-review twice, static checks, path and source-scan pins:
  tests under `pin_dirs` quoting a file it changes or moves or a directory holding one, the shrink-only ratchets of
  every crate, unit and strict lint scoped to the touched crates; FINISH_SCOPE=workspace for core and cross-cutting
  lanes runs the full unit and quality stages), then waits while the running train gates the row (a resubmit
  supersedes the whole run) and calls `clean-submit`. FINISH_NO_SUBMIT=1 gates only. A lane never copies the script to
  change a step: the step goes into finish.py for every lane.
- Every finish build waits while the train marks a compile stage (`train_stage`: clippy, unit, quality; at most
  FINISH_TRAIN_WAIT, 2700 s; the acceptance build sits inside the acceptance mark and is not waited out) and holds one
  machine-wide build slot (`cargo_slots.py`, FLEET_CARGO_CAP). Run
  `priority_keeper.ps1` (HARNESS_HOME, FLEET_LANE_PROC_RE): the state/train.pid tree AboveNormal, lane builds BelowNormal,
  Idle during a compile stage. Pilot: with 8 lanes building, the gate's quality stage went from 6-9 to 19-28 min.
- Lane scopes are exclusive (by crate or package): two lanes on one set of records each add a typed copy.
- Briefs say: never `git stash` (all worktrees share one stash list; commit a red test alone instead); every wait is
  one foreground until-loop at the tool's longest timeout (a default 2-min timeout spends a call per wait); temp dirs
  come from one test-kit helper that hands back a new empty dir (never named by pid: Windows reuses pids); lock and
  timing tests inject the clock and assert outcomes, never elapsed time.

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
  assignments up to HARNESS_CLOUD_TARGET (40). `cloud.py status` shows assigned/claimed/pushed/landed. Never stop
  `collect` while rows are queued: it pushes cloud-base, and rebasers would rebase onto a stale base.
- Builders and planners are both routines. A builder claims one assigned row (`BUILDER.md`); a planner claims one phase
  from `plan-tasks.txt` on `harness-cloud` and turns up to 15 roadmap rows into planned rows (`PLANNER.md`). Every claim
  is a UNIQUE commit pushed to a new ref (an identical commit pushes as a no-op "success", so two sessions would both win).
  Everything bases on `cloud-base`, never main. Sessions push `cloud/wip-<row>` + `HANDOFF-<row>.md` often so another
  session or account resumes (`cloud/HANDOFF-ACCOUNT.md`).
- Fixers are a third routine, for clean-up packets: the manager lists task ids in `state/fix-tasks.txt` (text in
  `state/fix-tasks/<id>.md`); `collect` publishes `fix-tasks.txt`, the task texts and `fix-check.sh` (the train's
  checks plus a merge check with the base; it stamps the passing tree). A session claims one task with
  `claim_task.sh fix` (planners use `claim_task.sh plan`: one claim script), follows `FIXER.md` (red test first, fix
  in the owner, self-review twice) and pushes `fix/cloud-<task>`; a stamped tip queues as CLEAN-fix-<task> ahead of
  every row (`queue_clean(front=True)`, also `clean-submit --front`), anything else goes back to
  `cloud/wip-fix-<task>` with `fix-rejects/<task>.md`. `cloud.py fixes` shows the lane. One `Lane` type carries
  every task lane, so a planner collector is the same Lane with a todo of unplanned phases.
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
  TRAIN-ERROR, DISK, BEAT-TIMEOUT, KNOWN-RED, REPEAT-TEST, ORIGIN-BEHIND; the last three in `beat_alarms.py`). Every alarm
  has a handler in `scripts/alarm_handlers.py` (one table; a test fails on an alarm without one) and
  `scripts/alarm_dispatch.py` runs them: install it as a scheduled task every 10 min (it runs fleet_beat first).
  Auto: an idempotent, time-boxed fix (push origin, free disk, release stale claims, archive finished briefs).
  Agent: one headless `claude -p` per alarm from `scripts/alarm_briefs/<template>.md` (RULE ZERO on top), at most 2
  at once, CARGO_BUILD_JOBS=2. Its lock holds pid, process start time and a deadline; past it the tree is killed. The
  check judges each attempt (alarm still firing unchanged = failed), a cooldown follows, 2 failed = ESCALATE until the
  alarm clears or `alarm_dispatch.py --reset KEY`. fleet_beat shows each alarm as handled-by/cooldown/ESCALATE; the
  manager fixes only ESCALATE items. With the dispatcher off no auto-fix runs, DISK included: the manager frees disk
  by hand. A failure mode found by hand gets a new check in fleet_beat.py.
- An owner card or dashboard reading state/health.json shows its alarms only while `ts` is under 20 min old, else
  "beat off since HH:MMZ" (stale alarms read as live ones); a pause shows the first line of state/pause as its reason.
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
- A second gate lane, if a project adds one, gets only the picks beyond HARNESS_BATCH_CAP, never half of a smaller
  batch: it lands only when lane one lands whole.
- Rebaser routine (`cloud/REBASER.md`): cloud agents rebase queued rows that stopped merging onto main. Fire it the
  moment a train lands: that is when queued rows stop merging.
- Rows owned by a fix lane (CLEAN-*, `cloud.lane_owned`) are never sent to the rebasers: a rebase onto cloud-base
  rewrites the lane's gated sha and breaks a stack on another queued CLEAN row. `send_rebase` is the one place a row
  goes out and refuses them (`rebase_skip_lane_owned`); a conflicting one waits for its lane to restack it.

## Run
1. Set HARNESS_REPO, HARNESS_HOME (state dir parent), HARNESS_WT (fast drive), HARNESS_STACK (rust or node).
2. Rename an old overhaul guard hook (`.git/hooks/reference-transaction` -> `*.disabled`): it blocks every main update
   the train makes. `py -3 harness.py frontier` must show rows. Free disk first (keep 80 GB+; slot targets about 5-9 GB each, the
   train's release target about 30 GB). Prune acceptance tests' leftover temp dirs too (they once filled 26 GB).
3. Start the train from PowerShell or cmd (never Git Bash), detached from any agent shell: `py -3 scripts/fleet_beat.py`
   starts it when state/train.pid is set and dead; the first time, start it with the quoted full path,
   `"<python>" -u "<full path>\harness.py" train --loop >> state/train.log 2>&1`, through Win32_Process Create.
4. Workflow tool: run workflows/builders.js with args {"slots": 16, "max": 40, "idle": 8, "home": "<HARNESS_HOME>"}.
5. Optional: `py -3 -u remote.py --slot 100` (one per desktop slot), or the cloud fleet instead of steps 4-5. Desk
   acceptance: a Windows second machine with ssh (cmd shell), Python, git, the toolchain and `<HARNESS_REMOTE_ROOT>
epo.git`
   (bare); set HARNESS_REMOTE_HOST, HARNESS_REMOTE_ROOT, HARNESS_REMOTE_ENV (its toolchain env, for builders and desk
   runs alike), then create state/desk-on; or a `[desk]` table in harness.toml (host, root, env, path: remote dirs put
   first on PATH, e.g. a node pinned to this machine's major), which turns it on. Seed repo.git once with a bundle of main
   and set refs/desk/base to that sha here, or the first send carries the whole history. Keep the desk otherwise idle while desk-on exists: its units are the
   perf-sensitive tests.
6. Watch only `train_end`, `train_error` and disk with one Monitor; measure with `harness.py status`
   (landed counts unique rows). The bar is functions proven on main per wall-clock hour, never rows per train-second
   or agents busy.
