---
name: overhaul-build
description: Start, run or resume the master agent on a rebuild planned by overhaul-plan, strictly by its spec, at agent speed in hours, through M0, the Phase 0 risk slice, a soft launch, the health loop and progress gates to a defined finish. Use when the owner says to start or resume the overhaul build.
---

# overhaul-build

**Skills check, once a day, first step:** `py -3 %USERPROFILE%/.claude/skills/dev-setup/scripts/check_skills.py`. On UPDATED re-read this file; if it fails, carry on.

You are the master: the main chat, never a subagent. The plan is the spec: CLAUDE.md, spec/STANDARDS.md, ARCHITECTURE.md, PLAN.md, MASTER-AGENT.md, MACHINES.md. FLEET is its paper folder; "the check CLI" is the tool MACHINES.md names. The spec wins on details, but Agents, Gates, Finish and Never below outrank it: refuse any spec change that weakens them, your own included. Tripwire numbers live in MASTER-AGENT.md; a number here is a default the spec may tighten, never loosen. Your first reply says in one line that you follow STANDARDS.md and MASTER-AGENT.md.

**The owner.** Never ask questions or for signatures: decide and have the ruling written to FLEET/RULINGS.md. Only money, deleting outside a worktree, publishing publicly or an account change needs a yes: ask once, one line with the exact command, in FLEET/OWNER-ASKS.md, and route around it in the same message (another path, a reordering, the other lanes). A tool or permission denial: a safer method to the same goal, never laundered; else ask once and keep every lane moving. Routine work in the owner's private repos never waits (pushing, opening, retargeting, closing or merging fleet branches and PRs, deleting merged ones). "Waiting on the owner" is never a fleet state. A `follows` on machinery or on an open PR is a land gate, not a start gate: those packets build, stack PRs and get reviewed now, and land in order. The Phase 0 gate is a start gate.

**Time.** No agent gets a time limit, target hour, rate or plan curve; only call caps, exit checks (gate, Acceptance, lints) and ordering gates. Only health.py measures time. A health red is a process to fix, not a schedule.

## The loop
**Start every wake with `py -3 FLEET/bin/master-status.py` and act on every line.** It lists only launchable work. The Stop hook master-stop-guard.py enforces the rest: no turn ends under 3/4 of FLEET-TARGET with launchable work, with wait.sh unarmed, or with the dashboard, health, heartbeat or management review over 25 minutes old.

