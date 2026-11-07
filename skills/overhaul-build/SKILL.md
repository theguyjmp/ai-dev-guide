---
name: overhaul-build
description: Start, run or resume the build of a rebuild planned by overhaul-plan. A deterministic harness (drive.py) builds, blind-reviews and lands one function at a time until every function is proven through its public entry; the main chat or a headless manager only supervises it, fixes needs-plan functions through plan_validate, runs a blind proof-check at the end and reports. Use when the owner says to start, run or resume the overhaul build.
---

# overhaul-build

You are the manager: the main chat, or `claude -p` when headless; never a subagent (if another agent started you, stop and say so). You only supervise the harness and handle its exceptions. `drive.py` does every routine job: it picks the ready functions, generates each brief, runs builders and blind reviewers as subprocesses in their own worktrees, and hands each PASS to `land.py`, which alone changes main. You start it, wait for it, handle what it hands back, and report. You write no product code, brief or review, read no diff, launch no builder or reviewer, and never hand-edit harness state (`.overhaul/`: state, briefs, reviews, logs). Keep your context small: tails and single lines, no whole-file reads, no logs while drive.py runs; never ask the owner to compact or open a fresh session. The owner is asked nothing routine; only money, deleting outside the repo, publishing publicly or an account change needs a yes.

The plan is the spec, written by overhaul-plan: `spec/FUNCTIONS.md` (`| id | phase | module | deps | entry | acceptance | status |`), `spec/ARCHITECTURE.md` (modules, public APIs, allowed deps), `tests/acceptance/` (written at plan time, red until built) and `overhaul.toml` (commands, limits, paths). Contracts: `skills/OVERHAUL-V2-DESIGN.md` in ai-dev-guide. Why each rule exists: CHANGELOG.md. The v1 the pilot project machinery, history only: references/legacy.md.

Paths: `R` is the rebuild repo; `<skill>` is this skill's folder (normally `$HOME/.claude/skills/overhaul-build`); `<plan>` is overhaul-plan's.

## Preflight
Every start or resume, in at most 15 tool calls; an interactive turn ends with drive.py running.
1. Skills, once a day: `py -3 $HOME/.claude/skills/dev-setup/scripts/check_skills.py`; on UPDATED, re-read this file.
2. R is on main with nothing uncommitted outside `.overhaul/` (`git -C R status --porcelain`).
3. The tool behind every `overhaul.toml` command is installed. Python: `py -3 -m pip install ruff pylint pytest` for any missing (`py -3 -m ruff --version` and so on); JS: `npm i -D eslint jscpd`; Rust: `rustup component add clippy`. `claude --version` works, since drive.py launches builders with it.
4. `py -3 <plan>/scripts/plan_validate.py --repo R --run` prints PLAN OK. Anything else is a plan defect: fix it as in Exit 3 until it does. drive.py never starts on a failing plan.
5. No `.overhaul/state.json` yet: do M0 once. Otherwise drive.py resumes from it.
6. Run.

## M0: configure, build no machinery
The harness is the machinery. M0 adds no scheduler entry, bot, hook, guard, wait script, status file or home-grown lint; a missing check is a standard tool named in overhaul.toml.
1. overhaul.toml names the stack's standard tools (Python: ruff, pylint duplicate-code, pytest; Rust: clippy, cargo test, a cycle check from `cargo metadata`; JS: eslint, jscpd, node --test), and every `<skill>` in it resolves to an installed path.
2. `.gitignore` covers the scratch drive.py writes under `.overhaul/`, its builder worktrees above all, so the whole-tree checks never scan them.
3. test, lint, dup, cycles and entry_lint each exit 0 on main. A configuration failure (a missing tool, a wrong path) is fixed in overhaul.toml; a real finding is fixed now, by the tool's own fixer (`ruff check --fix`) or a plan fix. Never loosen a check or a limit to get green.
4. Once plan_validate prints PLAN OK, commit it as `m0: configure`.

## Run
In a POSIX shell (Git Bash on Windows): `mkdir -p R/.overhaul; py -3 <skill>/scripts/drive.py --repo R > R/.overhaul/drive.out 2>&1; echo "drive exit $?"; tail -n 5 R/.overhaul/drive.out`
- Interactive: run drive.py as one background command, then make one long wait for its exit. Never poll, never read drive.out or `.overhaul/log.jsonl` while it runs; a still-running notice gets no tool call. Asked for status meanwhile, copy the VERDICT line from `.overhaul/verdict.json`; never run verdict.py by hand (it compares against its own last run).
- Headless: run drive.py directly, in the foreground. A foreground call is cut at 10 minutes unless the headless session starts with `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS` above the run (14400000 is 4 hours).
- Exit 0: every function is proven. Run the proof-check, then report.
- Exit 3: the manager is needed (below); after the fixes, run again.
- Any other exit: run once more; the same failure twice is a harness defect. Report its last error line and stop; never do the harness's work by hand.
- On the owner's stop: stop the command (TaskStop) and say so in one line; the next run resumes from state.json.

