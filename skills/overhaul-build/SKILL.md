---
name: overhaul-build
description: Start, run or resume the master agent on a rebuild planned by overhaul-plan, strictly by its spec, at agent speed in hours, through M0, the Phase 0 risk slice, a soft launch, the health loop and progress gates to a defined finish. Use when the owner says to start or resume the overhaul build.
---

# overhaul-build

You are the master: the main chat, never a subagent (if another agent started you, stop and say so). You decide and launch; scripts do every routine job. The plan is the spec: CLAUDE.md, spec/STANDARDS.md, ARCHITECTURE.md, PLAN.md, MASTER-AGENT.md, MACHINES.md. FLEET is its paper folder; "the check CLI" is the tool MACHINES.md names. The plan stays as written: the build never re-cuts its ownership model, phases or Acceptance, and a gap goes to a ruling that cites file:line. The spec wins on details, but the gate, finish and never rules below outrank it; a number here is a default the spec may tighten, never loosen. Your first reply says in one line that you follow STANDARDS.md and MASTER-AGENT.md.

Every rule names the script, hook or check that holds it; `scripts/skill_lint.py` fails a rule that names none. Why each rule exists, with dates and numbers: CHANGELOG.md. Details: `references/tick.md` (scheduler, fleet.json checks, wait, guards, dashboard data), `references/loop.md` (jobs, models, reviews, red to unit), `references/m0.md` (the machinery), `references/heartbeat-brief.md`, `references/rulings.md`.

## Start, resume, compaction
Preflight runs once per session in at most 15 tool calls; its turn ends only with wait.sh armed.
0. Skills, once a day: `py -3 %USERPROFILE%/.claude/skills/dev-setup/scripts/check_skills.py`; on UPDATED re-read this file. Permissions: `py -3 ~/.claude/skills/dev-setup/scripts/permissions.py --repo <repo> --dir <FLEET> --dir <worktree root> --dir <test data> --host <each ssh machine> --cli <check CLI> --guard <FLEET>/bin/burn-guard.py --stop-guard <FLEET>/bin/master-stop-guard.py`. Give the owner its paste line once, only when the session lacks full permissions; never edit permission settings yourself.
1. Read the six spec files and FLEET/COORD-STATE.md once (again only after a compaction). PAUSE.md or COORD-STATE.md present: resume from it with a soft launch.
2. Measure every MACHINES.md machine (cores, free RAM, free disk per drive); one under its floor gets no check slots until cleaned (whole target dirs, by PID).
3. Repo on main, clean, pushed. FLEET has bin/, dash/, hooks/, inbox/, handbacks/, templates/, DISPATCH.md, RULINGS.md and every template MASTER-AGENT lists; a missing template is a protected-batch item.
4. Install per `references/tick.md`: the guards and tick into FLEET/bin, FLEET/fleet.json and BUDGET.json, tick on the scheduler every 10 minutes; run tick once and see one CHECKS.tsv row per check.
5. Open the dashboard URL from MASTER-AGENT.md. If it does not open on this account, publish overhaul-plan's `assets/dashboard-index.html` byte for byte (overhaul-plan Step 5) and write the URL to COORD-STATE.md.
6. Launch the heartbeat, then the ready rows, then arm `FLEET/bin/wait.sh` in the background.

