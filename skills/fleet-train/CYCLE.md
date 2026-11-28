# The landing cycle: merge first, verify behind, release only what the full suite passed

> **RULE ZERO:** valuable, well made, clean code that can be built out and extended; a solid foundation beats depth;
> one owner per capability, no second copy of anything; never trade quality, tests or gates for speed or feature count.

The default way agents land work. It replaced the batch land train (queue, batches, restacks, bisects, one serial
gate everyone waited on), which measured about 1.5 packets/h at best and 0 for 13 h when it broke. No check was
dropped: each one runs at write time (affected, scoped) or behind on the trunk (full), never neither.

## Before tuning the pipeline: question the work list
Most lost hours came from what was queued, not from how it was gated: one packet per instance of a recurring defect,
packets that overlapped in flight, and shared files every packet edited. Fix those three first (the next four
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
  Every sweep packet names the gate packet as a prerequisite, so no sweep is listed while baselines are written
  (one-offs that touch none of its counters do not wait for it).
  The gate itself never waits for work already in flight: its baselines are counted on the trunk it lands on, and
  in-flight fixes can only lower them (one that raises a count is a real red). Making it wait idles the fleet.
- Each packet row carries a `class` (a class id, `gate`, or `one-off` for real one-of-a-kind design work, which must
  name its regression test or check) and a `gate` (the counter or owner that keeps the class gone).
- Enforced in the task loader, not in notes (`scripts/task_loader.py`, below): a row with no valid class, a gate
  counter that is neither on main nor delivered by a prerequisite, or a second open packet of the same class on an
  overlapping module group is held with its reason and never published as claimable. A test runs that check over the
  real plan file on every harness run.
- Slow tests are a class too (slow tests / perf budget): its gate is a per-test time budget; a property test that
  rebuilds a large fixture per case is fixed in the test (open it once, keep every assertion), never by raising a cap.
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

## The owners index and the duplicate check
- Agents create duplicates when they cannot find the owner. The owners index (overhaul-build `index_api.py`: every
  public item per module with its signature and first doc line) is regenerated on EVERY push as a fix-up commit of
  the push script, and `index_api.py --check` runs in the static quality stage. An index only one retired step
  regenerated went stale in 43 files and agents stopped trusting it. Until main's index is fresh, run the check on
  the writer path only, or the verifier blames in-flight pushes for main's drift.
- Before adding ANY public item (function, type, enum, trait, const, static), a writer runs
  `index_api.py --find "<purpose words>"` and a grep for the same rule or body, and names in its commit each owner it
  reused. Briefs say "every new or renamed public item", not "every function".
- The LLM duplicate review sees only the functions a diff adds, so it misses a new type, enum or const named like
  another module's, and a changed function whose signature stayed while its body grew into a copy of a sibling.
  `rules_gate.py` checks both mechanically (`dup_problems`, in the writer floor, pre-push and the quality stage): a
  public item named like another module's item fails unless the shrink-only debt (`dups` in state/rules-debt.json,
  seeded by `rules_gate.py init`) already counts that many modules; a function the diff adds or changes whose body
  now covers most of a sibling's fails unless that pair was already that close. Names every module declares by
  design (a per-module contract) go in `overhaul.toml` `[rules] name_conventions` (read from the base too, so a diff
  cannot exempt its own name); covered: Rust and JS/TS public items; existing doubles are sweep work, never room for more. On the
  commits that added the duplicates of one audit it flagged 5 of 5 and none of 6 clean fixes.

## Claiming: footprint and prerequisites
- A task is claimable only when its prerequisites have landed and its footprint (the modules or packages it may
  write, from its packet row) overlaps nothing in flight and no task listed before it. In flight = claimed, or pushed
  but not yet on the published base sessions start from. A task whose push the base holds is NOT in flight: it
  leases nothing and its claim is released at once (a later revert lists it again, its WIP kept). Holding claims
  until `verified` moved over them once blocked the gate packet and everything behind it behind 26 landed packets.
- Lease order: the class's gate packet first (it unblocks every sweep), then the tasks the last publish listed (a
  fresh claim is never displaced), then plan order. A claimed task stays listed.
- One owner computes footprints (a typed record: areas, prerequisites, class, gate, absorbed packets) and publishes
  the claimable list; the claim script only reads it. An unknown footprint (no areas) is logged, never guessed.
