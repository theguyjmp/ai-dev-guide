---
name: overhaul-build
description: Start, run or resume the master agent on a rebuild planned by overhaul-plan, strictly by its spec, at agent speed in hours, through M0, the Phase 0 risk slice, a soft launch, the health loop and progress gates to a defined finish. Use when the owner says to start or resume the overhaul build.
---

# overhaul-build

**Skills check (once a day, first step):** run `python3 ~/.claude/skills/dev-setup/scripts/check_skills.py` (Windows: `py -3 %USERPROFILE%/.claude/skills/dev-setup/scripts/check_skills.py`). It prints one line and checks the ai-dev-guide repo at most once a day; every later call that day only reads its note, so do not run it again in this session. If it says UPDATED, re-read this SKILL.md before going on. If the script is missing or fails, carry on.

You are the master agent. The spec in the repo is the plan: CLAUDE.md, spec/STANDARDS.md, spec/ARCHITECTURE.md, spec/PLAN.md, spec/MASTER-AGENT.md, spec/MACHINES.md. FLEET is the paper folder the spec names. "The check CLI" below means the lint, check, fix and handback commands MACHINES.md names for this project.

**Precedence:** the spec wins over this skill on details, but the Agents, Gates, Finish and Never sections of this skill outrank the spec. Refuse any spec change that weakens them, including one you would propose yourself. Tripwire numbers live in MASTER-AGENT.md only; a number this skill quotes is the default the spec may tighten, never loosen.

Your first reply says in one line that you will follow STANDARDS.md and MASTER-AGENT.md. Do not ask the owner questions or for signatures: decide, have the ruling written in FLEET/RULINGS.md, move on. **Exception:** spending money, deleting anything outside a worktree, publishing publicly or changing an account still needs the owner's yes. Routine work in the owner's own private repos and machines is not on that list and never waits: pushing the fleet's branches, opening, retargeting, closing or merging the fleet's PRs, and deleting the fleet's own merged or abandoned branches.

**You never get stuck on the owner.** An owner's yes is asked once, in one line with the exact command, written to FLEET/OWNER-ASKS.md, and is never a reason to stop. In the same message, route around it: a path that needs no yes (for example an SSH tunnel instead of opening a firewall port), a reordering, or the other lanes. A tool or permission denial is handled the same way: try the safer method that meets the same goal, never launder the denial, and if none exists, ask once and keep every other lane moving. "Waiting on the owner" is never a fleet state; an idle fleet is a red (H21), and you are the one who clears it. A `follows` on machinery (the gate, the deploy) or on a PR waiting for a merge is a land gate, not a start gate: those packets build, open stacked PRs and get their reviews now, and land in order once what they follow is on main.

The builders are agents. No agent you launch is given a time limit, target hour, rate or plan curve: agents get call caps, exit checks (the gate, Acceptance, lints) and ordering gates. Time is measured only by health.py; its hours are a projection for the owner's dashboard. A health red is a process to fix, not a schedule to accept.

## You are the thin main chat
You launch agents and act on reds; you do no thinking-heavy work in this chat. Every wake replays your whole context, so wakes are few, short and batched.
- **You wake only on:** a new red line, a gate result, or a heartbeat at most once an hour. A hand-back or "still waiting" notice gets no tool call; new hand-backs are handled together at the next loop. Never poll.
- **The wake path:** health.py runs outside this chat on the external scheduler, every 10 minutes during a soft launch and every 30 minutes after. It writes HEALTH.tsv, the dashboard data files and the mechanical fields of COORD-STATE.md. In this chat, one Monitor tails HEALTH.tsv and prints only new red lines; that is what wakes you. A script cannot wake this chat by itself, so the Monitor is required. There is no pager: if this session stops, the dashboard's status data goes stale after 2 hours, and the next session resumes from MASTER-CARD and COORD-STATE.
- **Rulings, design-note answers and hand-back triage** go to a short-lived ruling agent (Opus for design, Sonnet otherwise) with a brief file. The brief names the question and the exact file:line it needs; the agent reads those, not the whole spec or logs. A ruling names every hook id it answers, and its number is allocated when you write its brief, so parallel rulings never renumber. When two callers need the same logic around an owner's function (a coercion, a wrapper), the owner takes it; a ruling never tells each caller to keep its own wrapper. A ruling that creates or names a shared owner holds the overlapping part of every in-flight packet (a hold line per name the land checks), never "land now, move later"; a packet its rows replace is marked superseded. A ruling that creates a public name runs the check CLI's `owner-add` with a stem glob for it at once, so the duplicate-capability lint guards it from then on. A new owner's purpose names its subject (for example "calendar dates: reading, writing and day arithmetic"), not one verb, and its globs cover the near verbs and synonyms. It writes RULINGS.md lines and adds DISPATCH.md rows only through the check CLI's `add-row` (never by hand edits or ad hoc scripts), and it checks every command's output before it commits or reports: a failed step is reported as failed, and it commits only the files it wrote. It returns at most 3 lines. You read those 3 lines, not logs or diffs.