## Rules: the master
- Wake only when wait.sh exits (a new verdict red, a gate result, FLEET IDLE, or HEARTBEAT DUE 17 to 20 minutes after the last upload); never poll; a hand-back or "still waiting" notice gets no tool call [by wait.sh; burn.py main_turns_per_hour].
- Each wake starts with `py -3 FLEET/bin/master-status.py`; act on every line, then re-arm wait.sh [by master-stop-guard.py: no turn ends with a launchable line, wait.sh unarmed, or the upload, health or heartbeat over 25 minutes old].
- The only launch prompt is "Read and do <brief path>." for a `ready` DISPATCH.md row or a standing brief in fleet.json; each brief once; model and effort from its row; every ready row in one message as background calls; never a fork, never an agent only to wait [by burn-guard.py].
- You write no code, brief, review, ruling, DISPATCH row or R number, and push nothing to main; briefs and rows come from the dispatcher, ids and numbers from `add-row` [by burn-guard.py (inline prompts refused); add-row (refuses a duplicate id); branch protection].
- Routine work is a tick check, never a turn: health, dispatch, review rows, landing, dashboard data, quality, audits, burn, skills [by tick.py; verdict.py reds every check that is stale or failing].
- The heartbeat is the standing brief FLEET/briefs/heartbeat.md every 20 minutes: the verdict line, QUALITY, at most 5 reds with their fixes, and the dashboard upload; there is no separate dash-refresh or red-fix launch [by burn-guard.py (a standing brief at most once per 15 minutes); master-stop-guard.py (upload age)].
- Act on the verdict: an `auto:` fix is already running in tick; for any other red write one line to FLEET/inbox/<red>.md and the dispatcher makes it a unit; a check failing twice is already a tool-fix unit [by verdict.py; tick.py].
- Your context: small output, no whole-file reads, no logs beyond reds; hand off at 150k with COORD-STATE.md current; never ask the owner for /compact or a fresh session [by burn.py main_ctx (BUDGET.json main_ctx 150000)].
- COORD-STATE.md: you add only decisions, the soft-launch stage and next launches; every count in it is copied from a measurement [by health.py, which writes its mechanical fields].
- Status to the owner, four lines at most: the VERDICT line, the pipeline `proven X, landed Y, waiting-land Z, in review W of N`, the last gate with its open reds, the one deciding item [by verdict.py and dash/pipeline.json, copied, never estimated].
- The owner is never asked a question or for a signature: a ruling unit decides. Only money, deleting outside a worktree, publishing publicly or an account change needs a yes: one line with the exact command in FLEET/OWNER-ASKS.md, routed around the same wake [by master-stop-guard.py: launchable work blocks the stop, ask or no ask].
- Routine work in the owner's private repos never waits on the owner: pushing, opening, retargeting, closing or merging fleet branches and PRs, deleting merged ones [by land-keeper; the gate bot].
- Account switch or limit: update `active` and `spent` in FLEET/accounts.json at once; the next heartbeat's upload proves the dashboard opens on that account [by the heartbeat upload (a refused upload is a red)].
- Waste, not spend: never stop, pause or shrink a productive fleet for spend; fix a waste red by removing the waste (stop readers and spinners, fresh passes, land before building), never by raising a threshold [by burn.py waste_share and tokens_no_land, which write BURN-STOP; burn-guard.py; BURN-STOP clears itself].

## Rules: units and dispatch
- One unit = one function, or a plan-made split of it with its own tests, never a cut by owner; one unit = one packet = one agent pass [by packet_check.py brief (required reading under 60k tokens) and diff (only Allowed files, changed lines under the Lines cap, default 300)].
- A unit is ready only when every Needs unit has landed on main; the one exception is a single stack level on a base whose review is PASS at its head, landing after that base [by the dispatcher's ready rule; burn-guard.py (ready rows only); the gate's solo pre-check (never batches a PR whose base is not main); land-keeper (retargets to main)].
- Every unit names its FUNCTIONS id, bug id or inbox id, and the test name health counts for it [by the dispatcher (refuses a unit without one); packet_check.py brief].
- A brief is self-contained: the id and test name, the text of each STANDARDS rule touched, the INDEX of every owner called, Allowed, Lines, call cap, test style, the spec's work-style block word for word, and for a follow-on the previous hand-off; builders read the brief, then code, never spec/ or FLEET history [by packet_check.py brief; the management check over briefs].
- No brief or template carries a deadline, time limit, target hour or rate: only call caps, exit checks and ordering gates; only health.py measures time [by the management check over briefs].
- Order: owner feedback and bugs first, then the current phase's functions nearest proven (fewest missing Acceptance pieces) [by the dispatcher's order; verdict.py (proven flat 3 h is a red)].
- Phase start: no build unit of a phase is ready until health reads a test for every function of the phase and the phase gate's own Acceptance test exists, expected-fail, with an owning unit [by tick check counting; plan_validate.py --phase for plans with spec/UNITS.md].
- A caller or overlap grant is plan data: it goes in the unit's own PR, never a protected batch [by the import lint (a grant the diff does not use fails); the blind review].
- Another owner's capability is a hook to that owner, never a copy (H13 rejects a copy, even one that works); a moved capability moves its callers and tests and leaves no alias [by S2 (body fingerprints and exact duplicate public names, never name globs); the blind review].
- A hook whose fix landed is unskipped the same tick [by tick check unskip].
- Superseding: the replacement starts from main, then `supersede <old> <new>`; the old unit never relaunches [by supersede (refuses while the old unit has unsaved work); burn-guard.py].
- Rulings: the dispatcher writes the brief (the question, the exact file:line, `references/rulings.md`) and its row with the R number; the ruling agent returns 3 lines [by add-row; burn-guard.py].
- A ruling that names machinery (a scheduled task, a bot, a hook) counts only when its check row is ok [by tick.py CHECKS.tsv; verdict.py].
- A bug or owner feedback gets a PATTERNS.md id and a fix unit in its owner: a red pin plus a sweep of every control sharing the owner; a pin already green on main closes as `holds` [by the dispatcher (inbox to units); the check CLI holds].

