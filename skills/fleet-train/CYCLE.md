# The landing cycle: writers push, a verifier gates, main only moves to green

> **RULE ZERO:** valuable, well made, clean code that can be built out and extended; a solid foundation beats depth;
> one owner per capability, no second copy of anything; never trade quality, tests or gates for speed or feature count.

The default way agents land work. It replaced the batch land train (queue, batches, restacks, bisects, one serial
gate everyone waited on), which measured about 1.5 packets/h at best and 0 for 13 h when it broke. No check was
dropped: each one runs at write time (fast, scoped) or in the verifier (full), never neither.

## Invariant
`main` moves only by fast-forward to a sha whose FULL gate passed: format, strict lint, workspace unit tests, static
quality, every built acceptance and property test, and the LLM duplicate review. Only the verifier moves it.

## What ships and what you build
- Ships in `scripts/` and is reused unchanged: the gate stages (`harness.gate`), the one known-red rule
  (`known_red_tests`), quarantine heal, blame by entry and by file (`blame_entries`, `blame_by_file`), the duplicate
  review (`batch_review`, `self-review --base`), keyed and line-set merge drivers, ratchets, the claim kit
  (`cloud.py assign`, `cloud/`), the lock (`harness.lock`).
- Specified here, built per project on those leaves: the gate scope (every test in the tree but unbuilt rows'; the
  shipped `gate_tests` still selects proven rows' tests only); a per-push `base` in blame ranges (`own_ranges` takes
  one cumulative range today); the writer floor's base (`finish.py` compares with `origin/main` today); receipts and
  the compare-and-swap push; the cloud land script and the collector's evidence replay (the shipped `collect` still
  queues); the verifier loop; the shard protocol and its runner; speculative prefix gates; the native run behind
  main. Keep each one module with typed records.
- Retired: `harness.py train --loop` and its queue. Create `state/train.off` so the heartbeat never restarts it.

## Refs and records
- `integ`: the trunk. Linear (merge commits refused), moved only by compare-and-swap
  (`git update-ref refs/heads/integ <new> <old>`). Writers and the verifier append; nobody rewrites it.
- `main`: the green ref. `native-verified`: the last main sha the native OS's whole gate passed; releases come
  only from it.
- Receipt (one JSON line per push, appended BEFORE the CAS): `id, base, sha, kind (lane | revert | verifier |
  unrecorded), slot, name, rows, pkgs, patch_id, selection, reverts, t, rebased_from`. A trunk range with no receipt,
  or whose patch-id differs from its receipt, is `unrecorded` and is reverted before any gate.
- Reverted record (`{push id: reason, culprits, fails, revert_sha, rows, name}`): the author's next push prints it.
- One hold file (a revert that cannot be built, a ref another writer moved): pushes stop with its reason until
  `unhold --why "<fix>"`; the next cycle then gates the trunk's whole head (the fix is its newest push).

