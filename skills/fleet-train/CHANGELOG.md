# fleet-train changelog
- 2026-11-17: free_disk: a slot is protected only by a live attempt (pid alive, deadline ahead; `alarm_dispatch.live_attempt`, `Ctx.alive`) or a lane brief whose DISPATCH.md row is not done/paused/retired; dead records no longer block cleanup when dispatch is off. Tests: scripts/tests/test_alarm_dispatch.py (test_stale_slot_records).
- 2026-11-17: pick compile: the pick check runs the profile's `pick_build` (rust: `cargo check -q --profile test --lib --bins --workspace` on `{targets}/pick`; node: off) on main + the row, the verdict kept per main sha (`built`), so a row whose base went stale when a landed row changed a type it uses is rejected with the compiler's errors before its batch (pilot: two unit-stage reds, about 10 min of regate each; warm cost 6 s a clean row, 11 s a red one with main's own compile for comparison). No verdict while the train marks its acceptance stage or past HARNESS_PICK_BUILD_TIMEOUT (600 s); a main failing the same compile rejects no row. The train also re-execs when any module it imports changes (`TRAIN_CODE`), not only harness.py. Tests: scripts/tests/test_fail_fast.py (test_pick_build).
- 2026-11-17: desk acceptance split (`desk.py`, off until state/desk-on; HARNESS_DESK=0/1 overrides): with a second Windows machine on ssh, this machine runs the parallel unit while the desk runs the run-alone units one at a time, longest first, and this machine takes any the desk has not started. A desk unit with no verdict (error, late, other head or tests, stack `env_fail_re`) re-runs here and drops the desk for the gate; desk down or under 60 tests: all here. `run_tests(solo=)`, state/alone-times.json, `acceptance_tests()` at both gate sites; the kit (every script beside harness.py, harness.toml, run.cmd / start.ps1 / stop.cmd) is resent only when it changed; remote.py takes HARNESS_REMOTE_HOST / ROOT / ENV from desk.py. Pilot: first live gate 1094 s against 2490 s local, 0 of 1989 failed, the desk ran 6 of 7 solo units; later trains 7 of 7. This public copy's ssh transport has not run live. Tests: scripts/tests/test_desk.py.
- 2026-11-17: batch fill tuned to the gate cycle: its own threshold HARNESS_FILL_MIN (5; HARNESS_MIN_BATCH still drives the train-start wait) and HARNESS_FILL_WAIT 10 min (was MIN_BATCH and 20 min). With a ~25-min sharded gate cycle a longer hold costs more train time than the rebased rows it gains; a ~45-min cycle wants about 8 and 20. Tests: scripts/tests/test_batch_fill.py.
- 2026-11-17: plan check: the gate runs plan_validate on every batch (`rules_gate.problems(plan=)`: off | spec | always; a code-only batch can spend or delete another planned row's todo!() stub and the spec-only trigger let it land), with the landing rows' own spent stubs exempt (`landing`). The pick check adds `row_plan_problems`: plan_validate's row-local check_stubs on the planned rows whose files cell or FUNCTIONS line the row changes, new over main (about 1 s a row against minutes for a full run; on the pilot it caught each of the stub-class gate reds and matched a full run exactly). `PICK_CHECKS` versions cached pick passes so a pass by older checks runs again. Tests: scripts/tests/test_rules_gate.py (RowPlan, PlanModes), scripts/tests/test_fail_fast.py.
- 2026-11-16: batch fill: under HARNESS_MIN_BATCH clean picks while queued rows are out for cloud rebase (cloud.json `rebase_sent` = the queued sha), the train idles (`train_idle` "filling: ...") up to HARNESS_FILL_WAIT (20 min, 0 = off), then gates what is clean; a CLEAN-* pick never waits (two 1-row gates had each held the train ~45 min while 23 rows were with the rebasers). `fill_wait()`; harness owns `CLOUD_STATE`. Tests: scripts/tests/test_batch_fill.py.
- 2026-11-16: second gate lane rule: it gets only the picks beyond BATCH_CAP, never half of a smaller batch (lane two lands only when lane one lands whole: 0 of 7; one full lane went from 5-6 to 10-14 rows a train, 14/14 and 10/10 landed).
- 2026-11-16: owner card: health.json alarms older than 20 min read "beat off since HH:MMZ"; a pause shows state/pause's first line.
- 2026-11-16: runbook: never stop `collect` while rows are queued (rebasers rebased onto a stale cloud-base); fire rebasers when a train lands; the bar is functions on main per wall-clock hour; prune acceptance temp dirs (26 GB); with dispatch off, DISK is fixed by hand.
- 2026-11-16: quarantine_cap pause no longer deadlocks the fix: while state/main-broken.json holds, the train picks only CLEAN-* rows (clean-only batch; `quarantine.train_queue`) and feature rows wait; the pause also clears by itself when the quarantined and red tests pass alone on the unchanged main (rechecked uncached every 2 h). Tests: scripts/tests/test_quarantine.py.
- 2026-11-16: REQUEST alarm: an open `notes/requests/*.md` (older than 10 min, not `split-*`, no live lane brief naming it) gets one capped slot agent through the alarm dispatcher (template `request.md`: RULE ZERO, read and do the request, clean-start / clean-submit, delete the slot target dir); the agent moves the request to `notes/archive/requests/` when its fix is submitted, so it is never dispatched twice. Tests: scripts/tests/test_alarm_dispatch.py.
- 2026-11-16: acceptance scheduling (cargo runner): test binaries start longest first by their recorded seconds (state/accept-times.json; an unknown, new binary first), tests with a perf-budget miss in the last 2 days skip the parallel phase and get the same alone run once (they failed under load every batch and were re-run alone anyway: 500-1700 s of serial tail per gate on the pilot), and event `accept_split` logs build / parallel / serial seconds with the slowest binaries and alone runs. Same asserts, same verdicts. Tests: scripts/tests/test_accept_order.py.
- 2026-11-16: quality stage cut: plan_validate output cached per tree content (the batch's own tree, retries, the pre-push check and the next batch's base once that tree lands run it once, not twice per batch); entry_match compiles each CLI grammar regex once (plan_validate 214 s to 90 s on a 1,000-row plan, same output).
- 2026-11-16: train idle and reject dequeues: an empty disjoint batch no longer loops `train_start rows=[]` (every queued row conflicts with main: each is rejected once as a conflict, event `train_idle`, the loop sleeps); `reject()` dequeues the row under the state lock on every path (pick checks, rulings, batch rejects). Tests: scripts/tests/test_train_idle.py.

- 2026-11-15: stack-profile fixes from a node migration: a project `unit` that is a `node --test` glob now reruns flaky tests inside that glob (any other `unit` reruns nothing, never default discovery); `HARNESS_SPEC_REF` lets `frontier` read a branch's spec; `fleet_beat` runs without cloud numbers (`beat_alarms.cloud_status`); builders.js takes the stack's done check, env command and output habit from the claim JSON `stack` (profile keys `stack_label`, `env_use`, `done_brief`, `tail_brief`); `{changed}` in a profile `qa` command expands to the batch's changed files; profile `copy_ignored` globs copy gitignored assets into slot and train trees; every child process and the train re-exec (via `run_train.py` when present) use CREATE_NO_WINDOW. Tests: scripts/tests/test_profile_fixes.py.
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
- 2026-11-15: clean code and brevity, ported from the pilot project: `rules_gate.py` (swallow / expect / panic / allow / owner / text counts per file, shrink-only state/rules-debt.json tightened at each land, plan_validate on spec edits; Rust and Node scanners in `rules_langs.py`, Rust counts identical to the pilot's on its 156-file tree) in quality_problems; `brevity_check.py` + fleet_beat BREVITY alarm; `harness.py note`; quality-check compares against local main; the proven commit deletes the row's bullet and packet; state/held-rows.txt; cloud sessions get every overhaul-build script plus the rules gate and debt (HARNESS_CLOUD_QA and profile `cloud_qa` removed); BUILDER.md finds owners with `index_api.py --find` and re-runs the rules check before push; overhaul-build: `index_api.py --find`, packets in spec/packets/*.md, proven rows not packet-checked. Tests: scripts/tests (18).

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
- 2026-11-15: `quarantine.py`: a failing test no batch row owns is run alone on main before any blame or probe (both pinned a main-red test on an innocent row); red on main it is quarantined with evidence and a fix request and the batch is re-gated; cap 3, then the train pauses until main moves; released once green alone on main. `beat_alarms.py`: KNOWN-RED and REPEAT-TEST say whether a fix request exists and a DISPATCH lane works it; ORIGIN-BEHIND when origin main lags the train (its push is check=False). Gate events list the failed tests (`failed`).
- 2026-11-15: `alarm_dispatch.py` + `alarm_handlers.py` + `alarm_briefs/`: every fleet_beat alarm has a handler (auto
  fix, owner, headless agent or ESCALATE), run every 10 min; agent locks are pid + start time + deadline, the check
  judges each attempt, 2 failed = ESCALATE; fleet_beat shows handled-by/cooldown/ESCALATE and pings only ESCALATE.
- 2026-11-16: pick check: before a row enters a batch the train runs the gate's no-build checks (owner copies, size, rules count/text checks, QUALITY_CMDS; not the plan check or lint) and the duplicate reviewer on main + that row, in worktree <wt>/pick; a failing row is rejected and dequeued (one pass per sha, state/pick-verdicts.json; a main red on them rejects no row; no verdict = on to the gate, which keeps every check). Idle waits check queued rows ahead (PICK_IDLE_SECS). `cargo_slots.py`: one machine-wide cap on cargo-building dispatch agents shared by every fleet (FLEET_CARGO_SLOTS, default ~/.claude/fleet-cargo-slots.json, {pid, start, fleet}; FLEET_CARGO_CAP, default 3); none starts while the train marks its acceptance stage (state/train-stage.json).
- 2026-11-16: pick holds a stacked row (its branch carries another unproven row's commits, read from commit subjects)
  until that row is picked earlier in the batch or proven; landing it alone failed the plan check and cost a full gate.
  `STACK-ORPHAN` alarms when the carried row is neither queued nor claimed.
- 2026-11-17: pick acceptance: `harness.py pick-accept` runs each queued row's owner tests (the tests of proven rows
  whose file list holds a changed file, its own and changed test files; a full impact selection was a whole run for
  any core edit) on the desk, merged onto the batch head while a batch gates, else main; never in acceptance, and the
  gate's Desk.setup kills a running pass. pick_screen rejects a red verdict (read before the cached-pass skip) while
  main's code equals its base. Measured: the tier held every row-attributable acceptance breaker, ~5 tests a row.
