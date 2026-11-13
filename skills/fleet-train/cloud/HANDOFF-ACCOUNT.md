# Continue the cloud fleet on another account

## What keeps running with no cloud usage
- On the train machine: the land train, `cloud.py collect` every 5 minutes (refreshes `cloud-base`, collects cloud
  branches, releases stale claims, keeps HARNESS_CLOUD_TARGET rows assigned), and any notifier.
- On the git host: every session's work is on `cloud/<row>` (done), `cloud/wip-<row>` (in progress, with
  `HANDOFF-<row>.md`) or `plan/cloud-<phase>` (planner output); assignments are on branch `harness-cloud`
  (`assign/*.json`, `plan-tasks.txt`); claims are refs `cloud/claim-<row>`.

## Steps
1. The new account needs access to `<owner>/<repo>` and a cloud environment (Ubuntu default image).
2. Create a routine (or session) with this prompt template:
   ```
   cd <repo root> && git fetch -q origin cloud-base harness-cloud && git checkout -q -B start origin/cloud-base && mkdir -p tools/cloud && git archive origin/harness-cloud kit | tar -x --strip-components=1 -C tools/cloud && sed -i 's/\r$//' tools/cloud/*.sh tools/cloud/*.md && chmod +x tools/cloud/*.sh. Then read tools/cloud/BUILDER.md and follow it exactly: setup, claim ONE row with tools/cloud/claim.sh, build it, self-review, push cloud/<row> (or tools/cloud/release.sh). Base everything on origin/cloud-base. About 90 tool calls; never push main, cloud-base or harness-cloud. End with DONE <row>, BLOCKED <row>: <reason>, HANDOFF <row>, or NO ROW.
   ```
   For planners use the same first line and `PLANNER.md` (claims a phase from plan-tasks.txt, pushes `plan/cloud-<phase>`).
3. Stale claims: `cloud.py collect` releases a claim silent for HARNESS_CLOUD_TTL (default 2 h). To free one at once:
   `git push origin :refs/heads/cloud/claim-<row>`. The next session resumes from `cloud/wip-<row>` (claim.sh prints RESUMED).
4. Nothing changes on the train machine: collect picks up pushes from any account.
