# Cloud planner brief (one session, one plan task)

You plan rows of the rebuild in a cloud sandbox: move rows of ONE roadmap phase from `spec/ROADMAP.md` into buildable
rows of `spec/FUNCTIONS.md`, exactly like the rows already planned there. Builders then pick them up (this grows the
frontier). You write no product code beyond the stubs the plan needs.

1. Setup: if `/opt/harness/env.sh` is missing run `bash tools/cloud/setup.sh 2>&1 | tail -5`, then `source /opt/harness/env.sh`.
2. Claim a task: `git fetch -q origin harness-cloud`, read `git show origin/harness-cloud:plan-tasks.txt` (one phase id
   per line). For each, make a UNIQUE claim commit and push it to a new ref:
   `git commit -q --allow-empty -m "claim plan <phase> $(date +%s%N)" && git push -q origin HEAD:refs/heads/cloud/plan-claim-<phase>`
   (a unique commit makes the push fail when another session holds the ref; pushing the same commit would "succeed" as
   up-to-date and two sessions would plan the same phase). Then `git reset -q --hard origin/cloud-base`. The first push
   that succeeds is yours. None left: stop and report `NO TASK`.
3. Branch `plan/cloud-<phase>` from `origin/cloud-base` (or from `cloud/wip-plan-<phase>` if it exists: read its HANDOFF
   first; use `<id>` = `plan-<phase>` below). Read with grep and line ranges only (no whole files over 300 lines):
   the phase's ROADMAP rows; 3 recent planned rows in FUNCTIONS.md and their acceptance tests as the pattern; the
   ARCHITECTURE bullets of the modules your rows name; the decision log for ids in use.
4. Plan at most 15 rows of that phase (the first ones still in ROADMAP whose deps are planned or proven). For each row:
   - append it to FUNCTIONS.md with status planned, module, deps, entry, file list and work packet; split a row bigger
     than one builder (more than 3 files or more than one behaviour);
   - leave a "moved to FUNCTIONS" note in ROADMAP;
   - write ARCHITECTURE bullets; a new module gets Paths, Depends on (acyclic), Owns and `Why new:`; reuse existing owners;
   - write one acceptance test per row, driving only the public entry, that FAILS now (prove it by running it);
   - register the test in the build manifest and add a stub (`todo!()`) the builder replaces.
5. Keep test functions, source files and test files under the project's size caps; format your files. Run the project's
   strict lint and unit tests; both must be clean.
6. Commit `plan: phase <N> part (<k> rows)` and `git push -q origin plan/cloud-<phase>`. Never push main, cloud-base or
   harness-cloud. Report `DONE <phase> <k> rows; left in ROADMAP: <ids>`.

**Git discipline.** Same as the builder: push `cloud/wip-<id>` with `HANDOFF-<id>.md` (at most 10 lines) after the first
compiling step and every ~15 tool calls or ~10 minutes; on a limit warning push it and report `HANDOFF <id>: cloud/wip-<id>`.

About 80 tool calls. If the same step fails twice, commit what is clean, push, and report what blocked you.
