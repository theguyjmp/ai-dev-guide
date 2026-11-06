---
name: overhaul-check
description: Decide whether the current project needs a full overhaul (rebuild from the ground up), a partial rewrite of some areas, targeted refactors, or nothing. Measures and scores architecture, code organization, code quality, duplication, tests and the gate, dependencies, security, bug and change history, build and delivery, roadmap fit, wordiness (AI slop in code, comments, docs and UI text) and the owner's own rules, then writes a scored report with a verdict and the repair-versus-rebuild hours. Use when the owner asks whether a project needs an overhaul or rewrite, or wants a full health or quality audit of a codebase.
---

# overhaul-check

**Skills check (once a day, first step):** run `python3 ~/.claude/skills/dev-setup/scripts/check_skills.py` (Windows: `py -3 %USERPROFILE%/.claude/skills/dev-setup/scripts/check_skills.py`). It prints one line and checks the ai-dev-guide repo at most once a day; every later call that day only reads its note, so do not run it again in this session. If it says UPDATED, re-read this SKILL.md before going on. If the script is missing or fails, carry on.

You decide, from numbers, whether PROJECT (the current working directory's repo, unless the owner names another) needs an overhaul. You are read-only on PROJECT: no edits, commits, pushes, deploys, installs of new tools, or Docker. The only file you write inside PROJECT is the report, left uncommitted. Do not ask the owner questions; take the recommended option and note it in one line.

A rewrite is expensive and repeats old mistakes unless something forces it. So the default verdict is the smallest one the numbers allow, and OVERHAUL needs the proof in Step 6.

## Step 1. Inputs (find them yourself)
- PROJECT's root, language and toolchain, from its manifests (package.json, Cargo.toml, pyproject.toml, go.mod and so on).
- PROJECT's CLAUDE.md, README, architecture or design docs, roadmap or feature map, and any bug ledger (BUGS.md, notes/, issues). Use grep and head only; do not read whole large files in this chat.
- The owner's CLAUDE.md rules and memory. Every owner rule is checked in dimension O.
- Old fleet history if it exists (burn, outcome or context-watch output; hand-back folders): items landed per hour, tokens per item, rework passes.

## Step 2. Measure by script
Run `py -3 -I ~/.claude/skills/overhaul-check/scripts/measure.py <PROJECT> --out <scratchpad>/measure.json`. Add `--depth 3` when the top two folder levels are too coarse to separate modules. It prints 13 lines, which are the only part you read in chat. The JSON holds: lines of code by language and module, test ratio, files over 500/1000/2000 lines, functions over 75/150 lines, duplication % and the worst file pairs, module import cycles, fan-in and fan-out, duplicate-capability file names (several stores, registries, configs and so on), TODO and suppression density, secret hits (file:line only), LLM call sites without `cache_control`, raw colors versus tokens, wordiness (filler-phrase hits per kLOC, comment ratio, docs words per kLOC, emoji, the wordiest files and docs), manifests, and git churn, fix share, reverts and hotspots.

## Step 3. Run the project's own checks (read-only, at most 15 minutes each, low priority)
Use only tools already installed and dependencies already present. Anything you cannot run is recorded as "not run: <reason>"; it is never a pass.
- Build or compile from the current checkout (for example `cargo check`, `tsc --noEmit`, `go build ./...`, `npm run build` when it writes only to its usual output folder).
- Lint and type check (for example clippy, eslint, ruff, mypy or pyright): count errors and warnings per kLOC.
- The unit test suite, plus any other tests that need no network, credentials or running services: pass, fail and skip counts and the run time. End-to-end and integration tests that could reach prod, send mail or write a database are recorded as "not run: side effects". If it has failures, rerun only the failed tests once, so flaky tests are counted separately from real failures.
- Dependency audit where the tool exists (`npm audit --json`, `cargo audit`, `pip-audit`): high and critical counts, plus majors behind for the main framework.
- CI config: does a gate exist, and what does it run?
- Keep each command's output small: write it to a scratch file and read only the counts with grep or tail.

## Step 4. Three auditors in parallel (skip if PROJECT is under 5,000 LOC; then do this yourself)
Write each brief to the scratchpad and launch the three agents in the background with "Read and do <brief path>." Do not tell them a suspected verdict. Each one reads measure.json by key (`py -3 -c` or grep), never whole. Each one writes its findings file and hands back three lines.

- **Architecture and organization (Opus, 80 calls):** "Read measure.json (module graph, modules, largest files, duplicate-capability names) and PROJECT's docs. Then map the real layers and owners from the code. Report, with file:line for each:
  1. the intended architecture against the real one;
  2. import cycles and layer leaks (UI reaching storage, core reaching UI);
  3. god modules;
  4. capabilities implemented twice (state stores, registries, undo, pickers, config, HTTP clients). measure.json's duplicate-capability names are only leads; confirm each from the code, and list confirmed ones separately;
  5. global mutable state;
  6. hand-edited central lists;
  7. folders that mix concerns;
  8. naming drift.
  For each roadmap item in the docs, say whether it fits the current seams, needs a local refactor, or needs a new architecture. Give one verdict per module: keep, refactor or rewrite, with a one-line reason. Write <scratchpad>/check-arch.md."
- **Code quality and security sample (Opus, 80 calls):** "Read measure.json. Read the first 200 lines of each of the 10 longest functions, the duplicated span of the 5 worst duplicate pairs, and 10 random non-test files (at most 300 lines each). Rate each file on these points, each with file:line:
  1. error handling (swallowed errors, panics or unwraps on user paths);
  2. state handling and races;
  3. dead code;
  4. magic values;
  5. test quality (do tests wait on real results, or on proxies and sleeps; do they mock the thing they test);
  6. how readable it is to a new agent;
  7. wordiness (AI slop): comments that restate the code, filler in docs, READMEs, UI text and error messages, wrappers that only pass through, and docs nobody acts on. Start from measure.json's slop block, and quote the worst 5 with file:line.
  For security, check the secret hits without ever printing a value, injection (SQL, shell, path, HTML), authz holes, unsafe deserialization, and data leaving the machine. Write <scratchpad>/check-quality.md."
- **History (Sonnet, 60 calls):** "From git (measure.json's git block), the bug ledger and the issue tracker (`gh issue list --state all --limit 500 --json number,title,labels,state,createdAt,closedAt` if a GitHub remote exists), measure:
  1. open and closed bug counts and bugs per kLOC;
  2. recurring bug patterns (same root cause 3 or more times) with their ids;
  3. bugs that came back after a fix;
  4. the files where fixes concentrate;
  5. the median time to close.
  Also measure owner feedback themes, and the old fleet's landed-per-hour and tokens-per-item figures if they exist. Write <scratchpad>/check-history.md."

Wait for all three with no polling; a "still waiting" notice gets no tool call. Read only each file's findings list, not the agents' transcripts.

## Step 5. Score each dimension 0 to 3
0 is healthy, 1 minor, 2 major, 3 critical. Use the measured number; if two rules apply, take the worse score. Every score cites its numbers and the 1 to 3 worst file:line examples. Duplicate capabilities (dimension A) count only when the architecture auditor confirmed them; name hits alone never score.

| Dim | What | 1 when | 2 when | 3 when |
|---|---|---|---|---|
| A | Architecture | 1–2 module cycles, or 1 duplicate capability | 3–5 cycles, 2–3 duplicates, or a god module over 4,000 lines | over 5 cycles, a cycle of 6+ modules, over 3 duplicates, or no layer can change alone |
| G | Organization | naming drift, a few misplaced files | folders mix concerns, hand-edited central lists | no structure a new agent can navigate from the tree |
| Q | Size and complexity | files over 500 lines ≥5%, or functions over 75 lines ≥2% | ≥10% or ≥5% | ≥20% or ≥10% |
| U | Duplication | ≥3% of lines | ≥6% | ≥12% |
| H | Hygiene | lint and type errors ≥1 per kLOC, or suppressions ≥2 per kLOC | ≥5, or ≥5 | ≥20, or ≥15 |
| T | Tests and gate | test ratio under 0.5, or no CI gate | ratio under 0.2, red tests, or flaky tests | no tests, or the suite cannot run |
| D | Dependencies | majors behind on the main framework | any high vulnerability | a critical vulnerability, or an end-of-life framework |
| S | Security | minor issues | a confirmed injection or authz hole | a secret in the repo (tell the owner at once), or data leaving the machine without consent |
| B | Bug history | fix share ≥25%, or 1–2 recurring patterns | ≥40%, 3–5 patterns, or bugs that came back | ≥60%, or over 5 patterns |
| R | Build and delivery | build warnings | the build is flaky or slow (over 10 min for a check) | it cannot build from a clean checkout |
| F | Roadmap fit | 1 roadmap item needs a local refactor | several need refactors across modules | the core roadmap needs a new architecture |
| W | Wordiness (AI slop) | filler hits ≥2 per kLOC, or comment ratio ≥0.35 | ≥5 per kLOC, comment ratio ≥0.5, or docs over 2,000 words per kLOC | ≥10 per kLOC, or docs over 5,000 words per kLOC |
| O | Owner rules | 1 violation class | an LLM call path without prompt caching, or raw styles where tokens exist | any modesty, data-consent or secrets violation |

## Step 6. Verdict (the smallest one that fits)
W is fixed by a sweep and a slop lint, so it adds fix-list items but never counts toward PARTIAL REWRITE or OVERHAUL.
- **NO ACTION:** no dimension at 2 or above.
- **TARGETED REFACTOR:** any dimension at 2 or above, but A and F at 1 or below, and every fix stays inside one module.
- **PARTIAL REWRITE:** A, G or F at 2 or above, but confined to modules holding under a third of the code. Name the modules. Rewrite them behind their interfaces; keep the rest.
- **OVERHAUL:** A or F at 3, or A at 2 or above plus two of U, B, T or F at 2 or above, or five or more dimensions at 2 or above. On top of that, both of these must hold:
  1. **What a refactor cannot fix:** a list of the specific defects that only a rebuild removes (for example the core data model, a cycle through every layer, a framework at end of life). If this list is empty, the verdict drops to PARTIAL REWRITE.
  2. **Repair against rebuild, both in total agent-hours:**
     - Repair = fix packets × agent-hours per packet (1, or the old fleet's measured rate) × passes per packet (1.5, or measured).
     - Rebuild = functions (from the feature map, routes or commands) ÷ functions per builder-hour × passes per function, plus 60 agent-hours for M0.
     - Use the overhaul-plan defaults (1 function per builder-hour, 1.5 passes) where nothing is measured, marked ASSUMED.
     - Neither side is divided by fleet size. Wall-clock hours may be shown beside them but never decide the verdict.
     - OVERHAUL needs repair to be at least 60% of rebuild. Never in days.

## Step 7. Report
Write `<PROJECT>/OVERHAUL-CHECK.md` (do not commit it); overhaul-plan reads it from there. Keep it to about two pages:
1. The verdict in one line, and the three numbers that decided it.
2. The scorecard: dimension, score, numbers, worst file:line examples.
3. A per-module table: module, LOC, keep, refactor or rewrite, and the reason.
4. For OVERHAUL: the "what a refactor cannot fix" list, and repair against rebuild hours. For any other verdict: the ranked fix list, one line each (where, defect, fix, hours).
5. What must survive any rebuild: data formats, proven libraries, tests worth keeping, and the hard-won fixes from the bug history.
6. Not run: every check you could not run, and why.
7. A pointer to measure.json and the three findings files. Copy them next to the report as OVERHAUL-CHECK-data/ so overhaul-plan can use them.

## Step 8. Chat reply (four lines at most)
1. The report path.
2. The verdict and the one deciding number.
3. The next step: "/overhaul-plan" for OVERHAUL, the top fix for anything else.

## Never
- Edit, commit, push, deploy, install a new tool, or run Docker in PROJECT.
- Print a secret's value anywhere (report the file:line and kind only).
- Count a check you could not run as passed.
- Call OVERHAUL without the "what a refactor cannot fix" list and the hours comparison.
- Estimate in days or weeks.
- Launch agents beyond the three auditors.
- Read whole large files or long logs in this chat.