- A session works only listed tasks, each claimed by the claim script and landed with its own land call; it never
  folds an unlisted task into its land. A sent-back task is taken again only while its claim is still the
  session's own or the list still shows it; otherwise it overlaps work in flight and the session ends there.
- Parallel fixers on shared modules collided until this existed; worktrees stop file clobbering, not semantic
  collisions.

## Task loader (shipped: `scripts/task_loader.py`)
The fix-by-class check and the footprint listing above, as pure functions over a plan file, with tests
(tests/test_task_loader.py). The plan is Markdown data the loader reads, three tables found by their headers:
```
| class | problem | gates |              one row per class; gates = the counters or owners that keep it gone
| area | group |                        module or package -> module group (two sweeps of a class never share one)
| id | areas | deps | class | gate | replaces |    one row per packet; class = a class id, `gate` or `one-off`
```
- `py -3 task_loader.py --plan <plan.md> --check --repo <repo> --gate-dir <where main declares gates>` exits 1 on
  any refusal: run it on every harness test run and before publishing a plan.
- `py -3 task_loader.py --plan <plan.md> --state <loader.json>` prints `{listed, held, release, unknown}` from
  `{"claimed": [...], "unbased": [...], "done": [...], "previous": [...]}` (done = pushes the published base holds).
  Your collector writes that state from its refs, publishes `listed` as `<kind>-tasks.txt` (the shipped
  `claim_task.sh` takes the first unclaimed id in it) and deletes the claim refs in `release`.
- One-offs never wait for the gate packet unless they touch its counter; they depend only on what their change needs.

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
  review (`batch_review`, `self-review --base`) and the mechanical duplicate check (`rules_gate.py`), keyed and
  line-set merge drivers, ratchets, the claim kit (`cloud.py assign`, `cloud/`), the lock (`harness.lock`).
  overhaul-build ships derived test group roots and the owners index (`index_api.py`).
- Ships as a tested reference to build on (the minimum cycle below runs on these alone):
  `trunk.py` (receipts, the CAS push with its exit codes, `main` following the trunk, unrecorded commits reverted
  first, a verifier round over closed prefixes n, n/2, ... that moves `verified` to the longest green and reverts an
  isolated red push, holds); `gate_sha.py` (the duplicate review and the full gate on one sha against
  `verified`: exit 0 green, 1 red, 2 no verdict); `push.sh` and `verify_loop.sh` (steps 2 and 3); `task_loader.py` (the class check and footprint listing); `keepalive.py` (the watchdog).
- Specified here, built per project when the minimum cycle is too slow or too narrow, one module each with typed
  records: the gate scope (every test in the tree but unbuilt rows'; the shipped `gate_tests` still selects proven
  rows' tests only: widen it first); blame and true dependents before a revert (`trunk.py` reverts only an isolated
  single push; `own_ranges` takes one cumulative range); the writer floor's base (`finish.py` compares with
  `origin/main`: point it at the trunk commit it rebased onto); the cloud land script and the collector's evidence
  replay (the shipped `collect` still queues into the retired train: replace that step); sharding, the native run
  behind and the shared build cache.
- Retired: `harness.py train --loop` and its queue. Create `state/train.off` so the heartbeat never restarts it.

## The minimum cycle, step by step
Run every command from `<skill>/scripts` with HARNESS_REPO (the landing clone, which holds the refs) and
HARNESS_HOME set. On Windows use `py -3`; elsewhere `python3`.
1. Refs, once: `git -C $HARNESS_REPO branch integ main && git -C $HARNESS_REPO branch verified main`. Protect them
   on the host so only the landing clone pushes them; writers push only their own branches.
2. Writer push, the only push path: `bash $S/push.sh <slot> <name> <rows...>` in the writer's committed worktree
   (lanes and the cloud land script run the same file). It rebases onto `integ` (a conflict refuses), runs the
   writer checks (`FINISH_NO_SUBMIT=1 finish.py`, last line `GATES OK`), writes the check log as
   `CHECKED <sha> <patch-id>` + the checks, calls `trunk.py push` and `trunk.py mirror` (harness.toml `[trunk]
   mirrors`: which remote gets which refs, e.g. a CI host `main` only; default origin, all three).
   Exit 0 PUSHED, 1 REFUSED, 3 HELD, 4 RECHECK (run the checks again), 5 RACE (run it again; the receipt stays valid
   while the patch-id is unchanged).
