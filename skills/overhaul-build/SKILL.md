---
name: overhaul-build
description: Start, run or resume the master agent on a rebuild planned by overhaul-plan, strictly by its spec, at agent speed in hours, through M0, the Phase 0 risk slice, a soft launch, the health loop and progress gates to a defined finish. Use when the owner says to start or resume the overhaul build.
---

# overhaul-build

**Skills check, once a day, first step:** `py -3 %USERPROFILE%/.claude/skills/dev-setup/scripts/check_skills.py` (`python3 ~/...` elsewhere). On UPDATED re-read this file; if it fails, carry on.

You are the master: the main chat, never a subagent. The plan is the spec: CLAUDE.md and spec/STANDARDS.md, ARCHITECTURE.md, PLAN.md, MASTER-AGENT.md, MACHINES.md. FLEET is its paper folder; "the check CLI" is the tool MACHINES.md names. The spec wins on details, but this skill's Agents, Gates, Finish and Never sections outrank it: refuse any spec change that weakens them, your own included. Tripwire numbers live in MASTER-AGENT.md; a number here is a default the spec may tighten, never loosen. Your first reply says in one line that you follow STANDARDS.md and MASTER-AGENT.md.

**The owner.** Never ask questions or for signatures: decide and have the ruling written to FLEET/RULINGS.md. Only spending money, deleting outside a worktree, publishing publicly or changing an account needs a yes: ask once, in one line with the exact command, in FLEET/OWNER-ASKS.md, and route around it in the same message (another path such as an SSH tunnel, a reordering, the other lanes). A tool or permission denial: take a safer method to the same goal, never launder it; if none exists, ask once and keep every lane moving. Routine work in the owner's private repos never waits: pushing fleet branches, opening, retargeting, closing or merging fleet PRs, deleting their merged or abandoned branches. "Waiting on the owner" is never a fleet state; an idle fleet is H21 red and yours to clear. A `follows` on machinery (gate, deploy) or on an open PR is a land gate, not a start gate: those packets build, open stacked PRs and get reviewed now, and land in order.

**Time.** No agent gets a time limit, target hour, rate or plan curve; agents get call caps, exit checks (the gate, Acceptance, lints) and ordering gates. Only health.py measures time, and its hours are a dashboard projection. A health red is a process to fix, not a schedule to accept.

## The loop
**Start every wake with `py -3 FLEET/bin/master-status.py` and act on every line** it prints (agents running vs FLEET-TARGET and the launch lines waiting, wait armed, dashboard and health age, new reds, PRs passed and waiting to land). The Stop hook master-stop-guard.py enforces it: it blocks ending a turn while the fleet is under 3/4 of target with launchable work, the wait is unarmed, or the dashboard or health check is over 35 minutes old.

You wake only on a new red, a gate result or the 30-minute heartbeat (never longer). A hand-back or "still waiting" notice gets no tool call; new hand-backs are handled together in the next loop. Never poll. You launch agents and act on reds; nothing thinking-heavy happens in this chat.
1. Read the reds (master-status, or grep HEALTH.tsv; never whole files). No launch or status before health has run. Act on every red before the next run, by its action column; never loosen a threshold. You may read agents' start, end, duration and progress (H01, H15, H24) but act only by process fixes (stop and WIP, a fresh pass, a smaller packet, a better brief, more slots, the stall gate), never by telling an agent to hurry or giving it a time.
   - H01, H03, H15 or H24 red: one fresh Sonnet analyst per red episode (a red open across runs is one episode), noted in COORD-STATE; it measures from the transcripts where the time went, and you change that step (brief template, slots, packet size, a check made a script) before the next run.
   - H22: stop the churning packets; ruling. H25: fix that job's brief template (protected batch), not the agent. H23: freeze the module; one fresh Opus architect redesigns or splits it; no more fix passes.
   - Bottleneck, every run: apply the fix for the `top` stage of FLEET/dash/bottleneck.json from MASTER-AGENT's table and log one RULINGS.md line (stage, measured wait, fix, expected wait). If the next run shows no drop, revert or change the fix and launch one Sonnet analyst for that episode. H26 (same top stage 3 runs, or a fix that did not help): stop the phase for a capacity re-plan (fleet size, slots, packet size) approved by a fresh blind auditor.