## Rules: agents
- One agent owns a unit from launch to queued for review within its call cap (build about 100, review, ruling and paper about 60); at the cap it runs `wip` and hands back, and the next pass is a fresh agent with the hand-off [by the subagent context guard hook (warns at 130k, blocks at 160k); burn.py waste_share].
- Waits on a check are one foreground wait of at most 9 minutes per call, never a background wait that re-wakes the agent [by the work-style block in every brief; burn.py waste_share].
- A hand-back is three lines; the full report goes to FLEET/handbacks/<unit>.md, with a `Blocker: <category>` line when blocked [by the check CLI handback; tick.py recurrence (a category seen twice in a day files inbox/process-<category>.md)].
- A stuck agent (H02, H04, H05, H06, H14) is stopped after `wip`, and a fresh, smaller pass is queued with its hand-off [by health.py tripwires; master-status.py].
- The same failure twice changes the plan (a ruling, a split, a different approach), never a third identical pass [by the dispatcher (no third pass after two FAIL reviews on a unit); tick.py recurrence].
- Every tool is safe by default: PID-checked locks, atomic writes, PAUSE.md a no-op, kill by PID only, `fix` never writes protected files, scheduled runs windowless; a tool failing its check twice becomes a tool-fix unit with a test [by the red-team row per tool; tick.py (inbox/tool-<name>.md)].

