# The landing cycle: merge first, verify behind, release only what the full suite passed

> **RULE ZERO:** valuable, well made, clean code that can be built out and extended; a solid foundation beats depth;
> one owner per capability, no second copy of anything; never trade quality, tests or gates for speed or feature count.

The default way agents land work. It replaced the batch land train (queue, batches, restacks, bisects, one serial
gate everyone waited on), which measured about 1.5 packets/h at best and 0 for 13 h when it broke. No check was
dropped: each one runs at write time (affected, scoped) or behind on the trunk (full), never neither.

## Before tuning the pipeline: question the work list
Most lost hours came from what was queued, not from how it was gated: one packet per instance of a recurring defect,
packets that overlapped in flight, and shared files every packet edited. Fix those three first (the next three
sections); then measure (the last section) before changing the cycle itself.

## Fix by problem class, never by instance
- Every recurring defect type (untyped stored data, raw parameter reads, a closed name list, file I/O outside its
  owner, ids minted outside the allocator, a local copy of an owned capability, ...) gets ONE gate that lands first:
  a lint whose message carries the fix, a structural test, a shrink-only ratchet, or one shared owner the others
  must call. The gate's baseline is the whole-codebase count from a grep of every file, not the instances an audit
  happened to name.
- One counting owner for every ratchet (the shrink-only rule in SKILL.md "Gate rules": a count above its entry is a
  new instance; lists never grow; the verifier tightens them after green), with per-module baselines keyed per file.
  Per-module baselines are what let sweeps run in parallel: a sweep lowers only its own modules' entries. A counter
  at 0 everywhere becomes a hard ban.
- Then whole-codebase sweeps, one packet per class per group of modules, groups chosen so no two sweeps of a class
  share a module. Sweeps of different classes on one group serialize through the claim footprint (same files).
  Every sweep packet names the gate packet as a prerequisite, so nothing else is listed while baselines are written.
  The gate itself never waits for work already in flight: its baselines are counted on the trunk it lands on, and
  in-flight fixes can only lower them (one that raises a count is a real red). Making it wait idles the fleet.
- Each packet row carries a `class` (a class id, `gate`, or `one-off` for real one-of-a-kind design work, which must
  name its regression test or check) and a `gate` (the counter or owner that keeps the class gone).
- Enforced in the task loader, not in notes: a row with no valid class, a gate counter that is neither on main nor
  delivered by a prerequisite, or a second open packet of the same class on an overlapping module group is held with
  its reason and never published as claimable. A test runs that check over the real plan file on every harness run.
- Sample, tune, sweep: tune the gate on a sample of hits before the sweep, so the sweep is mechanical.

## No shared append points
- A file every packet appends to (a hand-sorted test module list, a test manifest, an index, a spec table edited at
  one hot spot) turns every race into a send-back: the rebase is clean but the diff's context lines changed, so the
  patch-id changed and the writer must check again. Measured: 48 send-backs against 36 landed pushes in about 29
  hours; in the pairs checked, the patch-id differed only in shared test-group roots and the spec table.
- Lists and indexes are generated from the files that exist: test registration by discovery (overhaul-build's
  derived test group roots: the build script declares every test file, so a branch only adds a file), indexes and
  tables by a generator with a check that its output is current, run in the verifier's bookkeeping push.
- Agents never hand-edit a shared list or a generated file. A branch adds its own file; the generator does the rest.
- Spec rows: the unit of change is one row with a stable id. Until a project has a drift-free shape (one new file
  per row, folded into the tables by the harness after the patch-id comparison, or the same fold as its own recorded
  push before the checks), expect send-backs on any change that edits a shared spec table, and choose one shape
  before building it: every reader of the spec must agree on it.
- A keyed merge driver resolves conflicts in such files but not patch-id drift; it is no substitute.

## Claiming: footprint and prerequisites
- A task is claimable only when its prerequisites have landed and its footprint (the modules or packages it may
  write, from its packet row, else from its file paths) overlaps nothing in flight: no claimed, pushed or not yet
  rebased task, and no task listed before it. A claimed task stays listed; a fresh claim is never displaced.