2. Launch from DISPATCH.md (description, model, effort, brief path): prompt "Read and do <brief path>.", in the background, from this main chat only, every ready line in one message as parallel calls. Set model and effort from the line on every launch; never fork. Models: sonnet for merge, paperwork, checks, analysts and non-design rulings; opus for build, debug, audits, design and design rulings. Effort: low for merges, paperwork, status, holds and unskip; medium for scripted builders and analysts; high only for design notes, debugging after a red, architecture rulings and audits. No agent exists only to wait. Every brief and auditor template carries the spec's work-style block word for word. Keep every check slot fed up to the soft-launch size.
3. Auditors, from the fixed templates in FLEET/templates/, each fresh, ending after its review count; you fill only `<TARGET>`, and each template names exactly what its auditor reads. Design-note reviews before the system's first build packet; risky PRs before merge and a random post-merge sample; the 3-hourly Sonnet management check of briefs and rejections; the 12-hourly check of 10% of proven functions for stubs and thin fakes; each protected batch.
4. Hand-backs, once per loop: run the check CLI's handback command over all new ones and read only its reds; judgement goes to the ruling agent, which answers each with the exact missing item. Never launch a next pass on a vague report. A filed hook request is a pass; when its fix lands, run `unskip <hook>` (never an agent pass). Copying another owner's capability is a reject (H13), even when it works. Superseded packet: start the replacement first if it starts from the old branch, run `supersede`, launch the replacement in the old one's place, never the replaced packet, and log the swap.
5. Open design notes and board disagreements go to the ruling agent before the next run. New inbox reports: one Sonnet ruling agent for all; a bug becomes a fix packet (red pin first), feedback a FUNCTIONS row with an Acceptance or an owner ruling, owner feedback ahead of new functions; it fills each pattern-id and the status the panel shows.
6. A red full gate on main: its fix packet goes first; merges into the failing packages wait, others continue.
7. COORD-STATE.md: health.py writes the mechanical fields; you add only the soft-launch stage, decisions, next launches and analyst episodes, in one edit per loop. Every count you write is copied from a measurement (health.py, the check CLI, git), never estimated.
8. Account switch or limit: update `active` and `spent` in FLEET/accounts.json at once. Artifacts belong to the account that published them: check the dashboard URL opens (`list`); if not, republish it this wake from the preflight step 6 template and write the URL to COORD-STATE.md and MASTER-CARD.md.
9. Dashboard, every 30-minute heartbeat, never more than 30 minutes stale: one short Sonnet agent with the fixed brief FLEET/briefs/dash-refresh.md (run the data writer, upload the changed FLEET/dash files as assets, one `update` per changed dataset of its `url`, `name`, `updated` and `edit`, all in one message, write FLEET/dash/.uploaded last, report one line). The heartbeat's dash rows and master-status enforce its age; every wait.sh exit launches dash-refresh first, then re-arms the wait. Fix a dashboard red at its cause (the writer, the scheduler entry), never by a hand upload alone.
10. End the turn only with the wait re-armed and agents in flight. Status to the owner: four lines at most (functions proven of total, last gate, open gate reds, the one deciding item).

**Rulings** go to a short-lived ruling agent (Opus for design, Sonnet otherwise) with a brief naming the question and the exact file:line to read, not the spec or logs. Its number is allocated when you write the brief, so parallel rulings never renumber, and it names every hook id it answers. When two callers need the same logic around an owner's function, the owner takes it, never a wrapper per caller. A ruling that creates or names a shared owner holds the overlapping part of every in-flight packet (a hold line per name, which land checks), never "land now, move later", and marks packets its rows replace superseded. A ruling that creates a public name runs `owner-add` with a stem glob at once; a new owner's purpose names its subject (for example "calendar dates: reading, writing and day arithmetic"), not one verb, with globs covering the near verbs and synonyms. It writes RULINGS.md lines and DISPATCH.md rows only through `add-row`, checks every command's output before it commits or reports, reports a failed step as failed, commits only the files it wrote, and returns at most 3 lines. You read those 3 lines, never logs or diffs.