3. Verifier loop: `bash $S/verify_loop.sh`, started detached by the watchdog. Each round runs `trunk.py verify`
   with `gate_sha.py` as the gate (the duplicate review of `verified..sha`, then `harness.gate`, with every baseline
   taken at `verified`: under merge first `main` may already hold the candidate, so a baseline at `main` would hide
   its reds) and runs `trunk.py mirror`. `trunk.py status` prints `main`, `verified`,
   unverified pushes, unrecorded commits and any hold; `trunk.py unhold --why "<fix>"` clears a hold.
   Proven end to end on a toy repo: a clean push lands and is verified; a red the writer checks refuse never
   lands; a red pushed past them is isolated, reverted on top of `main`, and the next round verifies the pair.
4. Task loader: the collector runs `task_loader.py --state` each cycle and publishes `listed` (above).
5. Watchdog: `<HARNESS_HOME>/keepalive.json` lists the verifier loop and the collector (`{"processes": [{"name":
   "verifier", "cmd": ["bash", "<S>/verify_loop.sh"], "cwd": "."}], "disk": {"path": ".", "prune_below_gb":
   100, "alarm_below_gb": 30, "prune_cmd": []}}`) and is scheduled every 5 minutes with no window (Operations).
6. Release only from `verified`. Measure (below) before adding sharding, bisect depth or a cache.

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
  base after its landings, so a sent-back session rebases onto what it overlapped. Its claim is released as soon as
  the base holds its push (Claiming); the task is done, and its WIP ref dropped, once `verified` holds it and no
  revert undid it.
- Pausing stops new claims; it never throws away claimed work that is still landing.
- Build these two on `trunk.py` (the shipped `cloud.py collect` still queues a stamped branch into the retired
  train: replace that call, keep its claim, WIP and release handling):
  ```
  <kind>-land.sh <task> <rows...>     (fix-land.sh, plan-land.sh; row-land.sh for builder rows; on the kit branch)
    rebase onto origin/<base>; run the writer checks (step 2 of the minimum cycle) on the VM -> check.log, GATES OK
    commit nothing after; push <kind>/cloud-<task> and cloud/check-<kind>-<task> holding
      check.json {base, head, selection}, finish.log, ROWS
    print LAND SENT <task> <sha>; poll the outcome note on the kit branch every 60 s for up to 25 min:
      LANDED <push id>        -> exit 0
      SENT BACK <reason>      -> re-claim only if the claim is still ours or the task is listed; rebase onto the new
                                 base; land again (3 rounds at most)
      nothing yet             -> LAND PENDING, exit 0 (the collector still lands it)
  collect (every 5 min, git only, nothing built)
    for each <kind>/cloud-<task> with evidence:
      evidence head != branch tip           -> send back "evidence names another sha"
      no evidence and branch younger than 20 min -> wait (its land run may still be pushing)
      rebase the tip onto integ with git only; a conflict -> send back "conflict with <base>"
      log = "CHECKED <evidence head> <patch-id of evidence base..head>" + finish.log
      trunk.py push --sha <rebased> --base <integ> --log log --name <task> --rows ROWS
        PUSHED -> outcome LANDED; RACE -> rebase again (3 times); RECHECK, REFUSED, HELD -> send back with the reason
    send back = the branch to cloud/wip-<kind>-<task>, the reason to <kind>-rejects/<task>.md, outcome SENT BACK
    then: cloud-base = integ; task_loader.py --state -> publish <kind>-tasks.txt; delete the claims in `release`
  ```

## Shared warm build cache (cloud writer sessions)
- A content-hashed compile cache (key: toolchain version, cache tool version, lockfile, repo path) seeded into each
  writer session from one snapshot before its first build; any failure (no snapshot, a key mismatch, a fetch error) is a
  cold build, never an error and never a reason to stop.
- A scheduled refresh rebuilds every test target and publishes the snapshot when it is missing, its key changed or
  it is older than a few hours; the refresh decides "needed" before any setup and logs hits and misses per build.
- Store it where the sessions' git access already reaches, in parts under the host's size limits, published
  atomically with a manifest; a reader that meets a part a newer publish replaced builds cold.