## Writer: the fast floor, then push (2-5 min typical)
One script is the only push path; cloud and local writers run the same one.
1. Clean tree; rebase onto `integ` (a conflict refuses); refuse unformatted files (formatter `--check` with the
   workspace's own edition).
2. Selection: the touched packages; the push's own test files; the stems the full gate does not run whole; the
   never-run list (tests of rows not built yet, tests red on this host at main for environment reasons, budget
   tests: the verifier judges the last two). One function owns "what the full gate runs" and both sides read it.
3. The floor, cheapest first, all compared against the trunk commit it rebased onto (never `origin/main`, which lags
   the trunk): LLM duplicate self-review of added functions TWICE (each must print `"duplicates": []`); static quality
   (rules gate, size caps, owner copies, cycles, plan validation); the own test files (one test-runner call per build
   target, and every file must match a test: a filter matching nothing fails); unit tests and strict lint of the
   touched packages. Path-pin, source-scan and shrink-only ratchet tests and the packages' reverse-dependency closure
   are the verifier's. The known-red list is the verifier's own: those tests are skipped here, then run alone, and
   one that passes is reported as a heal for its owner.
4. Push: the check log must start at exactly the checked sha and end `GATES OK`; write the receipt; CAS the trunk.
   Exit codes: 0 pushed, 1 refused, 3 held, 4 RECHECK (run the floor again), 5 RACE (the trunk moved: rebase, push).
5. Receipt validity after a race: a clean rebase whose patch-id is unchanged keeps a fast receipt valid (the full
   gate judges every candidate whole anyway). A strict receipt, or a fast one whose writer ran tests beyond its own
   files, also needs the trunk to have gained nothing in its packages' closure; else RECHECK.
6. `INTEG_FULL=1` (or a full gate on the native box) is the strict check for the rare push that must be judged first.
A writer never waits for the verifier; its next work bases on the trunk's head.

## Cloud writers
- A session claims one task by pushing a unique commit to a new ref (a second push is refused), works from the
  published base, and pushes WIP plus a short HANDOFF often so another session resumes.
- Its brief is generated per task: the packet text, its status, and every numbered owner ruling that names it.
- To land, the session runs a generated land script: the harness bundle and quality scripts from the kit branch,
  the trunk set to the published base, then the same push script. On `PUSHED` it pushes its branch plus an evidence
  commit (`check.json` = {base, head, selection}, `finish.log`, `ROWS`).
- The collector (every 5 min, git only, nothing built) replays the push with that evidence: the evidence must name
  the branch tip; a lost race rebases (at most 3 times); RECHECK, a conflict or a refusal goes back to the task's WIP
  ref with the reason and the task is listed again. A task is done once main contains its push.

## Verifier: one cycle
1. Fetch. Pushes = the trunk's first-parent chain after main, each matched to its receipt. Unrecorded: revert first.
2. Candidate, FIXED at cycle start: the head capped at 8 pushes, extended so no reverted push sits without its revert
   (a "closed" prefix); right after a red, the prefix before the first culprit (strictly shorter: progress); after an
   unhold, the whole head. Pushes arriving during the gate wait for the next cycle.
3. Duplicate review of main..candidate (duplicates = red; reviewer unavailable = no verdict and an owner alarm,
   never a pass), then the full gate. Failures that are known-red, or all proven red alone at main by quarantine
   heal, do not stand.
4. Green: publish main by CAS (fast-forward only), then mirror it. Bookkeeping the verifier writes (proven rows,
   ratchet tightening, reformatting) goes onto the trunk as its own recorded push and reaches main next cycle.
5. Red: blame, revert, record (below).

## Sharded full gate (census or no verdict)
- Publish the candidate as `gate/<id>` and one spec per shard (`{id, sha, k, tests, targets}`: each test file mapped to
  its build target in a tree AT the candidate) on a kit branch only the verifier writes. Shards are balanced
  longest-first by recorded seconds (24 for ~4,000 tests on 4-core sessions).
- A runner claims shard k with a unique commit on `gate-claim/<id>-<k>`, builds and runs exactly as the gate does
  (every failure re-run alone), and answers with `gate-result/<id>-<k>`: `{fails, tests, head, times, build_secs}`.
  `gate-done/<id>` closes it. Runners serve detached until idle 45 min, and warm the newest gate's build when idle.
- In parallel the native box runs every non-test stage plus the OS-only tests (red on the cloud OS for environment
  reasons, budget and perf tests, and the native-red list), re-runs every cloud failure (a cloud fail counts only when
  red natively: the cloud can pass a test, never forgive one), and takes unclaimed or late (60 min) shards.
- Census: every listed test gets exactly one verdict and the native side computed the same list, else no verdict. A
  shard that did not build, ran 0 tests, ran another head or another list, or a build refusing a target, is no
  verdict: never a pass, never a fail. No verdict falls back to the one-box gate.

## Red: blame, revert, record
- Order (the train's leaves): known-red filter, quarantine heal, blame by assert entry, blame by file, then a linear
  probe at each push's own sha. Reverted pushes and reverts are never suspects; the probe starts after the
  candidate's last revert, and a red already there holds (no innocent revert).
- A probe reproduces a red only by the same stage or one of the same tests red AT the probed sha, gated with that
  sha's own rows and test files.
- Revert set = culprits + dependents (later pushes sharing their files, transitively; any push the revert conflicts
  with; any push in a package a build check of head+revert names, 3 rounds), in ONE commit on the trunk's head by CAS.
  A push already reverted is never a dependent. No compiling revert, or a red before the last revert: hold.
- Escapes: a failure the culprit's own selection did not run is logged and widens the selection index.

## Speculative prefix gates (newly enabled, not yet measured)
Every closed prefix after main gets the same sharded gate at once (default 4 live, shortest first, none past a known
red). The cloud runs them in parallel; the native box serves one at a time (its lock per task), the longest prefix
not likely red first. Main moves to the longest green prefix (review from main clean) at once, so one bad push never
delays the good ones before it. Culprits come from adjacent verdicts (prefix j fails what j-1 passed), no probe.
Verdicts are reused per (tree id, test list). No verdict twice, or an unformatted prefix: that cycle uses the fixed
candidate. A script cannot create cloud sessions; it can wake a pool the owner created once.

## Native run behind main
When the cloud runs another OS than the one you ship: whenever main moves, the native box runs the WHOLE gate on
main's newest head (heads in between skipped). It never blocks main: it holds the native-box lock only to launch and
to read its verdict, and any main gate takes the box at once (the run yields, then restarts on the newest head).
Green moves `native-verified`. Red is routed by the verifier at its next cycle: known-red and heal judged at
`native-verified`, blame over the pushes since then, the one revert on the trunk (main moves forward past it), and
each standing test joins the native-red list so later sharded gates run it natively. No culprit: no hold.

## Operations
- A lock whose holder process is positively gone is dead at once (a failed probe is no evidence; probed at most every
  5 s), an age limit catches a hung holder, and the takeover moves the file aside and puts it back if it changed
  since read (`harness.lock`). Storing the holder's start time beside its pid also catches a reused pid.
- A watchdog every 5 min restarts the verifier, the native verifier and the collector task when gone, detached
  (never as an agent shell's child), by pid file; it never starts the retired train. Feature flags are state files
  read each cycle (sharding, speculation, native run): delete one to turn it off without a restart.
- Status prints pushes, candidates, reverts, holds and the escape rate; the bar stays work on main per hour.

## Measured (one project, 2026-11-24; a Rust workspace of ~4,000 integration tests)
- Old land train: ~1.5 packets/h at best; the gate itself 60-75 min, the rest picks, rejects and bisects.
- Writer fast floor: 1-13 min per push. Per-change full check: 8 min to 7 h by blast radius. A batch of 4 packets
  fully checked on the native box: 50 min, then lost its race with the trunk (RECHECK).
- Full gate on one quiet native box: 36-65 min before the gate ran every test, 55-81 min with all ~4,000. On the dev
  machine running agents its quality stage alone took 7x as long (a whole gate there estimated at 100-140 min).
- Sharded over 24 shards (12-16 taken by cloud sessions, the rest by the native box): 34-35 min, 2 green gates. A
  cloud shard runs in 0.2-2.5 min after a 2-13 min cold build (seconds when warm); the long pole is the native box's
  part (build stages, OS-only tests, the shards no session claimed). Session setup ~2 min.
- Not yet measured: packets/h of the new cycle over a full day, speculative gates, the native run's cycle time.

## Lessons (these replace older guidance; never append a contradiction)
- The full gate runs every built test. A gate scoped to "tests a live row lists" skipped half the files.
- One known-red list, read by writer and verifier alike; a writer-side copy diverged and blocked good pushes.
- Static quality stays in the fast floor: deferring it let cap and wiring reds reach candidates, and blame reverted
  a bystander first.
- Probe with the probed sha's own rows and tests: the candidate's rows at an older sha "reproduced" a red falsely.
- A build that refuses a test target, or a shard that ran 0 tests, is no verdict, not a mass failure.
- A push already reverted is never taken again as a dependent (the second revert conflicted into a hold).
- A fast receipt survives a clean rebase with the same patch-id; demanding a recheck sent finished cloud work back.
- Map shard files to build targets in a tree at the candidate: a stale tree named targets that no longer existed.
- Writers compare against the trunk commit they rebased onto; `origin/main` lags and blamed them for others' work.
- Strip CR from test lists printed on Windows, and refuse a filter that matches no test: both passed in silence.
- Every long-running process has a watchdog, including the scheduled collector (found disabled with sessions landing).
- Linux-green and native-red tests pass a cloud gate: the native run behind main closes that hole.
- The native run yields to main's gate; two gates on one box slow the one main waits for.
- Packet briefs carry the numbered owner ruling that names the packet; a session cannot read the owner's notes.
- Merge first, verify after beat per-change full checks and batch checks. A hosted CI matrix per change was not run
  (estimated ~250-350 runner minutes per change at this suite size: included monthly minutes cover about 10).
- A shared compile cache gave 0% hits on workspace crates across per-slot target dirs; keep per-slot warm targets.
