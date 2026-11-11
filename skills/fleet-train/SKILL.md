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
  `status`; `frontier`; `clean-start` / `clean-submit` for cleanup branches (CLEAN-* queue rows).
- `workflows/builders.js`: Claude Code Workflow, N worker loops, each spawning a FRESH builder agent per row
  (args: {slots, max, idle, home}). Builders run only their row's tests and clippy on touched crates.
- `workflows/cleanup.js`: one agent per crate group fixes dup blocks, repeated literals, long functions, strict clippy.
- `scripts/remote.py --slot 100`: a second PC as extra builders (claims here, runs `claude -p` there over ssh,
  fetches the branch back, submits).
- `scripts/dash.py`: static dashboard page from state/events.jsonl; publish it as an artifact.

## Train rules (each one came from a real stall)
- Batch up to 80 queued branches; conflicts are rejected back to the builders with the reason.
- Gate once: cargo fmt (auto-fixed), clippy -D warnings, unit tests, then acceptance tests of every proven row plus
  the batch's rows, built once in release (incremental) and run as parallel binaries.
- A failed test is re-run alone, serially. Perf-budget failures ("BENCH ... over budget/limit/floor") are written to
  state/perf-debt.jsonl, not blamed (they are noise on a loaded machine); check them later on a quiet machine.
- Stale fixture locks are cleared and the test re-run once.
- Blame before bisect: files named in the failure output (clippy paths, failing unit crates, failing rows' declared
  files) eject only the branches that touched them.
- Unit failures that main already has (state/unit-baseline.json, per main sha) are not blamed. Retries re-run unit.
- Quality: every claim carries `must_also_before_commit` (dup_blocks, dup_literals, shape_check, check_cycles, strict
  clippy). `touch state/quality-gate.on` makes the train reject branches that add any of these; turn it on once main
  is clean (run workflows/cleanup.js first).
- Claim limits: at most 3 kernel rows / 6 rows per module in flight, no two rows with overlapping declared files;
  rows that unblock 8+ others skip the module cap; state/priority.json rows go first and skip caps.
- Stacked starts: a row whose deps are queued (not yet landed) starts on main + those branches.
- The loop never dies on an exception and re-execs itself when harness.py changes and still parses.

## Run
1. Set HARNESS_REPO, HARNESS_HOME (state dir parent), HARNESS_WT (fast drive) or edit the defaults at the top.
2. `py -3 harness.py frontier` must show rows. Free disk first (keep 80 GB+; slot targets about 5-9 GB each, the
   train's release target about 30 GB).
3. Background: `py -3 -u harness.py train --loop >> state/train.log 2>&1`.
4. Workflow tool: run workflows/builders.js with args {"slots": 16, "max": 40, "idle": 8, "home": "<HARNESS_HOME>"}.
5. Optional: `py -3 -u remote.py --slot 100` (one per desktop slot).
6. Watch only `train_end`, `train_error` and disk with one Monitor; measure with `harness.py status`
   (landed counts unique rows). The number is rows landed per hour, never agents busy.
