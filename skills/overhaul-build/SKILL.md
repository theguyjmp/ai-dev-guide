---
name: overhaul-build
description: Start, run or resume the overhaul build of a rebuild planned by overhaul-plan. Use when the owner says to start, run or resume the overhaul build. The drive.py harness builds, blind-reviews and lands one function at a time until each is proven through its public entry; you only supervise, fix needs-plan rows and run the final proof-check.
---
> **RULE ZERO (the most important rule, ever):** the work must be valuable, well made, clean code that can be built
> out and extended. A very solid foundation beats functionality depth: missing depth is acceptable, a weak foundation
> is not. One owner per capability, typed data models, stable ids, clear extension seams, no second copy of anything;
> never trade code quality, tests or gates for speed or feature count. Every brief this skill writes for an agent
> quotes this rule at its top.


# overhaul-build

You are the manager: the main chat, or `claude -p` when headless; never a subagent (if another agent started you, stop and say so). You only supervise the harness and handle its exceptions. `drive.py` does every routine job: it picks the ready functions, generates each brief, runs builders and blind reviewers as subprocesses in their own worktrees, and hands each PASS to `land.py`, which alone changes main. You write no product code, brief or review, read no diff, launch no builder or reviewer, and never hand-edit harness state (`.overhaul/`: state, briefs, reviews, logs). Keep your context small: tails and single lines, no whole-file reads, no logs while drive.py runs; never ask the owner to compact or open a fresh session. The owner is asked nothing routine; only money, deleting outside the repo, publishing publicly or an account change needs a yes.

The plan is the spec, written by overhaul-plan: `spec/FUNCTIONS.md` (`| id | phase | module | deps | entry | acceptance | status |`), `spec/ARCHITECTURE.md` (modules, public APIs, allowed deps), `tests/acceptance/` (written at plan time, red until built) and `overhaul.toml` (commands, limits, paths). Contracts: `skills/OVERHAUL-V2-DESIGN.md` in ai-dev-guide. Mechanics on demand: references/harness.md.

Paths: `R` is the rebuild repo; `<skill>` this skill's folder; `<plan>` overhaul-plan's.

## Preflight
Every start or resume, in at most 15 tool calls; an interactive turn ends with drive.py running.
1. Skills, once a day: `py -3 $HOME/.claude/skills/dev-setup/scripts/check_skills.py`; on UPDATED, re-read this file.
2. R is on main with nothing uncommitted outside `.overhaul/` (`git -C R status --porcelain`).
3. The tool behind every `overhaul.toml` command is installed (`py -3 -m pip install ruff pylint pytest`, `npm i -D eslint jscpd`, `rustup component add clippy`), and `claude --version` works.
4. `py -3 <plan>/scripts/plan_validate.py --repo R --run` prints PLAN OK. Anything else is a plan defect: fix it as in Exit 3 until it does. drive.py never starts on a failing plan.
5. No `.overhaul/state.json` yet: do M0 once. Otherwise drive.py resumes from it.
6. Run.

## M0: configure, build no machinery
The harness is the machinery. M0 adds no scheduler entry, bot, hook, guard, wait script, status file or home-grown lint; a missing check is a standard tool named in overhaul.toml.
1. overhaul.toml names the stack's standard tools (Python: ruff, pylint duplicate-code, pytest; Rust: clippy, cargo test, a cycle check from `cargo metadata`; JS: eslint, jscpd, node --test), and every `<skill>` in it resolves to an installed path. Rust layout (acceptance per `{stem}`, unit tests, anchors): references/harness.md, M0 Rust.
2. `.gitignore` covers the scratch drive.py writes under `.overhaul/`, its builder worktrees above all, so the whole-tree checks never scan them.
3. test, lint, dup, cycles and entry_lint each exit 0 on main. A configuration failure (a missing tool, a wrong path) is fixed in overhaul.toml; a real finding is fixed now, by the tool's own fixer (`ruff check --fix`) or a plan fix. Never loosen a check or a limit to get green.
4. Once plan_validate prints PLAN OK, commit it as `m0: configure`.

