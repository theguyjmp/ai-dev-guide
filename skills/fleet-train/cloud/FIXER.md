# Cloud fixer brief (one session, one clean-up fix task)

> **RULE ZERO:** valuable, well made, clean code that can be built out and extended; a solid foundation beats depth;
> one owner per capability, no second copy of anything; never trade quality, tests or gates for speed or feature count.

You fix ONE clean-up packet of the rebuild in a cloud sandbox (a second copy of a capability, a swallowed error, an id
rule, a stored shape an audit found). Your branch lands through the landing cycle (fleet-train CYCLE.md): the verifier gates it with the full suite.

1. Setup: if `/opt/harness/env.sh` is missing run `bash tools/cloud/setup.sh 2>&1 | tail -5`, then `source /opt/harness/env.sh`.
2. Claim: `bash tools/cloud/claim_task.sh fix`. It claims the first free task of `fix-tasks.txt` on harness-cloud
   (unique commit pushed to `cloud/fix-claim-<task>`) and checks out `fix/cloud-<task>` from the base, or from
   `cloud/wip-fix-<task>` merged with the base. `NO TASK`: stop and report it. `MERGE CONFLICT`: resolve, commit.
   Read the task `git show origin/harness-cloud:fix-tasks/<task>.md`, why an earlier branch came back
   `git show origin/harness-cloud:fix-rejects/<task>.md` (fix that first) and `HANDOFF-fix-<task>.md` if resuming.
3. Recheck the packet on your branch (cite file:line). Already done: record it (step 6) so the task closes.
4. Write the proving test first and see it red, through the public entry (or the owning module's unit seam); "old
   documents still load" where a stored shape changes. Never edit or weaken another row's test.
5. Fix in the owner: before adding any function find the owner (the project's index search, then grep for the same rule
   or body); fold every copy into the owner and delete it. Green: the new test, the owning modules' unit tests, the
   acceptance tests that drive what you touched, the strict linter on what you touched, the formatter.
6. Record the packet's roadmap rows as fixed the way the project records finished clean-up rows, so no planner plans them
   again. Commit `fix(<module>): <task> <what>`.
7. Self-review twice: `bash tools/cloud/added_fns.sh` lists the functions you added; run the duplicate review in
   `tools/cloud/review_prompt.md` on that list, fix every duplicate (call the owner, delete your copy), commit, review
   again: both runs must report none.
8. Check: `git show origin/harness-cloud:fix-check.sh > /tmp/fix-check.sh && bash /tmp/fix-check.sh 2>&1 | tail -60`
   (strict lint, unit tests, quality scripts, rules check, merges with the base). Fix every FAIL, commit, run it again
   until `FIX CHECK OK`: it stamps your last commit for its own tree and only a stamped tip is queued, so commit nothing
   after it.
9. Check the claim is still yours (`git ls-remote origin refs/heads/cloud/fix-claim-<task>` shows the sha in
   /tmp/fix-claim; if not, push WIP with HANDOFF and stop), then `git push -q origin fix/cloud-<task>`. The collector lands
   it. Report `DONE <task>: <what changed>`. Never push main, the base or harness-cloud.

**Git discipline.** As the builder: push `cloud/wip-fix-<task>` with `HANDOFF-fix-<task>.md` (at most 10 lines) after the
red test and every ~15 tool calls; on a limit warning push it and report `HANDOFF fix-<task>: cloud/wip-fix-<task>`.
A claim with no push (claim, WIP) for HARNESS_CLOUD_TTL is released.

About 100 tool calls. If the same step fails twice, push the WIP with HANDOFF naming the blocker and report
`BLOCKED <task>: <reason>`.
