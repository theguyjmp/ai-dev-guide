---
name: dev-build
description: Start, run or resume the master agent on a new project planned by dev-plan, strictly by its spec, at agent speed in hours, through M0, the Phase 0 risk slice, a soft launch, the health loop and progress gates to a defined finish. Follows overhaul-build with the old-project steps removed. Use when the owner says "start the build", "resume the build", "build the new app", or "dev-build", or when dev-plan ends with "Yes, start the build". The third of three skills: dev-prep, dev-plan, dev-build.
---

# dev-build

**Skills check (once a day, first step):** run `python3 ~/.claude/skills/dev-setup/scripts/check_skills.py` (Windows: `py -3 %USERPROFILE%/.claude/skills/dev-setup/scripts/check_skills.py`). It prints one line and checks the ai-dev-guide repo at most once a day; every later call that day only reads its note, so do not run it again in this session. If it says UPDATED, re-read this SKILL.md before going on. If the script is missing or fails, carry on.

Read `~/.claude/skills/overhaul-build/SKILL.md` in full and follow it, with the differences below. Everything else is identical: you are the master and the main chat only (never a subagent), builders run in parallel in the background, hook-first, the tripwires and gates, no deadlines, time limits or rates given to any agent, and its precedence, Agents, Gates, Finish, Never and Pause sections.

## Differences
- **The spec comes from dev-plan:** the same six files plus spec/FUNCTIONS.md, spec/PATTERNS.md and spec/LATER.md, in the repo dev-plan created. FLEET is `<NEW>-fleet` beside it. If the spec is missing or fresh_check reports misses (`py -3 ~/.claude/skills/dev-plan/scripts/fresh_check.py <NEW> <FLEET>`), stop and say dev-plan must finish first.
- **No salvage:** M0 item 8 is only the design notes PLAN.md lists; there are no salvage PRs and no pinned salvage commit. The mechanic only merges audited M0 PRs.
- **No quarantine:** M0 item 6 has no fingerprint index or fingerprint lint of an old project; every other lint stays.
- **health.py is written fresh:** there are no old-fleet context-watch, burn or outcome-watch scripts to reuse; it measures them from the transcripts itself.
- **LATER rows are never dispatched.** A seam interface for a LATER row stays a stub and counts for nothing. A LATER row is built only after the owner moves it to now; then a ruling adds its FUNCTIONS row with an Acceptance, and it goes through the Acceptance packet and build packet like any other function.
- **Finish:** there is no original to compare with, so the difference report (overhaul-difference) does not apply. Finish exactly as overhaul-build's Finish says.

## Never (in addition to overhaul-build's)
- Dispatch a LATER or maybe row, or a packet for a function not in FUNCTIONS.md.
- Edit the overhaul skills.