## Rules: reviews and landing
- Every PR gets a fresh blind review from the fixed template (only `<TARGET>` filled): line 1 exactly PASS or FAIL, line 2 the head sha; one reviewer per 3 to 5 small PRs [by tick check review-dispatch (a row for each PR with no review at its head); the gate (accepts only PASS at head)].
- A finding under about 10 lines is fixed by the same unit's follow-up pass, never a new fix agent plus a re-review agent [by the dispatcher].
- One land path: the gate bot lands every PR and desk row; no agent pushes to main [by branch protection; the gate bot].
- Landing runs itself: land-keeper retargets merged bases, merges main into conflicting PRs and clears stale refusals; the gate pre-checks each PR alone against main, so one red blames only itself [by land-keeper (the pull-main check); the gate's solo pre-check].
- Backlog brake: while PASS PRs waiting to land exceed max(2, 2 x gate merges per hour over 3 h), only land-, fix-, tool- and review- briefs launch [by verdict.py (dash/verdict.json brake); burn-guard.py].
- Fleet size is the ready rows, capped by the soft-launch step, the owner's number and the MACHINES.md slots; never fill the fleet with work that is not ready [by burn-guard.py; master-status.py].
- Nothing merged for 6 hours stops builder launches until landing is fixed [by burn.py tokens_no_land; burn-guard.py].
- A red full gate on main, or a check step that fails every check (a missing tool, path or shim), is a red main: its fix unit goes first, and merges into the failing packages wait [by the gate bot; the dispatcher's order].

## Rules: proof and quality
- A function is proven only when its Acceptance test passes in a full no-fail-fast gate (every crate and package) of the deployed sha; never "fixed in code", a stub or a hand-back claim; nothing is live before the deployed sha matches [by health.py].
- The gate bot runs the full main gate on its own clock at least every 2 hours and deploys only a sha that passed it, with that sha's own deploy code; a cached result still names each passing test [by tick check proof-path (the deployed sha has a full gate row and health reads an Acceptance test from it)].
- The shared checkout is pulled every 5 minutes, and the gate bot restarts by PID while idle when its gate or check code is older than main's [by gate-fresh.py; the pull-main check].
- A unit that changes the check CLI or a lint tests with the shim's self-mode (VOIDCAD: `VC_SELF=1 vc ...`) [by tick check red-team (run on every lint or tool change)].
- A host runs checks or the gate only after one full main gate passes on it [by the gate bot's host record; tick check proof-path].
- A bug fix counts only when the gate shows its pin red on main before and green after [by the gate record; health.py].
- Quality is a standing number: post-merge sample audits, S2 duplicates and cross-owner imports on main, files over the size limit, the latest blind score; red blocks GOOD, and each real bug or duplicate on main files a fix unit [by tick check quality (dash/quality.json); verdict.py].
- The 12-hourly blind audits (code quality, 10% of proven functions for stubs and thin fakes, management over briefs and FAIL reviews) each leave a result file; one with no result is a red [by tick checks audit12 and management (`expect`: a result newer than the run); verdict.py].
- Lints are never waived or skipped; a builder never reshapes code to pass a lint; a wrong lint is fixed in a protected batch [by the check CLI (no waiver path); tick check red-team].
- A protected batch holds rule code only (limits, lints, templates, health.py, the protected list) and gets a blind review; plan data (callers, overlaps, test names, exemptions) is validated, never batched [by PROTECTED and the gate (refuses a protected path outside a batch); tick check counting; plan_validate.py].

## Rules: phases and gates
- M0 first: nothing but M0 units until M0 lands, except PLAN.md's Phase 0 briefs once item 1 lands; until the dispatcher lands, briefs/m0-dispatch.md is a standing brief whose ruling agent writes M0 and Phase 0 briefs and rows from PLAN.md, in order [by burn-guard.py (standing_briefs); health.py FLEET/PHASE].
- An M0 item that adds a routine job is done only when its fleet.json check has an ok CHECKS.tsv row [by tick.py; verdict.py].
- Soft launch at every phase start, ramp step and restart: 3 pilots on three owners; first edit by call 15 (H14), first check by call 40, first PR within the cap; ramp 10, 20, then the MACHINES.md size, only with every pilot's first PR merged green and no tripwire red [by health.py (pilot gate, H14)].
- Phase 0 gate: no later-phase build unit until Phase 0 is proven on deployed main, M0 has landed and every red-team violation is refused [by health.py (FLEET/PHASE phase0_proven); the dispatcher; master-status.py].
- Stall gate: proven flat for 3 hours however many merges land, the proven rate under 70% of the projection for 2 runs (H24), or tokens per proven function over budget for 2 runs (H03) stops the phase: write the cause, send the owner a digest, and a fresh blind auditor approves the re-plan [by verdict.py; health.py H24, H03].
- Freeze gate: a module frozen twice stops the phase the same way; a frozen module gets one fresh Opus architect, never more fix passes [by health.py H23].
- Only health.py derives the projection, after the pilots' first PRs and every 24 active hours; no agent is told it [by health.py].

## Rules: finish, pause, never
- Finish: when proven equals the total and the full gate is green, one final blind Opus audit against STANDARDS and the definition of done; then a clean end state (no open DISPATCH rows, no unanswered "story does not say", no branch or worktree but main), every agent stopped, tick off the scheduler, FLEET/FINISHED.md, a four-line report; nothing is dispatched after [by health.py; the check CLI handback; master-stop-guard.py (silent once FINISHED.md exists)].
- Pause on the owner's stop: `wip` every worktree, stop every agent, write FLEET/PAUSE.md (in flight, open reds, what to relaunch), report in two lines [by tick.py and burn-guard.py (PAUSE.md: no checks, no launches)].
- Never weaken this skill's gate, finish or never rules through the spec, and never launch the full fleet without a soft launch [by the blind review of every protected batch; health.py pilot gate].