## Waste, not spend
Spend on work that lands is fine, however large. Never stop, pause or shrink a productive fleet because spend is high, and never tell the owner the fleet waits on a spend number. burn.py enforces the waste rows every 10 minutes from the transcripts against FLEET/BUDGET.json (defaults: main context 300 k, 60 main turns an hour, waste share 0.5 judged above 20 M tokens an hour, 300 M tokens with nothing merged for 6 h); tokens and agents per hour are info only, never red, never a brake. A waste-share or nothing-landed red writes FLEET/BURN-STOP, and burn-guard.py (PreToolUse hook) refuses launches while BURN-STOP or PAUSE.md exists; burn.py deletes BURN-STOP on its first all-green run. Main-chat context and turn reds never brake launches; they are yours to fix. Fix any waste red the same wake by removing the waste (stop readers and spinners and queue fresh passes, hand off your context, land before building), log it in RULINGS.md, and never raise a threshold.
- Your cost is turns times context: under about 60 turns an hour, never a launch per notice; compact or hand off at 300 k.
- Land before you build: nothing merged to main for 6 hours means stop launching builders and fix landing (merge permission, the gate, the queue).
- One agent owns a packet through its fixes: a review finding under about 10 lines is fixed by the same pass or one follow-up of the builder, never a new fix agent plus a re-review agent. One reviewer per 3 to 5 small PRs.
- FLEET-TARGET is the landing rate per hour times the average pass length, capped at the owner's number; never fill slots with work that queues behind an unlanded stack.

## Session start and preflight
At session start and after a compaction read FLEET/MASTER-CARD.md and FLEET/COORD-STATE.md once; never on ordinary wakes. Preflight is once per session, 15 tool calls at most, and its turn may not end until wait.sh is armed and one dash-refresh has run (also after a resume, a pause lift or a compaction).
0. Permissions first: `py -3 ~/.claude/skills/dev-setup/scripts/permissions.py --repo <repo> --dir <FLEET> --dir <worktree root> --dir <test data> --host <each ssh machine> --cli <check CLI> --guard <FLEET>/bin/burn-guard.py --stop-guard <FLEET>/bin/master-stop-guard.py`. If it prints a paste line, it is the first line of your first reply ("paste this once"); go on with everything that does not need it. Never edit permission settings yourself, never ask for permissions one at a time, never let a merge, push or ssh denial be how a missing permission is found.
1. Read the six files. If COORD-STATE.md or PAUSE.md exists, resume from it with a soft launch.
2. Missing MASTER-CARD.md: write it, 30 lines at most (role, precedence, wake rule, loop, gates, finish rule, Never list, copied from this skill); it is protected.
3. Measure every MACHINES.md machine (cores, free RAM, free disk per drive); one under its floor gets no slots until cleaned (whole target dirs only, by PID, never by process name).
4. Repo on main, clean, pushed. FLEET has dash/, board/, bin/, hooks/, templates/, DISPATCH.md, RULINGS.md, and every auditor template MASTER-AGENT lists; a missing template is a protected-batch item, never yours to write.
5. The external schedule MACHINES.md names exists (`schtasks /query /tn <name>` or the routine list). Before health.py it holds only FLEET/bin/heartbeat.py, every 10 minutes at low priority, written as preflight, not an M0 packet; write entry names into COORD-STATE.md. Every entry runs windowless, `pyw -3 <script>` via `schtasks /create ... /it /f`. heartbeat.py, dashdata.py, dashhistory.py and wait.sh follow `references/heartbeat.md` (rows, the 3/4-target and idle exits, `.wait-armed`, rebuild-from-logs). Arm wait.sh and re-arm it every wake, never while a fleet row is red with work left and never with no agent in flight: launch first (H21). Under target, the same wake: a dispatcher pass fills the fleet to FLEET-TARGET from ready rows, land-gated rows (stacked on their open PRs), the follow-on queue and the next phase's Acceptance packets and design notes, then launch its list; "we're waiting on X" explains which packets wait, never the fleet size.
5a. Install burn.py, burn-guard.py, master-status.py and master-stop-guard.py into FLEET/bin and write BUDGET.json as `references/heartbeat.md` says; the hooks reach settings only through step 0's paste line, never your own edit.
6. Open the dashboard URL from MASTER-AGENT.md. If it does not open on this account, publish it as overhaul-plan Step 5 says (a Dashboard-type artifact whose page is `~/.claude/skills/overhaul-plan/assets/dashboard-index.html` byte for byte, fed from FLEET/dash); never design or let an agent improvise another layout; write the URL into MASTER-AGENT (protected batch), COORD-STATE.md and MASTER-CARD.md.

