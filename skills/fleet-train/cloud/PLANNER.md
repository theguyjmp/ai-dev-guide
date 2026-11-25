# Cloud planner brief (one session, one plan task)

You plan rows of the rebuild in a cloud sandbox: move rows of ONE roadmap phase from `spec/ROADMAP.md` into buildable
rows of `spec/FUNCTIONS.md`, exactly like the rows already planned there. Builders then pick them up (this grows the
frontier). You write no product code beyond the stubs the plan needs.

1. Setup: if `/opt/harness/env.sh` is missing run `bash tools/cloud/setup.sh 2>&1 | tail -5`, then `source /opt/harness/env.sh`.
2. Claim a task: `bash tools/cloud/claim_task.sh plan`. It claims the first free phase of `plan-tasks.txt` on
   harness-cloud (unique commit pushed to `cloud/plan-claim-<phase>`) and checks out `plan/cloud-<phase>` from the
   base, or from `cloud/wip-plan-<phase>` merged with the base (use `<id>` = `plan-<phase>` below; read its HANDOFF
   first). `NO TASK`: stop and report it. `MERGE CONFLICT`: resolve, commit.
3. Read with grep and line ranges only (no whole files over 300 lines):
   the phase's ROADMAP rows; 3 recent planned rows in FUNCTIONS.md and their acceptance tests as the pattern; the
   ARCHITECTURE bullets of the modules your rows name; the decision log for ids in use.
4. Plan at most 15 rows of that phase (the first ones still in ROADMAP whose deps are planned or proven). For each row:
   - append it to FUNCTIONS.md with status planned, module, deps, entry, file list and work packet; split a row bigger
     than one builder (more than 3 files or more than one behaviour);
   - leave a "moved to FUNCTIONS" note in ROADMAP;
   - write ARCHITECTURE bullets; a new module gets Paths, Depends on (acyclic), Owns and `Why new:`; reuse existing owners;
   - write one acceptance test per row, driving only the public entry, that FAILS now (prove it by running it);
   - add the test as its own file (the project's test discovery registers it; never hand-edit a shared test list or
     index) and a stub (`todo!()`) the builder replaces.
5. Keep test functions, source files and test files under the project's size caps; format your files. Run the project's
   strict lint and unit tests; both must be clean.
6. Commit `plan: phase <N> part (<k> rows)`, push the WIP, then land it with the project's land script in the
   background, as `FIXER.md` steps 8-9 say (`plan-land.sh <phase>` from harness-cloud). Never push main, the trunk,
   cloud-base or harness-cloud. On `LANDED` or `LAND PENDING` report `DONE <phase> <k> rows; left in ROADMAP: <ids>`.

**Git discipline.** Same as the builder: push `cloud/wip-<id>` with `HANDOFF-<id>.md` (at most 10 lines) after the first
compiling step and every ~15 tool calls or ~10 minutes; on a limit warning push it and report `HANDOFF <id>: cloud/wip-<id>`.

About 80 tool calls. If the same step fails twice, commit what is clean, push, and report what blocked you.
