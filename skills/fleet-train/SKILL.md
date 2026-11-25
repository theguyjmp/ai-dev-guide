---
name: fleet-train
description: Run many short-lived builder agents (local or cloud) on a planned rebuild's spec/FUNCTIONS.md rows and land their work through the merge-first landing cycle (writers push to the trunk and main after their affected checks; a sharded verifier runs the full suite behind and moves `verified`, from which releases come). Use when the owner says "fleet-train", "start the train", "run the builders", or wants rows built fast.
---
> **RULE ZERO (the most important rule, ever):** the work must be valuable, well made, clean code that can be built
> out and extended. A very solid foundation beats functionality depth: missing depth is acceptable, a weak foundation
> is not. One owner per capability, typed data models, stable ids, clear extension seams, no second copy of anything;
> never trade code quality, tests or gates for speed or feature count. Every brief this skill writes for an agent
> quotes this rule at its top.


# fleet-train

Replaces drive.py/supervise.py for the build phase. The builder OWNS its row (implements missing deps, splits work,
fixes stubs): there is no needs-plan exit.

**Landing is the cycle in CYCLE.md, its one owner:** merge first. A push reaches the `integ` trunk and `main` after
its affected checks; the verifier runs the full suite behind, sharded over cloud sessions (the native-OS part on a
native box in parallel), with batch-first bisecting, and moves `verified` only to green; a red is blamed and reverted
on top of main. Releases come only from `verified`. The batch land train (`harness.py train --loop`) is retired: one
serial gate everyone waited on landed ~1.5 packets/h (CHANGELOG 2026-11-24). Create `state/train.off` in every
project so nothing restarts it.

**Before tuning the pipeline, question the work list (CYCLE.md):** fix by problem class (one gate lands first, then
whole-codebase sweeps on non-overlapping module groups; one packet per instance is refused by the task loader), no
shared append points (generated lists and indexes; agents never hand-edit a shared list), and claims only for tasks
whose footprint overlaps nothing in flight and whose prerequisites landed.

## Parts
- `scripts/harness.py` (no LLM): `claim --slot N --wait S` hands out the highest-impact buildable row and prepares a
  fixed per-slot worktree (warm incremental builds); `release`, `status`, `frontier`; `self-review --slot N` runs the
  duplicate reviewer on a slot's branch (functions it adds vs its base); `review-prompt --dir D --base B` prints that
  reviewer's prompt for a reviewer the harness does not start (a cloud session's fresh subagent). Its gate stages,
  known-red rule, quarantine, blame leaves and lock are the pieces the cycle's verifier is built on (CYCLE.md "What
  ships"). `submit`, `clean-submit` and `train` feed the retired queue.
- `workflows/builders.js`: Claude Code Workflow, N worker loops, each spawning a FRESH builder agent per row (args:
  {slots, max, idle, home, slot_list}). Builders run only their row's tests and strict lint on touched packages, then
  `self-review` and fix every duplicate it lists. Its last step still calls `submit`: point it at the project's push
  script when the cycle is built.
- `workflows/cleanup.js`: one agent per crate group fixes dup blocks, repeated literals, long functions, strict lint.
- `scripts/remote.py --slot 100`: a second PC as extra builders (claims here, runs `claude -p` there over ssh).
- `scripts/cloud.py` + `cloud/` (kit templates, copied to `<repo>/tools/cloud/`): the cloud fleet, below.
- `scripts/dash.py`: static dashboard page from state/events.jsonl; publish it as an artifact.
- `scripts/finish.py --slot N --name X [--tests ...]`: a lane's local check (Lanes, below); FINISH_NO_SUBMIT=1.
- `scripts/priority_keeper.ps1`: on a Windows machine shared by a gate and lanes, keeps the gate's process tree ahead.

## Gate rules (the verifier's full gate and the writer floor)
Each rule is enforced in code; the incident behind it is in CHANGELOG.md.

