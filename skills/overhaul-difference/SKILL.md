---
name: overhaul-difference
description: Compare a rebuilt project (NEW) with the original (OLD) after or during an overhaul, from measurements only, and publish the difference as a dashboard artifact - verdict, OLD vs NEW scorecard on the overhaul-check dimensions, code metrics side by side, function parity, bug patterns, fleet cost and what survived. Use when the owner says "compare the overhaul with the original", asks for a "difference report", "what changed in the rebuild", or "overhaul-difference". The fourth skill after overhaul-check, overhaul-plan and overhaul-build.
---

# overhaul-difference

**Skills check (once a day, first step):** run `python3 ~/.claude/skills/dev-setup/scripts/check_skills.py` (Windows: `py -3 %USERPROFILE%/.claude/skills/dev-setup/scripts/check_skills.py`). It prints one line and checks the ai-dev-guide repo at most once a day; every later call that day only reads its note, so do not run it again in this session. If it says UPDATED, re-read this SKILL.md before going on. If the script is missing or fails, carry on.

You compare NEW (the rebuild overhaul-plan wrote and overhaul-build is building) with OLD (the project it replaces), and give the owner one dashboard page. Every number on it is measured or read from a file; anything missing says "not measured" and why, never a guess. You are read-only on OLD and NEW: no edits, commits, pushes or deploys. You write only FLEET/difference/ and FLEET/DIFFERENCE.md. Do not ask the owner questions; take the recommended option and note it in one line.

## Step 1. Inputs (find them yourself)
- OLD: the old project's git repo root. If the folder the owner names is not a git repo and holds worktrees or scratch copies, use the repo inside it; measuring copies inflates every number.
- NEW: the rebuild's repo. FLEET: its paper folder, `<NEW>-fleet` beside it unless the plan names another.
- Owner rules: the owner's CLAUDE.md files (dimension O).
- Reused, never re-measured: `<OLD>/OVERHAUL-CHECK.md` and `<OLD>/OVERHAUL-CHECK-data/measure.json`; NEW's spec/FUNCTIONS.md, PATTERNS.md, STANDARDS.md, PLAN.md, ARCHITECTURE.md; FLEET/dash/*.json and FLEET/HEALTH.tsv.

## Step 2. Measure only what is missing
- OLD: if OVERHAUL-CHECK-data/measure.json exists, use it. Otherwise run `py -3 -I ~/.claude/skills/overhaul-check/scripts/measure.py <OLD> --out <scratchpad>/old.json` once, in the background (it can take minutes on a large repo), and reuse that file on every rerun.
- NEW: always measure it fresh: `py -3 -I ~/.claude/skills/overhaul-check/scripts/measure.py <NEW> --out <scratchpad>/new.json`.
- If NEW has its own `<NEW>/OVERHAUL-CHECK.md`, compare.py takes NEW's scores from it. Otherwise it scores NEW by script on Q, U, H, T, B, W, O and A (cycles and module size only), and leaves G, D, S, R and F as not measured. Run overhaul-check on NEW only when the owner asks for a full scorecard.

## Step 3. Compare by script
Run:
`py -3 -I ~/.claude/skills/overhaul-difference/scripts/compare.py --old <OLD> --new <NEW> --fleet <FLEET> --out <FLEET>/difference --old-measure <old.json> --new-measure <new.json>`
Add `--old-bugs <ledger>` when OLD's bug ledger (BUGS.tsv or BUGS.md) is not inside OLD. It prints 6 lines, the only part you read in chat. It writes one dataset per report section into FLEET/difference/:

| File | Holds |
|---|---|
| verdict.json | one row: functions proven of total, the 3 biggest measured improvements, the biggest regression, dimensions better and worse, OLD's verdict, sources |
| scorecard.json | each dimension A to O plus W: OLD score, NEW score, the deciding numbers, change, sources |
| metrics.json | LOC, files over 500/1000/2000, functions over 75/150, duplication, cycles, largest module, duplicate-capability leads, suppressions and markers per kLOC, test ratio, filler and docs words per kLOC, comment ratio, LLM call files without caching, raw hex against tokens, secret leads, fix share |
| parity.json, functions.json | per system: proven, building, blocked, missing; every FUNCTIONS row with its dashboard status |
| patterns.json, bugs.json | each PATTERNS row, its rules and lints, whether the lint exists in NEW, bugs filed against it; old ledger against new ledger and git |
| cost.json | NEW's rates from dash/status.json, rates.json and HEALTH.tsv; the old fleet's measured numbers as PLAN.md recorded them |
| survived.json | salvaged files and whether NEW has them, kept data, rewritten parts, OVERHAUL-CHECK's must-survive list, owner capabilities, old systems with no proven function |

If NEW has no code yet, every NEW measurement reads "not built" and the page shows plan parity only. That is a valid report, not a failure.

Read the JSON by key if a summary line looks wrong (`py -3 -c`), never whole. If compare.py fails twice on the same step, stop and report the error line.

## Step 4. Publish the dashboard
The page is `~/.claude/skills/overhaul-difference/assets/difference-index.html`. Do not design a new one.
1. If FLEET/DIFFERENCE.md holds a dashboard URL, this is a rerun: skip to step 3 and update that artifact.
2. Run Artifact `quickstart` with intent `other` to get the Dashboard type's `type_url`. Create the dashboard with that `type_url`, title "<NEW> vs <OLD>" and `auto_open: "after_first_write"`, and follow the type's instructions it returns. Write the URL into FLEET/DIFFERENCE.md at once.
3. Upload each of the 9 JSON files in FLEET/difference/ as an asset of the dashboard (one call per JSON file, all in one message).
4. In one ArtifactData batch write `dash/meta` `{title}`, then `datasets/<name>` for each file as a `kind: "file"` source with its uploaded `url` and `name`, a short `title`, `updated`, `edit` and `fixed: true` (a snapshot; a rerun replaces it), then `files/index.html` with the template's text and `{{TITLE}}` replaced. On a rerun, read `files/index.html` first and write it only if the template changed; update each dataset with one `update` of `url`, `name`, `updated` and `edit`.
5. Run the type's check read and fix what it confirms, at most 2 fixes.
6. FLEET/DIFFERENCE.md: the URL, the date, compare.py's 6 summary lines, and the rerun recipe (Steps 2 to 4 with the same URL).

## Step 5. Chat reply (four lines at most)
1. The dashboard URL.
2. Functions proven of the total.
3. The biggest improvement and the biggest regression, or "NEW not built yet".

## Never
- Edit, commit, push or deploy in OLD or NEW.
- Guess a number or fill a gap from memory; it shows "not measured" with the reason.
- Call a score better when OLD and NEW were scored from different sources without saying so (compare.py notes it).
- Measure OLD twice, or measure a folder of worktree copies.
- Create a second dashboard when FLEET/DIFFERENCE.md already has one.
- Design a different page, or put numbers in the page's markup.
- Launch agents.
- Read whole JSON files, measure output or large docs in this chat.