## Run
The launcher is `supervise.py`; never start drive.py for a build by hand. In a POSIX shell (Git Bash on Windows): `mkdir -p R/.overhaul; py -3 <skill>/scripts/supervise.py --repo R > R/.overhaul/supervise.out 2>&1; echo "supervise exit $?"; tail -n 5 R/.overhaul/supervise.log`
It caps itself and every child at `[limits] proc_mem_gb`, runs drive.py as its child, handles each exit itself and logs one line per step to `.overhaul/supervise.log`; it writes nothing outside `.overhaul/`. One drive per repo: both take `.overhaul/drive.lock`, so a second start exits at once naming the running pid, and `--reset`/`--mark-proven`/`--repolish` are refused while another drive runs. To stop a build, `drive.py --repo R --stop` (kills builders too); never start a second one beside it.
- Interactive: run it as one background command, then one long wait for its exit. Never poll, never read its logs while it runs; a still-running notice gets no tool call. Asked for status meanwhile, copy the VERDICT line from `.overhaul/verdict.json`; never run verdict.py by hand (it compares against its own last run).
- Headless: run it directly, in the foreground. A foreground call is cut at 10 minutes unless the headless session starts with `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS` above the run (14400000 is 4 hours).
- While drive runs, the harness handles failures itself: `router.py` classes each failed attempt and routes it (a plan class to a plan-fix agent, a flake held or not charged), `feasibility.py` parks an unbuildable row before its first build, and supervise.py runs live re-plan rounds for needs-plan rows, critical path first. verdict.py's `failures` line names a class over 30% of the last hour: that class is the bottleneck to fix, not each function.
- drive exit 0: when every non-deferred row is proven, supervise.py runs the blind proof-check (below) and stops; never while functions remain.
- drive exit 3: after any live round in flight finishes, one Exit-3 round (below), then drive.py again; at most `--max-rounds` rounds (default 12). An exit 3 that names no function stops supervise.py with drive's line: `main red in plan stubs` (make the stubs pass in a plan commit), `systemic <reason>` (fix the shared cause: brief, gate or plan, not each function) or `main red` (fix main in a plan commit); then start supervise.py again.
- Any other drive exit: drive.py once more; the same failure twice stops with its error line (a harness defect: report it, never do the harness's work by hand).
- supervise exit 0: every function proven and REAL; report. 4: the proof-check found some not REAL (Exit 0). 3: rounds used up; the needs-plan ids are the deciding item. 1: the last line of supervise.log is the error.
- On the owner's stop: stop the command (TaskStop) and say so in one line; the next run resumes from state.json.

## Polish and gates (inside drive.py)
drive.py runs review, polish, phase audits, parallel lands, remote builders, stacked starts, periodic gates and the disk and memory limits by itself; each mechanism and its `[harness]`, `[limits]` and `[gates.periodic]` keys are in references/harness.md. You need only this:
- Each function gets one blind review against the fixed checklist (references/checklist.md); a verifier must reproduce each blocking NO, so an unreproduced NO never blocks.
- Phase barrier: no phase N+1 function starts while a phase <=N one is unproven; a finished phase gets its polish and arch audit before the next starts.
- Once every function is proven, polish runs the checklist plus a dedup role over the whole tree, at most 2 fix rounds; a round that raises the verified count is reverted.
- VERDICT GOOD needs every function proven, the gates green, 0 verified P0 or P1, and each module at its mutation floor when probes ran; nits and the open-audit trend never gate.
- A red periodic gate bisects to its culprit land; an unavailable gate or a `gate_config` line is an owner item.
- A memory-cap failure reads `process memory cap N GB hit`: shrink the fixture, never raise the cap. Disk under `min_free_gb` stops new builds (`disk_low`).
- A restart keeps work: builders run detached and the next drive re-adopts them. Restart for a code or config fix: `--stop --keep-builders`, then start supervise again.

## Exit 3: needs-plan functions
drive.py exits 3 when nothing is READY and some function is needs-plan: its retries are used up, its brief is over budget, or it cannot land. supervise.py runs the round: every needs-plan function at once (up to `[harness] plan_fix_parallel`) gets a `claude -p --model opus` plan-fix agent (`templates/plan-fix-brief.md`) on `plan/fix-<id>`; one integrator merges the branches, supervise.py checks PLAN OK, fast-forwards main and resets each fixed id. Each agent reads only the function's row, its review reason (`.overhaul/reviews/<id>.txt`) and its last events (`grep <id> .overhaul/log.jsonl | tail -n 3`), never its diff, and makes one plan fix by overhaul-plan's rules:
1. Split it, when the brief was over budget, the work spans modules, or the router classed it oversized (an oversized function counts as fixed only as a split [by router.split_ok]): new rows, each with one module, one entry and its own acceptance test that fails now, one function or one verb behaviour in at most 3 files with a work packet on its ARCHITECTURE bullet (overhaul-plan Step 4) [by plan_validate.py]; deps keep the phase order.
2. Fix its acceptance, when the test cannot go red before and green after through the public entry (it already passes on main, reaches an internal, or is flaky).
3. Fix the plan, when ARCHITECTURE.md blocks it (a missing dep, a cycle, a public API the function cannot reach).
Change nothing else: no other function's row or test, no re-cut of modules or phases, no raised limit, no loosened check, no edit to harness state. Then plan_validate must print PLAN OK; commit `plan: <id> split|acceptance|architecture: <why>`. A function at needs-plan again after two plan fixes: stop and report it as the deciding item (a third `--reset` is refused). Commit every plan fix: drive.py refuses to start on uncommitted tracked changes.

State changes go only through drive.py's own commands, never by editing `.overhaul/state.json`; each exits at once:
- `py -3 <skill>/scripts/drive.py --repo R --reset <id>`: the function goes back to planned with attempts 0, a fresh start after your plan fix.
- `py -3 <skill>/scripts/drive.py --repo R --mark-proven <id> --evidence <file>`: the function is marked proven, only when main already satisfies it; `<file>` is the non-empty blind review that backs it. The proof-check at the end still has to find it REAL.

## Exit 0: blind proof-check
supervise.py launches one Opus agent (`claude -p --model opus`) whose whole prompt is `Read and do <skill>/references/proof-check.md for repo R.`; a manager without supervise.py uses the Agent tool, in the background. It reads the plan and the code on main only, never `.overhaul/`, a review or this chat, and writes `.overhaul/proof-check.txt`; read its line 1 and its non-REAL lines only. UNCHECKED functions get one more pass with the prompt ending `, only <ids>.` (supervise.py does it). A PARTIAL or STAND-IN function goes back through Exit 3 option 2: its acceptance is rewritten to drive the real entry and to fail on the current code, its row returns to planned, PLAN OK, and drive.py runs again. The build is finished when the proof-check finds every function REAL; report then, and launch nothing more.

## Reporting
Four lines at most, at each drive.py exit and at the finish:
1. The VERDICT line, copied from verdict.py's output (`.overhaul/verdict.json`), never composed.
2. At the finish, the PROOF line; otherwise what runs now and since when.
3. The one item that decides what happens next, only if there is one.
4. The dashboard: its Artifact link, or `R/dash/index.html`.
Times come from `date` just before writing, in the owner's local time with its zone; durations from `.overhaul/log.jsonl`. Never estimate a time, a count or a rate.

## Rules
- Only drive.py launches builders and reviewers: one function per builder, in worktree `.overhaul/wt/<id>` on branch `fn/<id>`, with a brief it generates; supervise.py launches only the plan-fix agents, the integrator and the proof-check [by drive.py; supervise.py].
- Only land.py changes main's code, one branch at a time under a lock, fast-forward only; the manager commits only plan data and configuration, each after PLAN OK [by land.py; plan_validate.py; the guard/reference-transaction hook drive.py installs].
- One capability index: `spec/index/<module>.md` and `spec/INDEX.md`, generated, never hand-edited; a new function matching an indexed one is sent back naming `reuse <path::name>` and refused at land [by index_api.py; land.py; drive.py's pre-review gate].
- The whole-tree checks (test, lint, dup, cycles, entry_lint) are green on main [by land.py (all five before every fast-forward); verdict.py (a red main is BAD)].
- Acceptance drives only public entries (a CLI by subprocess, HTTP through a started server, UI through the page), with no mocks, fakes or internal imports; it is red on main before the merge and green after, and every proven function's acceptance stays green [by entry_lint; land.py].
- Module dependencies have no cycles [by check_cycles, in plan_validate.py and in every landing].
- A brief stays under brief_max_tokens; one over it is a plan problem, never trimmed by hand [by drive.py (refuses it; the function goes needs-plan)].
- A retry continues the failed attempt: its brief starts with the last failure and diff stat, and fn/<id> keeps its commits [by drive.py (start_build, keep_branch)].
- Retries are limited to retries_per_function; then the function is needs-plan, and a cycle with nothing to do exits instead of spinning [by drive.py (attempts in state.json; exit 3)].
- Functions of one module never build at once; a merge conflict at landing gets a conflict-fix pass, not an attempt [by drive.py (templates/conflict-brief.md)].
- Builders run on builder_model (default opus), reviewers on review_model (default sonnet), and every event names its model [by drive.py (claude --model)].
- Every claude worker starts lean: no user CLAUDE.md, memory, skills or MCP; project rules reach it only through its brief [by worker.py].
- A worker silent for worker_idle_min is killed and logged as a `timeout` naming its last tool [by drive.py (stream-json)].
- Builders stay in their allowed paths and never edit tests/acceptance, stand in for real behaviour or special-case a test [by drive.py; write_guard.py; the blind review].
- No plan commit moves main until `plan_land.py --repo R --branch B` is green, and main moves only through `plan_land.py --ff` [by plan_land.py; supervise.py].
- Build invariants are checked every 5 min, not at the manager's heartbeat; what the watchdog cannot fix goes to `.overhaul/ALERT.txt`, which the manager reads each heartbeat [by watchdog.py, a thread of supervise.py].
- A function counts only when land.py proved it on main and the proof-check finds it REAL [by land.py; the proof-check].
- The dashboard is dash.py's output, never hand-made or edited; publish `dash/index.html` as an Artifact (same URL each exit) only if the Artifact tool exists, else give its path [by dash.py, run by drive.py every cycle].
- A whole-tree check that fails on clean main too is never a function's attempt: new builds and lands hold and main gets one fix; repeated failures of one reason are systemic [by drive.py via mainred.py; verdict.py].

## Scaling
Big projects: more parallel builders and more machines keep every contract; how, in references/harness.md, Scaling.
