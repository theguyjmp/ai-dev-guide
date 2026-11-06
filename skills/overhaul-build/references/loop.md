# Loop details (overhaul-build)

Read when acting on a tripwire red, setting a launch's model, launching auditors, filling the fleet, triaging the inbox, switching accounts or fixing the dashboard refresh.

**Tripwire reds** (each by HEALTH.tsv's action column; never loosen a threshold):
- H01, H03, H15 or H24: one fresh Sonnet analyst per red episode (a red open across runs is one episode; no second analyst until it clears), noted in COORD-STATE. It measures from the transcripts where the agent time went; change that step (brief template, slot count, packet size, a check made a script) before the next run.
- H22: stop the churning packets and have a ruling written.
- Any red with the same action for 3 heartbeats: process defect; find the cause that wake (machinery missing, wrong tool, false check). merge_waiting: fix the merge path (gate bot registered, its PATH, its log), never an owner merge line.
- H25: fix that job's brief template (a protected-batch item), not the agent.
- H23: freeze the module and launch one fresh Opus architect to redesign or split it; no more fix passes to it.
- Bottleneck, every run: apply the fix MASTER-AGENT's bottleneck table gives for the `top` stage of FLEET/dash/bottleneck.json and log one RULINGS.md line (stage, measured wait, fix, expected wait). If the next run shows that stage's wait did not drop, revert or change the fix and launch one Sonnet analyst for that episode to measure why.
- H26 (the same top stage for 3 runs, or a fix that did not reduce its wait): stop the phase for a capacity re-plan (fleet size, slots, packet size), approved by a fresh blind auditor as for the stall gate.

**Model and effort by job** (the DISPATCH line carries both; set them on every launch, never fork):
- Sonnet: merge, paperwork, checks, analysts, non-design rulings. Opus: build, debug, audits, design and design rulings.
- Effort low: merges, paperwork, status, holds, unskip. Medium: builders on a scripted brief, analysts. High only: design notes, debugging after a red, architecture rulings, audits.

**Filling to FLEET-TARGET** (same wake the fleet is under target): the dispatcher pass takes ready rows, land-gated rows (stacked on their open PRs), the follow-on queue, and the next phase's Acceptance packets and design notes written ahead. Never a held row, and never a later-phase build packet before the Phase 0 gate is proven (master-status.py already leaves those out).

- Auditors, from the fixed templates in FLEET/templates/, each fresh, ending after its review count; you fill only `<TARGET>`, and each template names exactly what its auditor reads (never the whole spec). Design-note reviews before the system's first build packet; risky PRs before merge and a random post-merge sample; the 3-hourly Sonnet management check of briefs and rejections; the 12-hourly check of 10% of proven functions for stubs and thin fakes; each protected batch.
- Open design notes and board disagreements go to the ruling agent before the next run. New inbox reports: one Sonnet ruling agent for all; a bug becomes a fix packet (red pin first), feedback a FUNCTIONS row with an Acceptance or an owner ruling, owner feedback ahead of new functions; it fills each pattern-id and the status the panel shows.
- Account switch or limit: update `active` and `spent` in FLEET/accounts.json at once. Artifacts belong to the account that published them: check the dashboard URL opens (`list`); if not, republish it this wake from the preflight step 6 template and write the URL to COORD-STATE.md and MASTER-CARD.md.
- Dashboard, every 20-minute heartbeat, never more than 20 minutes stale: one short Sonnet agent with the fixed brief FLEET/briefs/dash-refresh.md (run the data writer, upload the changed FLEET/dash files as assets, one `update` per changed dataset of its `url`, `name`, `updated` and `edit`, all in one message, write FLEET/dash/.uploaded last, report one line). The heartbeat's dash rows and master-status enforce its age; every wait.sh exit launches dash-refresh first, then re-arms the wait, even when the last refresh is recent. It stays a master launch: headless `claude -p` runs have no Artifact tool, so a scheduled run cannot upload. Fix a dashboard red at its cause (the writer, the scheduler entry), never by a hand upload alone.