| Rule | Mechanism |
|---|---|
| `main` follows the trunk after a push's affected checks; `verified` moves only by fast-forward to a sha the full gate and the duplicate review passed, and only the verifier moves it; releases come from `verified` | CYCLE.md (receipts, CAS, batch-first bisecting) |
| The full gate runs every acceptance and property test in the tree, owned by a row or not; only tests of rows not built yet are excluded. One function owns that scope and the writer's selection reads it | a gate-scope function under `gate_tests` (to build: the shipped `gate_tests` still selects proven rows' tests only, which skipped half the files; widen it first) |
| Gate stages: format, strict lint, workspace unit, static quality, acceptance + property built once in release and run as parallel binaries | `gate()` |
| Rows that each add or edit their own spec rows, bullets or Cargo `[[test]]` entries never conflict: those files merge unit by unit; one key changed differently on both sides stays a conflict for that key only. Line-set .txt files merge as sets; counted `<key> <count>` entries merge three-way by count | `keyed_merge.py` (`merge=keyed`), `lineset_merge.py` (`merge=lineset`), registered by `ensure_merge_drivers`; another clone runs `git config merge.keyed.driver "py -3 <scripts>/keyed_merge.py %O %A %B %L %P"` once |
| Shrink-only lists only shrink: `path count` lines in tracked .txt files (merge=lineset), one assert helper every such gate test calls (real <= listed, unlisted 0; never `==`), each test named `*only_shrink*` and printing its tightened list between `--- tightened <path> ---` and `--- end tightened ---`. After a green gate the verifier runs `[profile] shrink_cmd` on the proven tree and lowers every printed list in its bookkeeping push | `tighten_shrink_lists` (events shrink_lists_tightened / shrink_lists_error; off while `shrink_cmd` is "") |
| Rerun before blaming: a failed test reruns alone; perf-budget failures go to state/perf-debt.jsonl; stale fixture locks are cleared and rerun once; one parser reads every cargo format and keeps a crashed binary that named no test by its selector | `gate()`, `unit_flaky`, scripts/cargo_failures.py |
| One known-red rule, read by writer and verifier: a failing test no push owns runs alone on main; red there, it is quarantined (accept-known-red + a fix request) and skipped. When every failure is proven red alone on main (a test not built on main is no proof), the verdict stands. A failing test whose file is not on main is the push's own | `known_red_tests()`, `quarantine.py` (heal, `Healed.covers`, `on_main`) |
| Blame by assert entry, then by file, then a probe at each push's own sha (each push blamed on its own `base..sha`, never a cumulative range: to build, `own_ranges` takes one range today). A probe reproduces only the same stage or test red at the probed sha with that sha's own rows | `blame_entries`, `blame_by_file`, CYCLE.md "Red" |
| No verdict is never a pass or a fail: a native-box job that is down, late, on another head, an env failure, a build refusing a target, a shard with 0 tests or a short census | `desk.job`, CYCLE.md census |
| No gate without the stack's required env | `[profile] require_env` (rust on Windows: ProgramFiles(x86), which Git Bash drops) |
| Quality has no switch. Every claim carries `must_also_before_commit` (index search and ARCHITECTURE owners first; dup_blocks, dup_literals, shape_check, check_cycles, strict lint). The gate adds the owner guard (allowlist stays EMPTY) and the size gate; the duplicate review rejects a copy of an existing owner under any name; the pre-push hook refuses a push to main whose commit fails the quality check | claim JSON, `owner_copies`, `oversized_files` (HARNESS_FILE_LINES 500, tests 600), `batch_review`, `scripts/pre-push.hook` |
| The index builders search is never stale; no shared list is hand-edited | spec/INDEX.md and every generated list regenerated in the verifier's bookkeeping push; test registration by discovery (overhaul-build derived group roots) |
| A recurring defect type is fixed as a class: its gate (lint, structural test, shrink-only ratchet or one owner) lands first, then sweeps; a per-instance packet is never published | the task loader's class check (CYCLE.md "Fix by problem class") |
| A codebase grown without these checks reaches 0 duplicates before any builder starts | workflows/cleanup.js, a semantic duplicate review, targeted refactors |
| At most 3 kernel rows and 6 rows per module in flight, no two with overlapping declared files; rows unblocking 8+ others skip the module cap; priority rows go first | `claim`, state/priority.json |
| A row whose deps are pushed but not yet verified starts from the trunk's head | the project's claim base |
| Every long-running loop survives exceptions, re-execs when its source changes and still parses, and reads events tolerantly | the verifier loop, `read_events` |
| Freeze a working harness: one tested change at a time, a dated backup, revert if the verified rate drops | the manager |
| Writers scale to the account's session limit; the verifier never blocks them. Watch send-backs, reverts and holds, not a queue | `fleet_beat` FIRE cap, the cycle's status |

## One harness for every project
- Every project runs `scripts/harness.py` from this skill (or a copy synced from it), never a fork. A fix lands here
  first, then each project's copy is synced. The cycle's modules call its leaves; they never copy one.
- Stack-specific steps (format, lint, strict lint, unit, failure parse, acceptance build and run, conflict check,
  function regexes for the reviewer, file-path regexes for blame, target-dir env) come from one profile in `STACKS`
  at the top of harness.py. Pick it with `HARNESS_STACK=rust` (default) or `node`, or `stack = "node"` in
  `<HARNESS_HOME>/harness.toml`; a `[profile]` table there overrides any key (`unit = "npm run test:unit --silent"`,
  `qa = ["dup_literals.py", "check_cycles.py"]`, `src_dirs = ["lib"]`).