- One owner computes footprints (a typed record: lease areas and prerequisites) and publishes the claimable list
  plus the footprints; the claim script only reads it. An unknown footprint is logged, never guessed.
- A session works only listed tasks and lands each with its own land call. A sent-back task is taken again only
  while its claim is still the session's own or the list still shows it; otherwise it overlaps work in flight and
  the session ends there.
- Parallel fixers on shared modules collided until this existed; worktrees stop file clobbering, not semantic
  collisions.

## Invariant and refs
- `integ`: the trunk. Linear (merge commits refused), moved only by compare-and-swap
  (`git update-ref refs/heads/integ <new> <old>`). Writers and the verifier append; nobody rewrites it.
- `main` follows `integ` at once: every accepted push, every collector land and every verifier move of the trunk
  fast-forwards it. `main` may hold a change the full suite has not judged yet, for one verifier cycle.
- `verified`: the last sha whose FULL gate passed (format, strict lint, workspace unit tests, static quality, every
  built acceptance and property test, the LLM duplicate review). Only the verifier moves it.
- `native-verified` trails `verified` (the shipped OS's whole gate). Releases come only from these two refs.
- A red is blamed and reverted on top of `main`, never rewritten. A trunk move that fails (a lost swap, a busy lock)
  is a lag healed by the next move, never a hold; hold only when `main` is truly no ancestor of the trunk.
- Status and dashboards report `main` and `verified` apart.

## What ships and what you build
- Ships in `scripts/` and is reused unchanged: the gate stages (`harness.gate`), the one known-red rule
  (`known_red_tests`), quarantine heal, blame by entry and by file (`blame_entries`, `blame_by_file`), the duplicate
  review (`batch_review`, `self-review --base`), keyed and line-set merge drivers, ratchets, the claim kit
  (`cloud.py assign`, `cloud/`), the lock (`harness.lock`). overhaul-build ships derived test group roots.
- Specified here, built per project on those leaves: the gate scope (every test in the tree but unbuilt rows'; the
  shipped `gate_tests` still selects proven rows' tests only); a per-push `base` in blame ranges (`own_ranges` takes
  one cumulative range today); the writer floor's base (`finish.py` compares with `origin/main` today); receipts and
  the compare-and-swap push; `main` following the trunk and the `verified` ref; the class check and the footprint
  listing in the task loader (the shipped `claim_task.sh` takes the first free task); the cloud land script and the
  collector's evidence replay (the shipped `collect` still queues); the verifier loop with batch-first bisecting;
  the shard protocol and its runner; the shared build cache; the native run behind. Keep each one module with typed
  records.
- Retired: `harness.py train --loop` and its queue. Create `state/train.off` so the heartbeat never restarts it.

## Records
- Receipt (one JSON line per push, appended BEFORE the CAS): `id, base, sha, kind (lane | revert | verifier |
  unrecorded), slot, name, rows, pkgs, patch_id, selection, reverts, relands, t, rebased_from`. A trunk range with
  no receipt, or whose patch-id differs from its receipt, is `unrecorded` and is reverted before any gate.
- Reverted record (`{push id: reason, culprits, fails, revert_sha, rows, name}`): the author's next push prints it;
  the task loader hands the packet back. A reland names the reverts it lands again, so nothing salvages them twice.
- One hold file (a revert that cannot be built, a ref another writer moved): pushes stop with its reason until
  `unhold --why "<fix>"`; the next cycle then gates the trunk's whole head (the fix is its newest push).

## Writer: the affected checks, then push
One script is the only push path; cloud and local writers run the same one.
1. Clean tree; rebase onto `integ` (a conflict refuses); refuse unformatted files (formatter `--check` with the
   workspace's own edition).
2. Selection: the touched packages and their reverse-dependency closure; the push's own test files; the affected
   acceptance and property tests (capped at a fixed count of files the full gate also runs; files only the writer
   runs are never capped); the never-run list (tests of rows not built yet, tests red on this host at `verified` for
   environment reasons, budget tests: the verifier judges the last two). One function owns "what the full gate runs"
   and both sides read it.
3. The checks, cheapest first, all compared against the trunk commit it rebased onto (never a lagging remote ref):
   LLM duplicate self-review of added functions TWICE (each must print `"duplicates": []`); static quality (rules
   gate, size caps, owner copies, cycles, plan validation, shrink-only lists never growing); the own test files (one
   test-runner call per build target, and every file must match a test: a filter matching nothing fails); unit
   tests and strict lint of the closure; the affected tests. The known-red list is the verifier's own: those tests are
   skipped here, then run alone, and one that passes is reported as a heal for its owner.
4. Push: the check log must start at exactly the checked sha and end `GATES OK`; write the receipt; CAS the trunk;
   fast-forward `main`. Exit codes: 0 pushed, 1 refused, 3 held, 4 RECHECK (run the checks again), 5 RACE (the trunk
   moved: rebase, push).
5. Receipt validity after a race: a clean rebase whose patch-id is unchanged keeps the receipt valid (the full gate
   judges every candidate whole anyway; only files the writer alone runs are run again). A changed patch-id is
   RECHECK: the writer runs its checks again.
6. A writer may revert a red the verifier has not reached: the push names the reverted push ids and why, each must
   be a lane push not yet reverted and fully undone by this push, and it is recorded as a verifier revert.
7. `INTEG_FULL=1` (or a full gate on the native box) is the strict check for the rare push that must be judged first.
A writer never waits for the verifier; its next work bases on the trunk's head.

## Cloud writers
- A session claims one listed task by pushing a unique commit to a new ref (a second push is refused), works from
  the published base, and pushes WIP plus a short HANDOFF often so another session resumes.
- Its brief is generated per task: the packet text, its class and gate, its status, and every numbered owner ruling
  that names it.
- To land, the session runs a generated land script in the background: the harness bundle and quality scripts from
  the kit branch, the trunk set to the published base, then the same push script. On `PUSHED` it pushes its branch
  plus an evidence commit (`check.json` = {base, head, selection}, `finish.log`, `ROWS`), then waits for the outcome
  note (landed, or sent back: rebase onto the new base and land again, at most 3 rounds).
- The collector (every 5 min, git only, nothing built) replays the push with that evidence: the evidence must name
  the branch tip; a young branch without evidence waits (its land run may still be pushing); a lost race rebases (at
  most 3 times); RECHECK, a conflict or a refusal goes back to the task's WIP ref with the reason. It refreshes the
  base after its landings, so a sent-back session rebases onto what it overlapped. A task is done once `verified`
  holds its push and no revert undid it; a WIP ref is dropped only then.
- Pausing stops new claims; it never throws away claimed work that is still landing.

## Shared warm build cache (cloud writer sessions)
- A content-hashed compile cache (key: toolchain version, cache tool version, lockfile, repo path) seeded into each
  writer session from one snapshot before its first build; any failure (no snapshot, a key mismatch, a fetch error) is a
  cold build, never an error and never a reason to stop.
- A scheduled refresh rebuilds every test target and publishes the snapshot when it is missing, its key changed or
  it is older than a few hours; the refresh decides "needed" before any setup and logs hits and misses per build.
- Store it where the sessions' git access already reaches, in parts under the host's size limits, published
  atomically with a manifest; a reader that meets a part a newer publish replaced builds cold.
- Measured: about 1.5x on cloud writer builds. Worth having, not a silver bullet. Shard runners do not use it: they
  keep their own build warm between gates. Lint runs are not cached by such a
  tool. Locally, a path-keyed cache missed every workspace crate across per-slot target dirs (0%): keep per-slot warm
  targets there; the cloud cache works because every session builds at the same path.

## Verifier: one round
1. Fetch. Pushes = the trunk's first-parent chain after `verified`, each matched to its receipt. Unrecorded: revert
   first.
2. Batch-first bisecting: a round gates the closed prefixes (no reverted push without its revert) at n, n/2, n/4 ...
   pushes after `verified` (longest first, n capped at 32), each with the full sharded gate; `verified` takes the
   longest green. A red narrows the next round to the range below it, so every good push before a bad one is
   verified in round one and the bad one is isolated in about log2(n) rounds. A round runs to its end. Verdicts are
   reused per (tree id, test list). Throughput over a full day: not yet measured.
3. Duplicate review of verified..candidate (duplicates = red; reviewer unavailable = no verdict and an owner alarm,
   never a pass), then the full gate.
4. Judging a red, one routine for every route: known-red dropped; every failing test re-run alone at the candidate
   (green alone = flaky: quarantined with a fix request, nothing reverted, no hold); red alone at `verified` too =
   pre-existing, healed by quarantine; a test red when every suspect push is documentation no code reads is no
   push's. Only what stands is blamed.
5. Green: move `verified` by CAS (fast-forward only). Bookkeeping the verifier writes (proven rows, ratchet
   tightening, regenerated indexes, reformatting) goes onto the trunk as its own recorded push. A quarantined test
   whose fix landed is released on the next green.
6. Red: blame, revert, record (below). A red its named culprits do not explain (a masked second bad push) is
   bisected before the revert, never left behind it as a hold.

## Sharded full gate (census or no verdict)
- Publish the candidate as `gate/<id>` and one spec per shard (`{id, sha, k, tests, targets}`: each test file mapped to
  its build target in a tree AT the candidate) on a kit branch only the verifier writes. Shards are balanced
  longest-first by recorded seconds (24 for ~4,000 tests on 4-core sessions).
- A runner claims shard k with a unique commit on `gate-claim/<id>-<k>`, builds and runs exactly as the gate does
  (every failure re-run alone), and answers with `gate-result/<id>-<k>`: `{fails, tests, head, times, build_secs}`.
  `gate-done/<id>` closes it. Runners serve detached until idle 45 min, and warm the newest gate's build when idle.
- In parallel the native box (or several registered native workers) runs every non-test stage plus the OS-only
  tests, re-runs every cloud failure (the cloud can pass a test, never forgive one), and takes unclaimed or late
  shards. A native shard that did not build falls back to another worker once per worker; red only after every
  worker failed it.
- Census: every listed test gets exactly one verdict and the native side computed the same list, else no verdict. A
  shard that did not build, ran 0 tests, ran another head or another list, or a build refusing a target, is no
  verdict: never a pass, never a fail. No verdict falls back to the one-box gate.

## Red: blame, revert, record
- Order (the train's leaves): blame by assert entry, blame by file, then a probe at each push's own sha. Reverted
  pushes and reverts are never suspects, and a documentation push is never a test's culprit (stages still can be).
- A probe reproduces a red only by the same stage or one of the same tests red AT the probed sha, gated with that
  sha's own rows and test files.
- Revert set = culprits + true dependents (later pushes sharing their files, transitively; any push the revert
  conflicts with; any push in a package a build check of head+revert names, 3 rounds), in ONE commit on the trunk's
  head by CAS, which `main` then follows. Files with keyed or line-set merge drivers (generated roots, spec tables)
  make no dependency; read their attributes from the tree. A push already reverted is never a dependent. No
  compiling revert, or a red before the last revert: hold.
- Escapes: a failure the culprit's own selection did not run is logged and widens the selection index.

## Native run behind
When the cloud runs another OS than the one you ship: whenever `verified` moves, the native box runs the WHOLE gate
on its newest head (heads in between skipped). It never blocks the cycle: it yields the box to any verifier gate.
Green moves `native-verified`. A red routes only proven tests: never a timing or budget test, never a whole stage,
and only a test red again alone at the candidate (red alone at the base too is pre-existing and healed). Anything
dropped is no verdict: nothing reverted, the ref kept. Each standing test joins the native-red list so later sharded
gates run it natively.

## Operations
- A lock whose holder process is positively gone is dead at once (a failed probe is no evidence; probed at most every
  5 s), an age limit catches a hung holder, and the takeover moves the file aside and puts it back if it changed
  since read (`harness.lock`). Storing the holder's start time beside its pid also catches a reused pid.
- A watchdog every 5 min restarts the verifier, the native verifier and the collector when gone, detached (never as
  an agent shell's child), by pid file, with no visible window (on Windows a hidden-window process start; a console
  that pops up on the owner's screen is a defect). It never starts the retired train. It also guards disk: prune
  stale targets below a free-space floor, an owner alarm below a lower one.
- Feature flags are state files read each cycle (sharding, bisecting rounds, native run): delete one to turn it off
  without a restart.
- A script cannot create cloud sessions; it can wake a pool the owner created once.

## Measure before claiming speed
- The one bar: fixes `verified` per wall-clock hour. Beside it: send-backs (RECHECK, conflict, refusal) per hour,
  reverts, holds and the escape rate. Never report agents busy or pushes made.
- Log per-phase times for every landed fix as events: claim, code done, land sent, pushed, on `main`, `verified`.
  Find the slowest phase from those before tuning anything; tune one thing at a time and keep it only if the
  measured rate rose.
- Report only measured rates. An estimate is labelled as one; a claimed speedup without a before and after is not
  reported.

## Measured (one project, 2026-11-24/25; a Rust workspace of ~4,000 integration tests)
- Old land train: ~1.5 packets/h at best; the gate itself 60-75 min, the rest picks, rejects and bisects.
- Writer floor: 1-13 min per push before the closure and affected tests moved into it. Per-change full check: 8 min
  to 7 h by blast radius. A batch of 4 packets fully checked on the native box: 50 min, then lost its race with the
  trunk.
- Full gate on one quiet native box: 55-81 min with all ~4,000 tests; sharded over 24 shards: 34-35 min. A cloud
  shard runs in 0.2-2.5 min after a 2-13 min cold build.
- Cloud fixers: 0.3-0.5 fixes per agent-hour. Over about 29 hours: 36 pushes landed against 48 RECHECK send-backs
  (about 1.3 per landed push, most from shared append points) and 6 verifier reverts.
- Shared warm build cache in cloud writer sessions: about 1.5x.
- Not yet measured: merge-first fixes per hour over a full day, batch-first bisecting, the native run's cycle time.

## Lessons (these replace older guidance; never append a contradiction)
- Fix by class: an audit's 75th instance of one defect as its own packet is the work list's fault, not the
  pipeline's. Gate first, then sweep every instance.
- Shared append points were the measured cause of most send-backs; generate lists, never hand-edit them.
- Claim by footprint: parallel fixers on shared modules collided and redid each other's work.
- The full gate runs every built test. A gate scoped to "tests a live row lists" skipped half the files.
- One known-red list, read by writer and verifier alike; a writer-side copy diverged and blocked good pushes.
- Static quality stays in the writer's checks: deferring it let cap and wiring reds reach candidates, and blame
  reverted a bystander first.
- Every red is re-run alone at the candidate before any revert: a timing-flaky test once reverted a
  documentation-only push.
- A native-behind red on budget tests (the box ran out of disk) reverted five innocent pushes through a log
  heuristic and a shared generated root: route only proven tests, and generated files are no dependency.
- Probe with the probed sha's own rows and tests: the candidate's rows at an older sha "reproduced" a red falsely.
- A build that refuses a test target, or a shard that ran 0 tests, is no verdict, not a mass failure.
- A push already reverted is never taken again as a dependent (the second revert conflicted into a hold).
- Writers compare against the trunk commit they rebased onto; a lagging remote ref blamed them for others' work.
- Strip CR from test lists printed on Windows, and refuse a filter that matches no test: both passed in silence.
- Every long-running process has a watchdog, including the scheduled collector; restarts open no window.
- A send-back must reach a live session: the land script waits for the outcome and lands again.
- Packet briefs carry the numbered owner ruling that names the packet; a session cannot read the owner's notes.
- A plan check cached by tree id alone kept a red after the check's own script changed: key caches by the checker
  too.
- Merge first, verify behind beat per-change full checks and batch checks. A hosted CI matrix per change was not run
  (estimated ~250-350 runner minutes per change at this suite size: included monthly minutes cover about 10).