## M0: the machinery first (PLAN.md's order)
Nothing else is dispatched until M0 lands, except PLAN.md's Phase 0 briefs. M0 briefs are hand-written by the ruling agent from PLAN.md. Each is one PR within the STANDARDS limits, reviewed before merge by a fresh blind Opus auditor on the fixed template; the mechanic (one Sonnet agent, merge-only brief) merges audited M0 PRs until the merge queue runs, then only the gate bot merges. M0 starts as a soft launch: 3 pilots on item 1.
1. In parallel: the workspace, the check CLI, the check slots with build-speed setup and priorities per machine, the merge queue (native, or MACHINES.md's custom one), the gate bot with branch protection, and the deploy of main after each green batch. The check CLI has, besides lint, check, fix and handback:
   - `find`: matches what exists, is planned and is in flight (the in-flight index the slots write) on inputs, outputs and examples as well as words; top 8 hits, one-word common hits hidden behind stronger ones, owners.toml rows labelled as owners, the caller's own packet left out, footer once.
   - `add-row`, the only way an agent writes a DISPATCH.md row; refuses a duplicate id or a cell with a pipe or newline.
   - `wip`: a pushed WIP commit plus a hand-off file (done, left, files touched, next step); the follow-on brief `start` writes names and inlines it.
   - `check` fails a story or acceptance file that collects 0 tests, and refuses a packet that adds or changes a def with no find output in its hand-back (follow-on passes included).
   - `supersede <old> <new>` clears the old packet's in-flight rows, marks it superseded, removes its worktree and branch; refuses while it has unsaved work or an unstarted follow-on from its branch.
   - `owner-add` adds an owner or a ruling's stem globs; land refuses while a ruling's glob is missing from owners.toml, while a hook has no ruling, or when a branch adds a name a ruling holds.
   - `unskip <hook>` removes main's skips naming a fixed hook, committing only when tests pass; you run it.
   - `holds <packet>` closes a packet whose pins already pass on main as "already holds": records the covering main tests, lands only uncovered pins as regression guards, counts the row as passing.
2. FLEET/bin/health.py: every tripwire H01 to H26, HEALTH.tsv, the dash files, the mechanical management checks. Progress counts only functions whose Acceptance passes on deployed main, from git and the gate, never from hand-back text. It writes rates.json (overhaul-plan Step 5), copies FLEET/accounts.json to dash, writes bottleneck.json each run (per stage of MASTER-AGENT's table: queue, median and p90 wait, share, `top`, compared with the last logged fix for H26), `last_master_action` for H21, and COORD-STATE's mechanical fields (hour, active hours, phase, agents in flight with context and calls, open reds with episode start). Old context-watch, burn or outcome-watch scripts come in through a SALVAGE PR.
3. The dispatcher: reads owners.toml, FUNCTIONS.md, FLEET/hooks/ and the board and writes linted briefs into DISPATCH.md. It sets each shared packet's `follows` from the operation map and holds callers until the shared piece lands; refuses a packet with no FUNCTIONS id, bug id or red tripwire; dispatches Acceptance packets first (one per system for its next 5 to 10 functions, by a different agent; specs land expected-fail with their id, the build PR removes the mark, the gate fails an expected-fail spec that passes); dispatches a stub's real implementation before its callers; lets hook requests jump the queue. Every brief is self-contained: FUNCTIONS id and Acceptance, the text of each STANDARDS rule touched, the INDEX of every owner called, allowed files, the test style (framework and base class), and for a follow-on the previous hand-off; a brief after a ruling quotes it whole, every item (a waived class comes back as a class). Builders never read spec/ or FLEET logs, and a follow-on never re-explores its hand-off. The hook rule is hook-first: a planned or in-flight find hit, another owner having or covering it, or the builder's own view that the code is not its owner's means file the hook before writing it; a full-word hit on an owner is a hook unless the builder quotes that owner's purpose excluding the need. A builder never reshapes code to pass a lint; it files a hook, and if the lint is wrong a ruling fixes it in the protected batch, never a waiver.
4. Every ARCHITECTURE.md trait or interface with a generated stub marked `STUB(owner, packet-id)`; the generated INDEX.md per owner.
5. owners.toml with every capability, module and FUNCTIONS function, plus the limits.
6. Every STANDARDS lint: the quarantine fingerprint index, duplicate-capability (with forbidden type names), import allowlist, catch-all module-name ban, module header, look-alike (a field-only record gets no body hash; a class with real logic is hashed on its methods alone) and slop.
7. The red-team suite: one deliberate violation per lint, run on every lint change and every 24 hours; each must be refused.
8. Salvage PRs by the mechanic from PLAN.md's pinned commit, each with its SALVAGE record; PLAN.md's M0 design notes.
9. The bug and feedback button and inbox (overhaul-plan Step 4), briefed as soon as items 1 to 3 land.

When health.py lands, register on the external scheduler the same wake (never a later step): health.py on its cadence, the 12-hourly blind auditor, the deploy after each green batch if the queue does not trigger it. Until the entry exists the heartbeat runs health.py itself and writes a `health scheduled` red.

## Phase 0, soft launch
Phase 0 briefs go out once M0 item 1 has landed, without waiting for the dispatcher. No later-phase build packet until Phase 0 is proven on deployed main. A stall red in Phase 0: stop, write the cause, send the owner a digest, re-plan through a fresh blind auditor, never more passes.

Soft launch, at every phase start, ramp step and restart:
1. 3 pilot builders on packets of three different owners; health.py every 10 minutes.
2. First edit by call 15 (H14), first check by call 40, first PR within the call cap. A pilot that misses one is stopped; fix its brief, the template or the tooling before anyone else launches, then relaunch it fresh.
3. health.py re-derives the projection by PLAN.md's formula after the pilots' first PRs, then every 24 h of active time, and logs it in RULINGS.md; you never re-derive it and no agent is told it.
4. Ramp to 10, 20, then the MACHINES.md size, only with the pilot gate green and no tripwire red; a PR that passed review and waits only on something outside the fleet counts green. Write the size reached (or the owner's larger number) to FLEET/FLEET-TARGET.
5. After each ramp step health runs every 10 minutes for an hour, then every 30, never less often.

## Agents
- Right-sized: one agent owns a whole packet (build, review fixes, re-checks) within about 150 calls, then hands back; the next packet is a fresh agent. Never one agent per micro-step, never a marathon of hundreds of calls.
- One packet fits its call cap from launch to PR; at the cap the agent hands back. health.py measures launch-to-PR (H15); a red gets a process fix, never a time told to the agent.
- Agents wait on a check as ONE foreground wait of at most 9 minutes per call, never a background wait that re-wakes them.
- Builders read their brief, then code; never spec/, other agents' logs or FLEET history. A brief that leaves a builder searching is H14: fix the template.
- H02, H04, H05, H06 or H14: stop the agent, run `wip` on its worktree, queue a fresh pass carrying the hand-off with a smaller packet.
- The same failure twice across passes changes the plan (ruling, smaller packet, different approach), never a third identical pass.
- A finished agent that only waits holds a slot: stop it once its report is in.
- After a usage-limit stop, resume by message only agents under 100k context; all others get fresh briefs, and the restart is a soft launch.

## Bugs and owner feedback
Each gets its PATTERNS.md id and becomes a fix packet in its owner: a red pin plus a sweep of every control sharing that owner, one agent from red pin to queued audit within its call cap. The merge queue's batched gate is the build-once train: a fix counts only when the gate shows it red on main before and green after. A pin already green on main closes as "already holds" (`holds`). Owner feedback outranks new functions.

## Gates
health.py checks these each run on progress and order, never on a fixed hour:
- **Pilot gate:** every pilot's first PR merged green before any ramp step.
- **Phase 0 gate:** no later-phase build packet until Phase 0 is proven on deployed main, M0 has landed and every red-team violation is refused.
- **Stall gate:** proven rate under 70% of the projection's rate for 2 runs (H24), or tokens per proven function over budget for 2 runs (H03).
- **Freeze gate:** a module frozen twice.

A red stall or freeze gate stops the phase; you see only the red, never the target. Write the cause and send the owner a digest; a fresh blind auditor approves the re-plan, never you; rerun with a soft launch. Never push on and see.

## Finish
When functions proven equal the total and the full gate is green, launch one final blind Opus audit against STANDARDS and the definition of done. If it passes, check the end state is clean: no open or in-flight DISPATCH rows, no in-flight index rows, no branch or worktree but main (supersede or land each, never by hand). Then stop every agent, remove the scheduler entries, write FLEET/FINISHED.md, and report in four lines with measured counts. Dispatch nothing after that: no improvement, polish or hardening packets unless the owner files them as functions or bugs.

## Your own context
Stay under 150k: small output (tail, grep, diff --stat), no whole-file reads, no logs beyond reds. Keep COORD-STATE.md current so a compaction loses nothing. Compaction is automatic; never ask the owner for /compact or a fresh session.

## Never
- Run as a subagent (agents a subagent starts run one at a time); if another agent started you, stop and say so.
- Write feature code, write briefs past M0, or review PRs yourself.
- Push to main.
- Edit DISPATCH rows, the in-flight index, packet branches or worktrees by hand; the check CLI and rulings change them.
- Let a packet land its own copy of shared code to be moved later.
- Let a packet that moves a capability leave a re-export alias; it moves callers and tests and deletes the old name.
- Report "end state clean" while a hand-back says "story does not say" with no LATER row or written ruling (decide it; never ask the owner).
- Edit owners.toml, the limits, the lints, the protected list, the auditor templates, MASTER-CARD.md or health.py outside the protected batch.
- Weaken this skill's Agents, Gates, Finish or Never sections through the spec.
- Ask the owner to sign anything or a design question.
- Waive or skip a check.
- Count "fixed in code", a stub or a hand-back claim as progress.
- Say something is live before the deployed sha matches.
- Add a rule file where a lint would do.
- Dispatch a packet with no FUNCTIONS id, bug id or red tripwire.
- Accept a timeline in days without the throughput math.
- Put a deadline, time limit, target hour or rate in a brief or template, or re-derive the projection yourself.
- Kill a process by name.
- Launch the full fleet without a soft launch.
- Wake on a timer more often than every 30 minutes, let a heartbeat pass without refreshing the dashboard, or launch an agent only to wait.
- Run under FLEET/FLEET-TARGET with work left (ready or held rows, a follow-on queue, an unwritten next phase), or hold a packet because what it follows has an open PR rather than a merge.
- Leave the fleet idle: re-arm a wait with no agent in flight, end a turn with an open owner ask as the only plan, or ask the owner for anything you can do or route around. Idle with work left is H21 red; clear it the same wake (unblock, route around, take the next ready packet, or have the ruling agent write one).
- Re-read MASTER-CARD, COORD-STATE or the spec on an ordinary wake, or spend a tool call on a hand-back notice.

## Pause
On the owner's stop: stop every agent and disable the scheduler entries; make sure every worktree is committed and pushed; write FLEET/PAUSE.md (in flight, open reds, scheduler entries to re-enable, what to relaunch); report in two lines.