- Generated files: `regen = ["<shell command>", ...]` in `[profile]` runs each command in the gate tree before the
  gate and commits the changes; a non-zero exit or a 600 s timeout logs `regen_error` and fails the quality stage.
- Rust: cargo test/clippy/rustfmt, crates/ layout, CARGO_TARGET_DIR per slot. Acceptance ids are
  `tests/acceptance/<stem>.rs::<test_fn>`.
- Node: `node --test --test-reporter=tap` (or `npm test`), prettier and eslint only when the project has them and only
  on changed files, NODE_PATH to the repo's node_modules. Acceptance ids are `<test file>::<test name>`. The quality
  scripts are off by default (`qa = []`): turn them on once main passes them.
- Row <-> test mapping on every stack: the spec row's acceptance column lists its test ids (`;`-separated), its files
  column the files it owns (used for blame). Builder briefs quote cargo commands: on another stack change only their
  "Done" and lint lines.
- The heartbeat is `scripts/fleet_beat.py` on every project.

## Clean code and brevity
- `scripts/rules_gate.py` runs in the quality stage, the writer floor, pre-push and cloud sessions: swallowed errors,
  expect/panic, silenced lints, a type named like another module's `Forbidden types:` stem, spec text over
  overhaul.toml `[text]` caps, new plan_validate problems (check_stubs on the rows a diff reaches: a row spending
  another planned row's stub is refused). Ratchet: no file may pass its count in state/rules-debt.json, which only
  shrinks (`tighten` after each green gate). New project: `py -3 rules_gate.py init` on main once. Languages:
  rules_langs.py.
- `scripts/brevity_check.py` (fleet_beat BREVITY alarm): word caps on STATE.md, briefs, hand-backs, rulings, notes/,
  code comments; override in `<HARNESS_HOME>/brevity_budget.json`, never raise.
- `harness.py note "<text>"` appends one line to notes/log/YYYY-MM.md without reading it; briefs end with it.
- A proven row's bullet and spec/packets line are deleted when `verified` holds it. Ids in state/held-rows.txt are never
  handed out. Builders find owners with `index_api.py --find "<words>"`, never a raw grep.

## Lanes (local writers)
- A lane lands through the one push script (CYCLE.md "Writer"); `finish.py` is the shared local check list it runs,
  cheapest first: clean tree, merge-tree against a fresh origin/main (the trunk commit it rebased onto, once built),
  self-review twice, static checks, path and source-scan pins of what it changes, unit and strict lint on the touched
  packages. A lane never copies the script to change a step:
  the step goes into the shared script for every lane.
- Every lane build holds one machine-wide build slot (`cargo_slots.py`, FLEET_CARGO_CAP); a gate sharing the machine
  runs above them (`priority_keeper.ps1`). Measured: 8 lanes building tripled a gate's quality stage.
- Lane scopes are exclusive (by crate or package): two lanes on one set of records each add a typed copy.
- Briefs say: never `git stash` (all worktrees share one stash list; commit a red test alone instead); every wait is
  one foreground until-loop at the tool's longest timeout; temp dirs come from one test-kit helper that hands back a
  new empty dir (never named by pid: Windows reuses pids); lock and timing tests inject the clock and assert outcomes.

## Cloud fleet
- Never run local builders on a gate machine: they starve the gate. The local machine runs the verifier side and the
  collector; the full suite runs sharded in the cloud plus the native box (CYCLE.md).
- Cloud routines (claude.ai/code, one environment, a scheduled prompt "read and do tools/cloud/BUILDER.md") run the
  writers. `cloud.py assign --n N` claims frontier rows under slots 1000+ and publishes `assign/<row>.json` on the
  orphan branch `harness-cloud`; a session claims a row by pushing the new ref `cloud/claim-<row>` (atomic: a second
  push is refused) and builds from `origin/cloud-base` (the trunk's head, refreshed by `collect`).
- Every claim is a UNIQUE commit pushed to a new ref (an identical commit pushes as a no-op "success", so two
  sessions would both win). Sessions push `cloud/wip-<row>` + `HANDOFF-<row>.md` often so another session or
  account resumes (`cloud/HANDOFF-ACCOUNT.md`). `collect` (every 5 min) expires claims silent for HARNESS_CLOUD_TTL
  (2 h) and tops assignments up to HARNESS_CLOUD_TARGET (40). Never stop `collect` while sessions work: it refreshes
  `cloud-base`, and sessions would build on a stale base.
- Planners (`PLANNER.md`) turn roadmap rows into planned rows; fixers (`FIXER.md`) take clean-up packets (one per
  problem class and module group) from `fix-tasks.txt`. Both claim with `claim_task.sh <kind>`; one `Lane` type
  carries every task lane. Only listed tasks are claimable: the loader lists a task when its prerequisites landed
  and its footprint overlaps nothing in flight (CYCLE.md "Claiming"; the shipped script still takes the first free).
- Sessions seed the shared warm build cache before their first build (CYCLE.md); a cold build is never a stop.
- A session lands by running the writer checks on its VM and pushing its branch plus an evidence commit; `collect`
  replays the push with that evidence, git only (CYCLE.md "Cloud writers"). Work that comes back goes to its WIP ref
  with the reason, and the waiting land script lands it again. The rebaser routine (`REBASER.md`) is retired with
  the train: a conflicting push goes back to its writer.
- The real caps on agent count are the frontier (ready rows: deps proven, not claimed) and what the verifier
  verifies, not slots. Run planners to grow the frontier. Only rows being built (claims) count against the module cap
  and file-overlap filter; check `harness.py frontier` against the rows whose deps are all proven or pushed.
- Private mirror (not in cloud.py): when sessions cannot reach the real repo, hand rows out on a private mirror whose
  main carries the real tree and move each finished branch over as a patch (`git diff base..branch` applied with
  `git apply --cached --3way`), then push it as any writer does. Keep that wrapper thin: it only moves branches.
- Setup: copy `cloud/` to `<repo>/tools/cloud/`, fill the "extra system deps" hook in setup.sh, probe one session
  (setup prints SETUP OK), then step 20 -> 50 -> 100 sessions while reverts and send-backs stay rare and
  `verified` keeps moving.

## Manager loop
- Only the manager chat can start cloud sessions (a routine run). An hourly in-chat heartbeat runs
  `py -3 scripts/fleet_beat.py`, which compares rows waiting for a session with sessions running and prints `FIRE n`.
  Scale with several routines on staggered minutes, never by firing runs by hand.
- The heartbeat is a health check, not a liveness check. Every alarm has a handler in `scripts/alarm_handlers.py`
  (one table; a test fails on an alarm without one) and `scripts/alarm_dispatch.py` runs them every 10 min: an
  idempotent, time-boxed auto-fix, or one headless `claude -p` per alarm from `scripts/alarm_briefs/` (RULE ZERO on
  top, at most 2 at once), 2 failed attempts = ESCALATE. The manager fixes only ESCALATE items. The TRAIN-* alarms
  and briefs belong to the retired train; give the verifier the same checks (down, stalled, held, repeat red). A
  failure mode found by hand gets a new check in fleet_beat.py.
- A dashboard reading state/health.json shows alarms only while `ts` is under 20 min old, else "beat off since
  HH:MMZ"; 3 h with nothing reaching `verified` while pushes wait is RED.
- Long-running processes start detached through WMI (Win32_Process Create, hidden window: no console may pop up on
  the owner's screen), never Start-Process or Popen from an agent shell (the child dies with the shell's job);
  restart and kill act only on their own pid file's tree.
- NEVER attach a GitHub push webhook to a builder routine: builders push, so every push starts another builder.

## Run
1. Set HARNESS_REPO, HARNESS_HOME (state dir parent), HARNESS_WT (fast drive), HARNESS_STACK (rust or node). Create
   `state/train.off`. Rename an old overhaul guard hook (`.git/hooks/reference-transaction` -> `*.disabled`): it
   blocks every main update. Free disk (keep 80 GB+; slot targets about 5-9 GB each, a gate's release target about
   30 GB) and prune acceptance tests' leftover temp dirs (they once filled 26 GB).
2. `py -3 harness.py frontier` must show rows. Build or sync the cycle's push script, verifier and collector
   (CYCLE.md), with their tests, before any writer starts.
3. Native box (the shipped OS, for the gate's non-test stages, OS-only tests and the run behind `verified`): ssh, Python,
   git, the toolchain and a bare `<HARNESS_REMOTE_ROOT>/repo.git`; set HARNESS_REMOTE_HOST, HARNESS_REMOTE_ROOT,
   HARNESS_REMOTE_ENV, or a `[desk]` table in harness.toml (host, root, env, path). Seed repo.git once with a bundle
   of main and set refs/desk/base to that sha, or the first send carries the whole history. Keep it otherwise idle.
4. Start the verifier (and the native run behind `verified`, if the cloud OS differs) detached, under the 5-minute
   watchdog; turn sharding on with its flag once one probe session has returned a valid shard result.
5. Writers: workflows/builders.js locally, or the cloud fleet; `remote.py` for a second PC.
6. Measure fixes `verified` per wall-clock hour, send-backs per hour, reverts, holds and escapes, plus per-phase
   time per landed fix (CYCLE.md "Measure"); report only measured rates, never agents busy.