- Measured: about 1.5x on cloud writer builds. Worth having, not a silver bullet. Shard runners do not use it: they
  keep their own build warm between gates. Lint runs are not cached by such a tool. Locally, a path-keyed cache
  missed every workspace crate across per-slot target dirs (0%): keep per-slot warm targets there; the cloud cache
  works because every session builds at the same absolute repo path.
- Build it as one script with four commands (Rust example: sccache, a pinned release checked by sha256; any
  content-hashed compiler cache fits the same shape):
  ```
  key      = sha256(compiler -vV, cache tool version, lockfile, absolute repo path)
  seed     : install the pinned tool (verify its checksum) -> read the store's MANIFEST {key, gen, parts: [commit]}
             -> stream every part, check each commit is the one MANIFEST names (else: cold) -> unpack into the local
             cache dir -> start the cache server -> only if it answers, export the compiler wrapper in the env file
             every build sources. Prints WARM or COLD; exit 0 either way.
  needed   : exit 0 (and why) when there is no snapshot, its key differs or it is older than the refresh period;
             else exit 3. Runs before any setup, so a scheduled refresh that is not due costs nothing.
  refresh  : seed, then build every test target with incremental compilation off (incremental builds are not
             cacheable), log hits and misses, compress the cache dir, split it under the host's object size limit,
             push the parts as orphan commits on fixed branches, push MANIFEST last (it names this generation).
  ```
  Keep the store out of the product repo (its own private repo the sessions can fetch); never delete refs (some git
  proxies refuse it: overwrite fixed branches instead). Writers seed it in step 1 of their brief; COLD never stops a
  session.

## Verifier: one round
1. Fetch. Pushes = the trunk's first-parent chain after `verified`, each matched to its receipt. Unrecorded: revert
   first.
2. Batch-first bisecting: a round gates the closed prefixes (no reverted push without its revert) at n, n/2, n/4 ...
   pushes after `verified` (longest first, n capped at 32), each with the full sharded gate; `verified` takes the
   longest green. A red narrows the next round to the range below it, so every good push before a bad one is
   verified in round one and the bad one is isolated in about log2(n) rounds. A round runs to its end. Verdicts are
   reused per (tree id, test list). Throughput over a full day: not yet measured.
3. Duplicate review of verified..candidate (duplicates = red; reviewer unavailable = no verdict and an owner alarm,
   never a pass), then the full gate. Any crash of the gate command is no verdict (`gate_sha` exits 2): a crash exits 1,
   which reads as red and would revert an innocent push.