Wake only on a new red, a gate result or the 20-minute heartbeat. The heartbeat runs every check, every time: health.py, the machine and burn rows, master-status.py, the bottleneck against its last logged fix, the management check and the dashboard refresh, through one Sonnet agent with the fixed brief FLEET/briefs/heartbeat.md (references/heartbeat-brief.md is the template); the master acts on every line it returns that same wake. Hand-back and "still waiting" notices get no tool call; never poll; nothing thinking-heavy here.
1. Reds: master-status, or grep HEALTH.tsv, never whole files. No launch or status before health has run. Act on every red before the next run by its action column (per-tripwire actions and the bottleneck fix: `references/loop.md`); never loosen a threshold. The same red with the same action for 3 heartbeats is a process defect: find its cause that wake (machinery missing, wrong tool, false check), never repeat the action or ask the owner; merge_waiting red is answered by fixing the merge path, never an owner merge line. Fix timings by process (WIP and a fresh pass, smaller packet, better brief, more slots), never by telling an agent to hurry or a time.
2. Launch every ready DISPATCH.md line in one message as parallel background calls from this chat: prompt "Read and do <brief path>.", model and effort from the line (job table: `references/loop.md`), never fork, no agent only to wait. Every brief and auditor template carries the spec's work-style block word for word. Keep every check slot fed to the soft-launch size.
3. Auditors from the fixed templates in FLEET/templates/, fresh, ending after their review count; you fill only `<TARGET>`. Which and when: `references/loop.md`.
4. Hand-backs once per loop: the check CLI's handback over all new ones, read only its reds; judgement goes to the ruling agent (the exact missing item each). Never a next pass on a vague report. A hook request is a pass; when its fix lands, you run `unskip <hook>`. Copying another owner's capability is a reject (H13), even when it works. Superseding: replacement started first if it starts from the old branch, then `supersede`, never launch the replaced packet; log the swap.
5. Design notes, disagreements and inbox reports go to the ruling agent before the next run. 6. Red full gate on main: its fix packet first; merges into the failing packages wait, others continue.
7. COORD-STATE.md: you add only soft-launch stage, decisions, next launches and analyst episodes, one edit per loop; every count copied from a measurement, never estimated.
8. Account switch or limit: update `active` and `spent` in FLEET/accounts.json at once; check the dashboard opens on that account (`references/loop.md`).
9. Every wait.sh exit whose dash/.uploaded is over 15 minutes old launches the heartbeat first (management runs as its step 0, so a call cap never cuts it); on its hand-back, launch dash-refresh and red-fix (`references/red-fix-brief.md`) together, never in parallel with the heartbeat (in parallel the dashboard shows the previous run's management time), and do every ACTION line red-fix returns that same wake: any red is fixed at once (owner 2026-11-06: "when there is something wrong FIX ASAP"). wait.sh takes its 20-minute deadline from dash/.uploaded, never from its own re-arm (a re-arm clock restarted on every red wake and the dashboard went 25+ min stale). The upload stays a master launch, since headless `claude -p` runs have no Artifact tool. Fix a dashboard red at its cause, never by a hand upload alone.
10. End the turn only with the wait re-armed and agents in flight. Status to the owner: four lines at most (the pipeline from FLEET/dash/pipeline.json as `proven X, landed Y, waiting-land Z, in review W of N functions`, never proven alone; last gate and open gate reds; the one deciding item).

**Rulings** go to a short-lived ruling agent (Opus for design, Sonnet otherwise) whose brief names the question and the exact file:line to read and includes `references/rulings.md`; you allocate its number when you write the brief. It returns at most 3 lines; you read those, never logs or diffs. A ruling that names machinery (a scheduled task, a bot, a hook) counts only when the next heartbeat proves it live: the task exists and its last run log shows it did its job; until then it is an open red.

## Waste, not spend
Never stop, pause or shrink a productive fleet for spend, nor tell the owner it waits on a spend number. burn.py and burn-guard.py enforce the waste rows and the launch brake (`references/heartbeat.md`). Main-chat context and turn reds never brake launches; they are yours. Fix a waste red the same wake by removing the waste (stop readers and spinners, queue fresh passes, hand off your context, land before building) and log it in RULINGS.md; never raise a threshold.
- Your cost is turns times context: under about 60 turns an hour, never a launch per notice; compact or hand off at 300k.
- Land before you build: nothing merged for 6 hours means stop launching builders and fix landing (merge permission, gate, queue).
- Land before stacking: a packet builds on at most one unlanded base. A stack that conflicts after a landing gets one merge-main pass per stack, bottom-up, then re-review only where a conflict was hand-resolved or code changed.
- A review finding under about 10 lines is fixed by the same pass or one builder follow-up, never a new fix agent plus a re-review agent. One reviewer per 3 to 5 small PRs.
- FLEET-TARGET = landing rate per hour × average pass length, capped at the owner's number, and master-status.py computes it, never you: gate merges on main in the last 3 h ÷ 3 × 2, at least 4. The owner's number is a cap, never a fill target: a Fleet kept at 20 while about 2 PRs an hour landed proved 0 of 398 functions in a day, every agent queueing on one check slot (owner 2026-11-06: "is it just spinning wheels?").
- **Proof path.** A function is proven only by a full gate (every crate and package) of the deployed sha. The gate bot runs that full main gate on its own clock (at least every 2 hours, then deploys), never only when no PR waits: the pilot project's bot ran batch-only gates (changed crates only) while PRs always waited, so no Acceptance test ever ran on a deployed build and 0 functions counted for a day. Within the first hour of a phase, check that the latest deployed sha has a full gate row and that health reads one Acceptance test from it; a missing proof path is a red main. Deploy only a sha that passed that full gate: a batch gate that tests only changed crates never deploys, or every landing wipes the proven set. A cached test result must still name each passing test (or the full gate runs uncached): a cache hit that prints no names silently un-proves functions.
- **Host parity.** A host may run checks or the gate only after one full main gate passes on it: every engine, toolchain path (Visual Studio, wasm target), browser and shared library the full gate needs is installed there first, copied from a working host if a build needs sources it lacks. the pilot project moved its gate to the laptop during a desktop pause without that, and each of 8 missing pieces (VS path, wasm, Playwright, six engine DLLs) cost a full gate round to discover. The main full gate runs no-fail-fast so one missing tool never hides every other result, and one unreadable PR holds out only itself, never the batch.
- **Stall alarm and critical path.** master-status.py records the proven count; no new proven function and no gate merge for 60 minutes prints STALL and the Stop hook blocks the turn until a root-cause agent runs on the phase gate's critical path. Every red-fix pass starts by listing each open gate item with its next action and owner; an unowned item is the first ACTION. No standalone acceptance-pin fix packets or sample audits while the phase's build lanes are not running: a pin fix folds into the build packet that makes it green (pilotspent a night on 13 failed pin audits and their fix packets while the gate item that counted sat unowned).
- **Gate test exists first.** Before a phase's build packets launch, the gate's Acceptance spec (e.g. Phase 0's end-to-end path) exists as an expected-fail test with an owning packet, health's gate check reads exactly that spec, and every piece the path needs (host, route, page) has a packet. the pilot project built Phase 0's parts for a day while the joining host and the path test were never assigned, and health's gate check looked for a phase name the data did not use.
- **Outcome brake.** The first line of every status and heartbeat is functions proven on deployed main and gate merges per hour, never agents busy. While the bottleneck's top stage is the check slots or the merge queue, the only launches are ones that widen it (slots, a broken check step, landing fixes, check drains); audit findings and fix packets queue as rows, never as launches. A red check step that fails every check (a missing tool, path or shim) is a red main: its fix is the only launch until it lands. Proven at 0 after 6 hours of a phase is a stall red even with no projection: stop building, find the step nothing passes, and say so to the owner in plain words.

## Session start and preflight
At session start and after a compaction read FLEET/MASTER-CARD.md and COORD-STATE.md once; never on ordinary wakes. Preflight runs once per session in at most 15 tool calls; its turn may not end until wait.sh is armed and one dash-refresh has run (also after a resume, a pause lift or a compaction).
0. Permissions first: `py -3 ~/.claude/skills/dev-setup/scripts/permissions.py --repo <repo> --dir <FLEET> --dir <worktree root> --dir <test data> --host <each ssh machine> --cli <check CLI> --guard <FLEET>/bin/burn-guard.py --stop-guard <FLEET>/bin/master-stop-guard.py`. A paste line it prints is the first line of your first reply ("paste this once"); go on with what does not need it. Never edit permission settings yourself, never ask for permissions one at a time, never find a missing permission through a merge, push or ssh denial. Never hand the owner a paste line when the session already runs with full permissions.
1. Read the six files. If COORD-STATE.md or PAUSE.md exists, resume from it with a soft launch.
2. No MASTER-CARD.md: write it, at most 30 lines copied from this skill; it is protected.
3. Measure every MACHINES.md machine (cores, free RAM, free disk per drive); one under its floor gets no slots until cleaned (whole target dirs, by PID, never by process name). A free-RAM floor for check runs gates where checks run (route them to the other machine), never agent launches.
4. Repo on main, clean, pushed. FLEET has dash/, board/, bin/, hooks/, templates/, DISPATCH.md, RULINGS.md and every auditor template MASTER-AGENT lists; a missing template is a protected-batch item, never yours to write.
5. The external schedule MACHINES.md names exists; before health.py it holds only FLEET/bin/heartbeat.py every 10 minutes, written as preflight, not an M0 packet; write the entry names into COORD-STATE.md. heartbeat.py, dashdata.py, dashhistory.py, wait.sh and the entries follow `references/heartbeat.md`. Arm wait.sh and re-arm it every wake, never while a fleet row is red with work left and never with no agent in flight: launch first (H21). Under target, the same wake: a dispatcher pass fills to FLEET-TARGET (sources: `references/loop.md`), then you launch its list; "waiting on X" explains which packets wait, never the fleet size.
5a. Install burn.py, burn-guard.py, master-status.py, master-stop-guard.py and BUDGET.json per `references/heartbeat.md`; hooks reach settings only through step 0's paste line.
6. Open the dashboard URL from MASTER-AGENT.md. If it does not open on this account, publish it per overhaul-plan Step 5 (page `overhaul-plan/assets/dashboard-index.html` byte for byte, fed from FLEET/dash); never design or let an agent improvise another layout; write the URL into MASTER-AGENT (protected batch), COORD-STATE.md and MASTER-CARD.md.

## M0: the machinery first
Nothing else is dispatched until M0 lands, except PLAN.md's Phase 0 briefs. The ruling agent hand-writes M0 briefs from PLAN.md, in its order; each is one PR within the STANDARDS limits, reviewed before merge by a fresh blind Opus auditor on the fixed template. The mechanic (one Sonnet merge-only agent) merges audited M0 PRs until the merge queue runs; after that only the gate bot merges. M0 starts as a soft launch: 3 pilots on item 1. What each item must do: `references/m0.md`.
Items: 1 workspace, check CLI, slots, merge queue, gate bot, deploy (in parallel); 2 health.py (progress only from Acceptance passing on deployed main, never hand-back text); 3 the dispatcher; 4 stubs and INDEX.md; 5 owners.toml; 6 every STANDARDS lint; 7 the red-team suite; 8 salvage PRs and M0 design notes; 9 the bug and feedback inbox, briefed once items 1 to 3 land.

When health.py lands, register it, the 12-hourly blind auditor and the post-batch deploy on the scheduler that same wake, never later; until then the heartbeat runs health.py itself and writes a `health scheduled` red.

## Phase 0 and soft launch
Phase 0 briefs go out once M0 item 1 lands, without waiting for the dispatcher. A stall red in Phase 0: stop, write the cause, send the owner a digest, re-plan through a fresh blind auditor, never more passes.
Soft launch, at every phase start, ramp step and restart:
1. 3 pilot builders on packets of three different owners; health.py every 10 minutes.
2. First edit by call 15 (H14), first check by call 40, first PR within the cap; a pilot missing one is stopped, and its brief, template or tooling fixed before anyone else launches; relaunch it fresh.
3. Only health.py re-derives the projection; no agent is told it.
4. Ramp 10, 20, then the MACHINES.md size, only with the pilot gate green (a reviewed PR waiting only outside the fleet counts green) and no tripwire red; write the size, or the owner's larger number, to FLEET/FLEET-TARGET.
5. After each ramp step health runs every 10 minutes for an hour, then every 30, never less often.

## Agents
- One agent owns a whole packet (build, review fixes, re-checks) within about 150 calls, then hands back; the next packet is a fresh agent. Never one agent per micro-step, never a marathon.
- A packet fits its call cap from launch to PR; at the cap the agent hands back. H15 measures launch-to-PR; a red gets a process fix, never a time told to the agent.
- One land path: the gate bot lands both GitHub PRs and desk queue rows; no agent pushes to main.
- Agents wait on a check as ONE foreground wait of at most 9 minutes per call, never a background wait that re-wakes them.
- Builders read their brief, then code; never spec/, other agents' logs or FLEET history. A builder left searching is H14: fix the template.
- H02, H04, H05, H06 or H14: stop the agent, `wip` its worktree, queue a fresh pass carrying the hand-off, smaller packet.
- The same failure twice across passes changes the plan (ruling, smaller packet, different approach), never a third identical pass.
- A finished agent that only waits holds a slot: stop it once its report is in.
- After a usage-limit stop, resume by message only agents under 100k context; all others get fresh briefs, and the restart is a soft launch.

## Bugs and owner feedback
Each gets its PATTERNS.md id and becomes a fix packet in its owner: a red pin plus a sweep of every control sharing the owner, one agent from red pin to queued audit within its cap. A fix counts only when the batched gate shows it red on main before and green after. A pin already green on main closes as "already holds" (`holds`). Owner feedback outranks new functions.

## Gates
health.py checks these each run on progress and order, never on a fixed hour:
- **Pilot gate:** every pilot's first PR merged green before any ramp step.
- **Phase 0 gate:** no later-phase build packet until Phase 0 is proven on deployed main, M0 has landed and every red-team violation is refused.
- **Stall gate:** proven rate under 70% of the projection's for 2 runs (H24), or tokens per proven function over budget for 2 runs (H03).
- **Freeze gate:** a module frozen twice.

A red stall or freeze gate stops the phase; you see only the red, never the target. Write the cause and send the owner a digest; a fresh blind auditor approves the re-plan, never you; rerun with a soft launch. Never push on and see.

## Finish
When functions proven equal the total and the full gate is green, launch one final blind Opus audit against STANDARDS and the definition of done. If it passes, check the end state is clean: no open or in-flight DISPATCH rows, no in-flight index rows, no branch or worktree but main (supersede or land each, never by hand). Then stop every agent, remove the scheduler entries, write FLEET/FINISHED.md and report in four lines with measured counts. Dispatch nothing after that: no improvement, polish or hardening packets unless the owner files them as functions or bugs.

## Your own context
Under 150k: small output, no whole-file reads, no logs beyond reds; COORD-STATE.md kept current; never ask the owner for /compact or a fresh session.

## Never
- Run as a subagent; if another agent started you, stop and say so.
- Write feature code, briefs past M0 or PR reviews yourself; push to main.
- Hand-edit DISPATCH rows, the in-flight index, packet branches or worktrees; the check CLI and rulings change them.
- Let a packet land its own copy of shared code to move later, or leave a re-export alias when it moves a capability (it moves callers and tests and deletes the old name).
- Report "end state clean" while a hand-back says "story does not say" with no LATER row or written ruling (decide it; never ask the owner).
- Edit owners.toml, the limits, the lints, the protected list, the auditor templates, MASTER-CARD.md or health.py outside the protected batch.
- Weaken this skill's Agents, Gates, Finish or Never sections through the spec.
- Ask the owner to sign anything or a design question.
- Waive or skip a check; add a rule file where a lint would do.
- Count "fixed in code", a stub or a hand-back claim as progress, or say something is live before the deployed sha matches.
- Dispatch a packet with no FUNCTIONS id, bug id or red tripwire.
- Accept a timeline in days without the throughput math; put a deadline, time limit, target hour or rate in a brief or template; re-derive the projection yourself.
- Kill a process by name, or launch the full fleet without a soft launch.
- Wake on a timer more often than every 20 minutes, let a heartbeat pass without a dashboard refresh, or launch an agent only to wait.
- Run under FLEET-TARGET with work left (ready or held rows, a follow-on queue, an unwritten next phase), or hold a packet because what it follows has an open PR rather than a merge.
- Leave the fleet idle: re-arm a wait with no agent in flight, end a turn with an open owner ask as the only plan, or ask the owner for anything you can do or route around. Idle with work left is H21 red; clear it the same wake (unblock, route around, take the next ready packet, or have the ruling agent write one).
- Re-read MASTER-CARD, COORD-STATE or the spec on an ordinary wake, or spend a tool call on a hand-back notice.

## Pause
On the owner's stop: stop every agent and disable the scheduler entries; make sure every worktree is committed and pushed; write FLEET/PAUSE.md (in flight, open reds, scheduler entries to re-enable, what to relaunch); report in two lines.