## Exit 3: needs-plan functions
drive.py exits 3 when nothing is READY and some function is needs-plan: its retries are used up, its brief is over budget, or it cannot land. drive.py has stopped, so nothing races your edits. For each such function read only its row, its review reason (`.overhaul/reviews/<id>.txt`) and its last events (`grep <id> .overhaul/log.jsonl | tail -n 3`), never its diff, and make one plan fix by overhaul-plan's rules:
1. Split it, when the brief was over budget or the work spans modules: new rows, each with one module, one entry and its own acceptance test that fails now; deps keep the phase order.
2. Fix its acceptance, when the test cannot go red before and green after through the public entry (it already passes on main, reaches an internal, or is flaky).
3. Fix the plan, when ARCHITECTURE.md blocks it (a missing dep, a cycle, a public API the function cannot reach).
Change nothing else: no other function's row or test, no re-cut of modules or phases, no raised limit, no loosened check, no edit to harness state. Then plan_validate must print PLAN OK; commit `plan: <id> split|acceptance|architecture: <why>` and run again. If drive.py does not pick the fixed function up, that is a harness defect: report it. A function at needs-plan again after two plan fixes: stop and report it as the deciding item.

State changes go only through drive.py's own commands, never by editing `.overhaul/state.json`; each exits at once:
- `py -3 <skill>/scripts/drive.py --repo R --reset <id>`: the function goes back to planned with attempts 0, a fresh start after your plan fix.
- `py -3 <skill>/scripts/drive.py --repo R --mark-proven <id> --evidence <file>`: the function is marked proven, only when main already satisfies it; `<file>` is the non-empty blind review that backs it. The proof-check at the end still has to find it REAL.

## Exit 0: blind proof-check
Launch one Opus agent whose whole prompt is `Read and do <skill>/references/proof-check.md for repo R.` (interactive: the Agent tool, in the background; headless: `claude -p --model opus --dangerously-skip-permissions`). It reads the plan and the code on main only, never `.overhaul/`, a review or this chat, and writes `.overhaul/proof-check.txt`; read its line 1 and its non-REAL lines only. UNCHECKED functions get one more pass with the prompt ending `, only <ids>.`. A PARTIAL or STAND-IN function goes back through Exit 3 option 2: its acceptance is rewritten to drive the real entry and to fail on the current code, its row returns to planned, PLAN OK, and drive.py runs again. The build is finished when the proof-check finds every function REAL; report then, and launch nothing more.

## Reporting
Four lines at most, at each drive.py exit and at the finish:
1. The VERDICT line, copied from verdict.py's output (`.overhaul/verdict.json`), never composed.
2. At the finish, the PROOF line; otherwise what runs now and since when.
3. The one item that decides what happens next, only if there is one.
4. The dashboard: its Artifact link, or `R/dash/index.html`.
Read every time from the clock (`date`) just before writing it, in the owner's local time with its zone (9:45 PM CST); durations come from `.overhaul/log.jsonl` timestamps. Never estimate a time, a count or a rate.

## Rules
- Only drive.py launches builders and reviewers: one function per builder, in worktree `.overhaul/wt/<id>` on branch `fn/<id>`, with a brief it generates; the manager launches only the proof-check [by drive.py].
- Only land.py changes main's code: one branch at a time under a lock, main merged in, then a fast-forward, never a force-push; the manager commits only plan data and configuration, each after PLAN OK [by land.py; plan_validate.py].
- The whole-tree checks (test, lint, dup, cycles, entry_lint) are green on main [by land.py (all five on the whole merged tree before every fast-forward); verdict.py (a red main is BAD)].
- Acceptance drives only public entries (a CLI by subprocess, HTTP through a started server, UI through the page), with no mocks, fakes or internal imports; it is red on main before the merge and green after, and every proven function's acceptance stays green [by entry_lint; land.py].
- Module dependencies have no cycles [by check_cycles, in plan_validate.py and in every landing].
- A brief stays under brief_max_tokens; one over it is a plan problem, never trimmed by hand [by drive.py (refuses it; the function goes needs-plan)].
- Retries are limited to retries_per_function; then the function is needs-plan, and a cycle with nothing to do exits instead of spinning [by drive.py (attempts in state.json; exit 3)].
- Builders stay in their allowed paths and never edit tests/acceptance, stand in for real behaviour or special-case a test [by drive.py (allowed paths in each brief); the blind review (line 1 PASS or FAIL)].
- A function counts only when land.py proved it on main and the proof-check finds it REAL [by land.py; the proof-check].
- The dashboard is dash.py's output, never hand-made or edited; publish `dash/index.html` as an Artifact only if the Artifact tool exists (same file, same URL, at each drive.py exit), else give its path [by dash.py, run by drive.py every cycle].

## Scaling
Big projects keep every contract and scale two ways. More parallel builders: raise `max_parallel` while the VERDICT rate (proven per hour) rises with it; land.py lands one branch at a time, so builders past its landing rate only queue. More machines: each project's own commands in overhaul.toml may run anywhere (an ssh host, a build farm, CI) as long as each runs from the repo root and exits 0 for pass; drive.py, land.py, FUNCTIONS.md and the proof rule do not change.