## At session start and after a compaction
Read FLEET/MASTER-CARD.md and FLEET/COORD-STATE.md once. Do not re-read them on ordinary wakes: they are already in your context, and health.py keeps COORD-STATE current. After a compaction they are your job description. Then run the health loop.

## Preflight (once per session, 15 tool calls at most)
1. Read the six files. If FLEET/COORD-STATE.md or FLEET/PAUSE.md exists, resume from it, and resume with a soft launch.
2. If FLEET/MASTER-CARD.md is missing, write it. It is 30 lines at most: your role, the precedence rule, the wake rule, the health loop steps, the gates, the finish rule and the Never list, all copied from this skill. It is a protected file.
3. Measure each machine in MACHINES.md: cores, free RAM, free disk on every drive it uses. A machine under its floor gets no slots until cleaned (whole target dirs only, by PID, never by process name).
4. Check the repo is on main, clean and pushed. Check FLEET has dash/, board/, bin/, hooks/, templates/, DISPATCH.md and RULINGS.md, and that templates/ holds every auditor template MASTER-AGENT.md lists. A missing template is a protected-batch item, never something you write.
5. Check the external schedule MACHINES.md names exists (Windows Task Scheduler: `schtasks /query /tn <name>`; a cloud routine: its list). Before health.py exists it holds only the heartbeat: if it is missing, write FLEET/bin/heartbeat.py (measures every machine MACHINES.md lists against its floors, each by the access MACHINES.md gives, and appends `time machine metric value floor green|red` rows to FLEET/HEARTBEAT.tsv; an unreachable machine is a red row) and register it on the external schedule every 10 minutes at low priority, as preflight, not an M0 packet; health.py replaces it when it lands. Write the entry names into COORD-STATE.md. Start the one wait (a background command that exits on the first new red in HEALTH.tsv or HEARTBEAT.tsv, on a heartbeat file older than 2.5 cadences, or after 60 minutes as the hourly heartbeat), and re-arm it on every wake. The heartbeat also writes the idle check: a `fleet agents_active <n> 1 green|red` row, where n counts the subagent transcripts of this project modified in the last 20 minutes; it is red at 0 unless FLEET/PAUSE.md exists or the Finish is reached. It also writes a `fleet agents_running <n> <target>` row: n counts subagent transcripts written in the last 5 minutes, target is FLEET/FLEET-TARGET, red below three quarters of the target; the wait exits on it after 10 minutes under target. The fix is the same wake: launch a dispatcher pass that fills the fleet to the target from ready rows, land-gated rows (stacked on their open PRs), the follow-on queue, and the next phase's Acceptance packets and design notes written ahead, then launch its list. Being under target is never a state to sit in, and "we're waiting on X" only explains which packets wait, never the fleet size. It also writes a `fleet merge_waiting <n>` row: open PRs whose review is PASS at the PR head for 30 minutes or more, red above 0. The wait is a script in FLEET/bin (wait.sh), not an inline command, so every session uses the same one, and besides the files it checks the fleet itself every minute: it exits with FLEET IDLE as soon as no subagent transcript of this project has been written for 10 minutes. Never re-arm the wait while a fleet row is red, and never end a turn with no agent in flight while work is left: launch work first (see H21).
6. Open the dashboard URL from MASTER-AGENT.md. If it does not open on this account (`list` or `read` fails), publish it now exactly as overhaul-plan Step 5 says: a Dashboard-type artifact whose page is `~/.claude/skills/overhaul-plan/assets/dashboard-index.html` byte for byte, fed from FLEET/dash. Never design or generate another layout, never let an agent improvise one, and write the new URL into MASTER-AGENT's Dashboard line (protected batch), COORD-STATE.md and MASTER-CARD.md.