4. Judging a red, one routine for every route: known-red dropped; every failing test re-run alone at the candidate
   (green alone = flaky: quarantined with a fix request, nothing reverted, no hold); red alone at `verified` too =
   pre-existing, healed by quarantine; a test red when every suspect push is documentation no code reads is no
   push's. A re-run alone killed by its time cap is never red and never a pass: when it caps at `verified` too it
   is pre-existing slow (quarantined with a fix request in the slow-tests class, the rest of the verdict stands);
   when it caps only at the candidate the round is no verdict (nothing moves, nothing is reverted). Shipped:
   `TIME_CAP`, `capped.json`, `gate_sha.verdict`, `quarantine.slow`. One 21-minute property test past a 15-minute
   cap under load made two candidates red and stalled `verified` for hours. Only what stands is blamed.
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
- A revert set bigger than the culprits plus 3 dependents is no revert: hold and raise a fix-forward alarm (the
  manager fixes the culprit's red on the head within one beat). VOIDCAD 11-27: one spec line red in CU-578 reverted
  81 good pushes (a day's landings) because 80 were its "dependents"; before that, the hold sat 7 h unread.
- A hold is an alarm, never a state to wait in: the manager's 15-minute beat reads it and fixes forward.
- Escapes: a failure the culprit's own selection did not run is logged and widens the selection index.

## Native run behind
When the cloud runs another OS than the one you ship: whenever `verified` moves, the native box runs the WHOLE gate
on its newest head (heads in between skipped). It never blocks the cycle: it yields the box to any verifier gate.
Green moves `native-verified`. A red routes only proven tests: never a timing or budget test, never a whole stage,
and only a test red again alone at the candidate (red alone at the base too is pre-existing and healed). Anything
dropped is no verdict: nothing reverted, the ref kept. Each standing test joins the native-red list so later sharded
gates run it natively.

## Shared desk (several projects on one native box)
- One lease per machine, one owner: `scripts/desk_lease.py`, installed once on the box (its home holds
  `projects.json`, `leases.json` and the state lock; copy `drivelock.py` from overhaul-build beside it). Every
  project's heavy desk job (gates, tests, builds) runs through it; git-only work (a collector, ref mirroring) and the
  watchdog do not need it.
- `projects.json`: `{"projects": {"<name>": {"priority": 0, "probe": {"match": [re], "ignore": [re],
  "children_of": [re]}}}}`. A lower number wins. A job may start, and keeps running, only while no higher-priority
  project holds a live lease, has an unexpired `want`, or shows work in its probe; another project of the same
  priority holding the desk blocks too (first come).
- The probe lets a project win without calling the lease: a process counts when its command line matches `match` or
  it runs under one matching `children_of`, unless it matches `ignore` (console hosts never count). List a project's
  idle loops and servers in `ignore` and `children_of`, so an idle verifier loop blocks nobody while the gate it
  starts does. A project that can call the lease also sends `want` before it ships work, so lower ones yield earlier.
- `run` takes the lease or exits 3 (deferred, never started), re-checks every 15 s and on a higher priority kills the
  job's tree by pid and exits 75 (yielded). With `--result F` both write `{"error": "desk lease: ..."}` to F, so the
  caller's poll ends at once with no verdict. Fail closed: an unreadable process table or config is "not now".
- `desk.py` wires it with `[desk] lease = '<lease home>'` and `project = "<name>"`: every desk run's command goes
  through `desk_lease.py run`, and `may` is asked before a bundle is sent. A lower-priority project's `verified` can
  then starve while the box is busy: give its full gate a path that does not need the box (cloud shards).

## Operations
- A lock whose holder process is positively gone is dead at once (a failed probe is no evidence; probed at most every
  5 s), an age limit catches a hung holder, and the takeover moves the file aside and puts it back if it changed
  since read (`harness.lock`). Storing the holder's start time beside its pid also catches a reused pid.
- A watchdog every 5 min restarts the verifier, the native verifier and the collector when gone, detached (never as
  an agent shell's child), by pid file, with no visible window (on Windows a hidden-window process start; a console
  that pops up on the owner's screen is a defect). It never starts the retired train. It also guards disk: prune
  stale targets below a free-space floor, an owner alarm below a lower one. Shipped: `keepalive.py` (an unknown
  probe is no evidence: it restarts only what is positively gone). Schedule it windowless:
  Windows `schtasks /Create /SC MINUTE /MO 5 /TN <project>-keepalive /TR "\"<pythonw.exe>\" \"<scripts>\keepalive.py\" --home \"<home>\""`
  (pythonw has no console); POSIX `*/5 * * * * python3 <scripts>/keepalive.py --home <home>` in crontab.
- Every background job an agent starts (a land run, a gate, a loop) is windowless and detached the same way, with its
  output in a file the agent tails; never a visible console, never a child of the agent's shell.
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
- The owner's card (the devping skill, one message edited in place) shows these numbers with progress over the WHOLE
  platform: every planned row, roadmap rows not moved into the plan yet and any separate track, each counted once.
  Counting only the current plan read 91% while far less was done. It survives re-planning: done counts the plan's
  packets plus the older packets their `replaces` cells absorb, the rate counts every fix push on main (not only the
  new plan's ids), and open problems come from the class gates' own counts (the day-one baseline, marked not
  measured, until the gate lands; never 0). After one switch to class packets the card read 0 done and 0 per hour.

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
- Release the claim of work the base holds: claims kept until `verified` moved held every lease and blocked the gate
  packet behind 26 packets already on main.
- The gate packet goes first and never waits for work in flight: making it wait idled the fleet. One-offs that touch
  no counter of it never wait for it.
- The duplicate review sees only added functions: types, enums, consts and grown functions need the mechanical check,
  and agents need an owners index regenerated on every push.
- A test killed by its time cap is no verdict, not red; a slow test is a class with a time budget, fixed in the test.
- The owner's card counts the whole platform and survives a re-plan; a card counting one plan's ids read 0 after it.
- Every background job runs windowless and detached; a console window opening on the owner's screen is a defect.
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
