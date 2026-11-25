# Cloud fixer brief (one session, one clean-up fix task)

> **RULE ZERO:** valuable, well made, clean code that can be built out and extended; a solid foundation beats depth;
> one owner per capability, no second copy of anything; never trade quality, tests or gates for speed or feature count.

You fix ONE clean-up packet of the rebuild in a cloud sandbox. A packet ends a problem CLASS (a second copy of a
capability, a swallowed error, an id rule, a stored shape) in its module group, every instance, never one instance:
its `class` and `gate` cells name the class and the gate (lint, structural test, shrink-only ratchet or shared owner)
that keeps it gone. Your branch reaches `main` after its affected checks; the verifier runs the full suite behind it
and reverts what fails (fleet-train CYCLE.md).

1. Setup: if `/opt/harness/env.sh` is missing run `bash tools/cloud/setup.sh 2>&1 | tail -5`, then
   `source /opt/harness/env.sh`. Seed the shared warm build cache if the project publishes one (CYCLE.md "Shared warm
   build cache"): it prints WARM or COLD, and COLD is never a reason to stop.
2. Claim: `bash tools/cloud/claim_task.sh fix`. It claims the first unclaimed task of `fix-tasks.txt` on
   harness-cloud, which lists only claimable tasks (class check passed, prerequisites landed, footprint overlapping
   nothing in flight; a task not listed is never yours to take): a unique commit pushed to `cloud/fix-claim-<task>`.
   It checks out `fix/cloud-<task>` from the base, or from `cloud/wip-fix-<task>` merged with the base. `NO TASK`:
   stop and report it. `MERGE CONFLICT`: resolve, commit.
   Read the task `git show origin/harness-cloud:fix-tasks/<task>.md`, why an earlier branch came back
   `git show origin/harness-cloud:fix-rejects/<task>.md` (fix that first) and `HANDOFF-fix-<task>.md` if resuming.
3. Recheck the packet on your branch: grep the WHOLE module group for the class, not only the cited lines (cite
   file:line). Already done: record it (step 6) so the task closes.
4. Write the proving test first and see it red, through the public entry (or the owning module's unit seam); "old
   documents still load" where a stored shape changes. Never edit or weaken another row's test. A new test file is
   only added, never declared in a shared list (the build discovers it).
5. Fix in the owner. Before adding ANY public item (function, type, enum, trait, const, static) or renaming one, find
   the owner: `index_api.py --find "<purpose words>"` (the project's owners index), then grep for the same rule or
   body. A public name another module already declares is refused at land (rules_gate `dup_problems`): reuse that
   owner, or pick a name that says what differs. Before growing a function, check it does not now repeat a sibling's
   body (one function with a flag or a shared step instead). Fold every copy into the owner and delete it. Fix every
   instance of the class in your modules and lower their ratchet entries to the new count (never raise one; another
   group's entries are not yours). Name each owner you reused in the commit message (`reuses: <module>::<item>`).
   Green: the new test, the owning modules' unit tests, the acceptance tests that drive what you touched, the strict
   linter on what you touched, the formatter.
6. Record the packet's rows as fixed the way the project records finished clean-up rows, so no planner plans them
   again. Never hand-edit a shared list or generated index: add your own files and let the generator register them.
   Commit `fix(<module>): <task> <what>`.
7. Self-review twice: `bash tools/cloud/added_fns.sh` lists the functions you added; run the duplicate review in
   `tools/cloud/review_prompt.md` on that list, fix every duplicate (call the owner, delete your copy), commit, review
   again: both runs must report none.
8. Land: push the WIP (Git discipline), then run the project's land script IN THE BACKGROUND with its output in a
   file: `git show origin/harness-cloud:fix-land.sh > /tmp/fix-land.sh && bash /tmp/fix-land.sh <task> <rows...>
   > /tmp/land-<task>.log 2>&1 &` (CYCLE.md "Cloud writers"). It rebases onto the base, runs the writer checks and
   refuses on the first red (fix it, commit, run it again); on `GATES OK` it pushes your branch plus the evidence and
   waits for the outcome. Commit NOTHING after it starts: only the exact checked sha lands.
9. Read its last line between other steps: `LANDED <push id>` or `LAND PENDING`: report `DONE <task>: <what
   changed>`. Sent back: it re-claims, rebases onto the new base and lands again by itself (3 rounds at most).
   `NOT LANDED: <why>`: fix, commit, run it again. Never push main, the trunk, the base or harness-cloud.
10. Next task: while a land runs, claim the next listed task in its own worktree (`git worktree add -q --detach
   <dir> origin/<base>`, then steps 2-8 there). Only one land script runs at a time; never fold an unlisted task
   into a land.

**Git discipline.** As the builder: push `cloud/wip-fix-<task>` with `HANDOFF-fix-<task>.md` (at most 10 lines) after the
red test and every ~15 tool calls; on a limit warning push it and report `HANDOFF fix-<task>: cloud/wip-fix-<task>`.
A claim with no push (claim, WIP) for HARNESS_CLOUD_TTL is released.

About 100 tool calls. If the same step fails twice, push the WIP with HANDOFF naming the blocker and report
`BLOCKED <task>: <reason>`.