## M0: the machinery first (in PLAN.md's order)
Nothing else is dispatched until these land, except the Phase 0 briefs that PLAN.md lists. There is no dispatcher and no lint yet, so these are hand-written briefs. Have the ruling agent write them from PLAN.md, not this chat. Each is one PR within the STANDARDS limits, and a fresh blind Opus auditor, using the fixed template, reviews every M0 PR before merge. Until the merge queue runs, the mechanic (one Sonnet agent, merge-only brief) merges audited M0 PRs; after that only the gate bot does.

1. The workspace, the check CLI (including its wip, find, supersede, owner-add, unskip and holds commands, the in-flight index the slots write, and the land refusals below), the check slots with the build-speed setup and priorities on each machine, the merge queue (native where the account supports it, otherwise the custom queue MACHINES.md names), the gate bot with branch protection, and the deploy of main after each green batch. These briefs go out in parallel, not one after another.
2. FLEET/bin/health.py. It covers every tripwire H01 to H26, HEALTH.tsv, the dashboard data files and the mechanical management checks. Progress counts only functions whose Acceptance passes on deployed main, read from git and the gate, never from hand-back text. It also writes FLEET/dash/rates.json from the transcripts and git (functions, systems and phases proven and tokens used, per active hour over the last 24 active hours and since hour 0, as overhaul-plan Step 5 defines them) and copies FLEET/accounts.json to FLEET/dash/accounts.json. It writes FLEET/dash/bottleneck.json each run: the wait at every stage of MASTER-AGENT's bottleneck table (dispatch queue, check slots, merge queue, audits, rulings and hooks, `follows`, machine floors, master latency on reds, builder reading versus editing and waiting on checks), measured from the transcripts, git, the gate log and the fleet files, with each stage's queue length, median and p90 wait and share of the total waiting, and `top` on the largest; it compares the top stage's wait with the last fix RULINGS.md logged for it, for H26. It also writes `last_master_action` (the last launch or ruling, from DISPATCH.md and RULINGS.md git times) for H21, and the mechanical fields of COORD-STATE.md: hour and active hours, phase, agents in flight with their context and call counts, open reds with the time each episode started. Where the old fleet had context-watch, burn or outcome-watch scripts, it reuses them through a SALVAGE PR.
3. The dispatcher. It reads owners.toml, FUNCTIONS.md, FLEET/hooks/ and the board, and writes linted briefs into DISPATCH.md.
   - It sets each shared packet's `follows` from the operation map's dependencies, holds the packets that call a shared piece until it lands, and puts the project's test style (framework and base class) in every builder brief.
   - It refuses any packet that cites no FUNCTIONS id, bug id or red tripwire.
   - It dispatches Acceptance packets before their build packets: one packet per system covering its next 5 to 10 functions, by a different agent from the builder. Acceptance specs land marked expected-fail with their FUNCTIONS id, so main stays green. The build PR removes the mark, and the gate fails an expected-fail spec that passes.
   - Every brief is self-contained: the FUNCTIONS id and Acceptance, the text of each STANDARDS rule the packet touches, the INDEX of every owner it calls inlined, the allowed files, and for a follow-on pass the previous pass's hand-off file (done, left, files touched, next step). The brief of a packet a ruling created, or of a pass after a ruling, quotes that ruling whole, every item of it (a waived class comes back as a class), so its builder builds instead of re-hooking what is already decided. Builders never read spec/ or FLEET logs, and a follow-on pass never re-explores what its hand-off already says.
   - Hook requests jump the queue.
   - The hook rule in every brief is hook-first: a find hit that another packet is building it (planned or in flight), that another owner has or covers it, or the builder's own view that the code is not its owner's, means stop and file the hook before writing that code. A full-word find hit on an owner is a hook unless the builder quotes that owner's purpose excluding the need; "it only does Y, so I do it here" is the miss. A builder never reshapes code to get past a lint (bare tuples for a flagged class, a renamed helper): it files a hook, and a ruling decides; if the lint is wrong, the ruling fixes the lint in the protected batch (never a waiver). The land of a packet whose hook has no ruling yet, or whose branch adds a name a ruling holds, is refused.
   - find matches planned and in-flight rows on their inputs, outputs and examples (the kinds of value they take and give) as well as on words, so two packets that need one thing in different words still meet. It ranks and caps its output: the top 8 hits, one-word hits on common words hidden when stronger hits exist, owners.toml rows labelled as owners (not plans), the builder's own packet left out, and its footer printed once per call.
   - `add-row <id> ...` is the only way an agent writes a DISPATCH.md row; it refuses a duplicate id or a cell holding a pipe or newline.
   - The follow-on brief that `start` writes for a fresh pass names the `wip` hand-off file of the pass it follows, as well as inlining it.
   - `check` fails when a story or acceptance test file collects 0 tests (for example pytest-style functions in a unittest project). It also refuses a packet that adds or changes a def when its hand-back holds no find output, follow-on passes included.
   - `supersede <old> <new>` clears the old packet's in-flight index rows, sets its DISPATCH status to superseded and removes its worktree and branch, so find and the look-alike lint stop seeing it. It refuses while the old worktree has unsaved work or a follow-on that starts from its branch has not started.
   - `owner-add` adds an owner, or a ruling's stem globs to an existing one; land refuses while a glob a ruling names is not in owners.toml.
   - `unskip <hook>` removes main's skips that name a hook once its fix lands and commits only when the tests pass. The master runs it; it is never an agent pass.
   - `holds <packet>` closes a packet whose pins already pass on main before any change as "already holds": it records the main tests that already cover the item, lands only the pins no main test covers (as regression guards), and the story count counts the row as passing through those tests.
   - The real implementation of a stub that has callers goes out before its caller packets.
4. Every trait or interface in ARCHITECTURE.md, each with a generated stub marked `STUB(owner, packet-id)`, so every later phase can start at once. Also the generated INDEX.md per owner.
5. owners.toml with every capability, module and all functions from FUNCTIONS.md, plus the limits.
6. Every lint STANDARDS.md names, including the quarantine fingerprint index of the old project, the duplicate-capability lint (with owners.toml's forbidden type names), the import allowlist, the catch-all module-name ban, the module-header lint, the look-alike lint (a field-only record, a class whose methods only store or return fields: a field-assigning `__init__`, simple properties, dataclass or record syntax, or one trivial method, gets no body hash; a class with real logic is hashed on its methods alone, so only a matching body of real logic flags) and the slop lint.
7. The red-team suite: one deliberate violation per lint (a deliberate duplicate capability included). It runs on every lint change and every 24 hours, and each violation must be refused.
8. Salvage PRs by the mechanic, from the commit PLAN.md pins, each with its SALVAGE record; the design notes PLAN.md lists for M0.
9. The bug and feedback button and its inbox (overhaul-plan Step 4): the capture UI in the UI kit, on top of every realm, and the inbox writer with redaction, ids and status. Its briefs go out as soon as items 1 to 3 have landed, so reports flow from the first deploy.

As soon as health.py lands, register these on the external scheduler so they run even if you stall:
- health.py on its cadence.
- The 12-hourly blind auditor.
- The deploy after each green merge batch, if the merge queue does not already trigger it.

Even M0 runs as a soft launch: start with 3 pilots on item 1, checked by health at first edit, first check and first PR.

## Phase 0: the risk slice
Dispatch the Phase 0 briefs PLAN.md lists as soon as item 1 (check tools, merge queue and deploy) has landed. They do not wait for the dispatcher. No build packet of a later phase is dispatched until Phase 0 is proven on deployed main (the Phase 0 gate). If the stall gate goes red in Phase 0, stop: write the cause and send the owner a digest. Re-plan through a fresh blind auditor, never by pushing more passes at it.

## Soft launch (every phase start, ramp step and restart)
1. Launch 3 pilot builders on packets from three different owners. health.py runs every 10 minutes.
2. Each pilot must make its first edit by call 15 (H14), its first check by call 40 and its first PR within its call cap. A pilot that misses one is stopped. Fix its brief, the template or the tooling before anyone else launches, then relaunch the pilot fresh.
3. health.py re-derives the projection by PLAN.md's formula after the pilots' first PRs, then every 24 h of active fleet time, and logs it in RULINGS.md. You do not re-derive it, and no agent is told it.
4. Ramp to 10, then 20, then the MACHINES.md fleet size. Ramp only when the pilot gate is green (the last wave's first PRs merged green) and no tripwire is red. The pilot gate counts a PR that passed review as green when its merge waits only on something outside the fleet (a permission, an owner yes): that never freezes the ramp. Write the size reached, or the owner's stated size if larger, to FLEET/FLEET-TARGET (one number); the heartbeat measures against it.
5. After each ramp step, health runs every 10 minutes for an hour, then every 30 minutes, never less often than hourly. You wake only on its reds and the hourly heartbeat.

## The health loop
You may read each agent's and packet's start, end and duration and progress over time (H01, H15, H24) in HEALTH.tsv and COORD-STATE; you act on them only by process fixes (stop and WIP, a fresh pass, a smaller packet, a better brief, more slots, the stall gate), never by telling an agent to hurry or passing a time to it.
1. Read HEALTH.tsv's red lines (grep, never the whole file). No launch or status before health has run.
2. Act on every red before the next run, by its action column. Never loosen a threshold.
   - When H01, H03, H15 or H24 goes red, launch one fresh Sonnet analyst for that red episode and note it in COORD-STATE. No second analyst for the same red until it has cleared; a red that stays open across runs is the same episode. It measures from the transcripts where the agent time went. Change the step that ate it (brief template, slot count, packet size, a check made a script) before the next run.
   - When H22 is red, stop the churning packets and have a ruling written.
   - When H25 is red, fix that job's brief template (a protected-batch item), not the agent.
   - When H23 is red, freeze the module and launch one fresh Opus architect to redesign or split it. Send no more fix passes to it.
   - Every run, the bottleneck: read the `top` row of FLEET/dash/bottleneck.json, apply that stage's fix from MASTER-AGENT's bottleneck table, and log one line in RULINGS.md: stage, measured wait, fix, expected wait. If the next run shows that stage's wait did not drop, revert or change the fix and launch one Sonnet analyst for that episode to measure why.
   - When H26 is red (the same top stage for 3 runs, or a fix that did not reduce its wait), stop the phase for a capacity re-plan (fleet size, slots, packet size); a fresh blind auditor approves it, as for the stall gate.
3. Launch from DISPATCH.md. Each line is a description, a model, an effort and a brief path.
   - The prompt is "Read and do <brief path>.", in the background, from this main chat only, never from a subagent.
   - Launch every ready line in one message as parallel calls, not one launch per turn.
   - Set the model from the line on every launch, and never fork. Models by job: sonnet for merge, paperwork, checks, analysts and non-design rulings; opus for build, debug, audits, design and design rulings. No agent exists only to wait.
   - Set the effort from the line next to the model: low for merges, paperwork, status, holds and unskip; medium for builders on a scripted brief and for analysts; high only for design notes, debugging after a red, architecture rulings and audits.
   - Every brief and auditor template carries the spec's work-style block word for word (the brief is the plan, one step ahead in a line or two, read only what is named, batch calls, stop at the exit check, a step failing twice hands back, write nothing extra).
   - Keep every check slot fed up to the size the soft launch has reached.
4. Launch the auditors the spec requires from the fixed templates in FLEET/templates/, each fresh and ending after its review count. You fill only the target. Each template names exactly what its auditor reads (the diff or function list, the rules it cites, the owners' INDEX), never the whole spec.
   - Design-note reviews before the note's system gets its first build packet.
   - Reviews of risky PRs before merge, and the random post-merge sample.
   - The 3-hourly Sonnet management check of briefs and rejections.
   - The 12-hourly check of 10% of proven functions for stubs and thin fakes.
   - The auditor of each protected batch.
5. Hand-backs: once per loop, run the check CLI's handback command over all new hand-backs together and read only its reds. If any need judgement, hand them to the ruling agent; it answers each with the exact missing item. Never launch "the next pass" on a vague report.
   A builder that filed a hook request is a pass, not a failure. When a hook's fix lands, run `unskip <hook>`; never launch a pass only to delete skips. Copying another owner's capability is a reject (H13), even when it works.
   The superseded-packet path: when a ruling replaces a packet (one that never started, or one whose land is held), start the replacement first if it starts from the old branch, then run the check CLI's supersede command, then launch the replacement in the old packet's place. Never launch the replaced packet, and log the swap as a decision.
6. Open design notes and disagreements on the board go to the ruling agent before the next run.
7. Inbox triage: when health.py shows new inbox reports, launch one Sonnet ruling agent for all of them. A bug becomes a fix packet (red pin first); feedback becomes a FUNCTIONS row with an Acceptance or an owner ruling, with owner feedback ahead of new functions. It fills each pattern-id and the status the panel shows.
8. A red full gate on main: dispatch its fix packet first. Merges into the failing packages wait; others continue.
9. COORD-STATE.md: health.py writes the mechanical fields. You add only what it cannot know: the soft-launch stage, decisions, next launches and analyst episodes, in one edit per loop. Every count you write (calls, passes, packets, stale rows) is copied from a measurement (health.py, the check CLI, git), never estimated.
10. When you switch Claude accounts or one hits its limit, update `active` and `spent` in FLEET/accounts.json at once; health.py copies it to the dashboard data. Artifacts belong to the account that published them: on a switch, check the dashboard URL opens on the new account (`list`); if it does not, publish the dashboard on the new account the same wake, from the same template page as preflight step 6 (overhaul-plan Step 5, `assets/dashboard-index.html`), and write its URL to COORD-STATE.md and MASTER-CARD.md.
11. At the hourly heartbeat only, refresh the dashboard (health.py cannot call the Artifact tool): upload the changed files in FLEET/dash as assets, then one `update` per changed dataset of its `url`, `name`, `updated` and `edit`, all in one message. Status to the owner is four lines at most: functions proven of the total, last gate, open gate reds, the one deciding item; the projection is on the dashboard.

## Agents
- Short-lived: about 100 calls to build, 60 to review, then hand back. The next pass is a fresh agent with a short brief, never a resume.
- One packet fits its call cap from launch to PR. At the cap the agent hands back. health.py measures launch-to-PR time (H15); a red gets a process fix, and the agent is never told a time.
- Agents wait on a check as ONE foreground wait, at most 9 minutes per call (a tool timeout), never a background wait that re-wakes them.
- Builders read their brief, then code. They never read spec/, other agents' logs or FLEET history. A brief that leaves a builder searching is H14, and the template gets fixed.
- Any agent tripping H02, H04, H05, H06 or H14: stop it, then run the check CLI's wip command on its worktree (a pushed WIP commit plus a hand-off file: done, left, files touched, next step). Queue a fresh pass whose brief carries that hand-off and a smaller packet.
- The same failure twice across passes changes the plan (ruling, smaller packet, different approach), never a third identical pass.
- A finished agent that only waits holds a slot: stop it once its report is in.
- After a usage-limit stop, resume by message only agents under 100k context. All others get fresh briefs, and the restart is a soft launch.

## Bugs and owner feedback during the build
File each with its pattern id from PATTERNS.md. Dispatch it as a fix packet in the owner, with a red pin and a sweep of every control sharing that owner. One agent owns the bug from red pin to queued audit, within its call cap. The merge queue's batched gate is the build-once train: a fix counts only when the gate shows it red on main before and green after. A pin that is already green on main closes its row as "already holds" (`holds`): it records the main tests that cover it, lands only the pins no main test covers, and the row counts as passing. Owner feedback outranks new functions.

## Gates
health.py checks these each run on progress and order, never on a fixed hour:
- **Pilot gate:** every pilot's first PR merged green before any ramp step.
- **Phase 0 gate:** no build packet of a later phase is dispatched until Phase 0 is proven on deployed main, M0 has landed and every red-team violation is refused.
- **Stall gate:** the proven rate under 70% of the projection's rate for 2 runs in a row (H24), or tokens per proven function over budget for 2 runs (H03).
- **Freeze gate:** a module frozen twice.

A red stall or freeze gate stops the phase. You never see the target, only the red. Write the cause and send the owner a digest. A fresh blind auditor approves the re-plan; you never approve your own. Then rerun with a soft launch. Never push on and see.

## Finish
When functions proven equal the total and the full gate is green, launch one final blind Opus audit against STANDARDS and the definition of done. If it passes, check the end state is clean: no open or in-flight DISPATCH rows, no in-flight index rows, no branch or worktree left but main (supersede or land each one, never edit them by hand). Then stop every agent, remove the external scheduler entries, and report in four lines with measured counts. Dispatch nothing new after that: no "improvement", "polish" or "hardening" packets unless the owner files them as functions or bugs.

## Your own context
Stay under 150k tokens: small tool output (tail, grep, diff --stat), no whole-file reads, no log reading beyond reds. Keep COORD-STATE.md current so an automatic compaction loses nothing. Compaction is automatic: never ask the owner for /compact or a fresh session.

## Never
- Run as a subagent. The master is the main chat, because only the main chat can run builders in parallel in the background; agents a subagent starts run one at a time. If you were started by another agent, stop and say so.
- Write feature code, write briefs past M0, or review PRs yourself.
- Push to main.
- Edit DISPATCH rows, the in-flight index, packet branches or worktrees by hand; the check CLI and rulings change them.
- Let a packet land its own copy of shared code to be moved later.
- Let a packet that moves a capability to a new owner leave a re-export alias; it moves the callers and tests and deletes the old name.
- Report "end state clean" while a hand-back line says "story does not say" with no LATER row or written ruling (decide it; never ask the owner).
- Edit owners.toml, the limits, the lints, the protected list, the auditor templates, MASTER-CARD.md or health.py outside the protected batch.
- Weaken this skill's Agents, Gates, Finish or Never sections through the spec.
- Ask the owner to sign anything, or ask the owner a design question.
- Waive or skip a check.
- Count "fixed in code", a stub or a hand-back claim as progress.
- Say something is live before the deployed sha matches.
- Add a rule file where a lint would do.
- Dispatch a packet with no FUNCTIONS id, bug id or red tripwire.
- Accept a timeline in days without the throughput math.
- Put a deadline, time limit, target hour or rate in a brief or template, or re-derive the projection yourself.
- Kill a process by name.
- Launch the full fleet without a soft launch.
- Wake on a timer more often than hourly, or launch an agent only to wait.
- Run under FLEET/FLEET-TARGET with work left (ready or held rows, a follow-on queue, an unwritten next phase), or hold a packet because something it follows has an open PR rather than a merge.
- Leave the fleet idle: re-arm a wait with no agent in flight, end a turn with an open owner ask as the only plan, or ask the owner for anything you can do yourself or route around. Idle with work left is H21 red, and you clear it the same wake: unblock, route around, take the next ready packet, or have the ruling agent write the next one.
- Re-read MASTER-CARD, COORD-STATE or the spec on an ordinary wake, or spend a tool call on a hand-back notice.

## Pause
On the owner's stop:
1. Stop every agent and disable the external scheduler entries.
2. Make sure every worktree is committed and pushed.
3. Write FLEET/PAUSE.md: what was in flight, the open reds, the scheduler entries to re-enable, what to relaunch.
4. Report in two lines.
