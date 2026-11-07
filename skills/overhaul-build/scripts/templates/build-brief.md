# Build {{id}} (attempt {{attempt}})

You build ONE function of the rebuild. You work alone in a git worktree and hand back 2 lines.

- Worktree (your working directory): `{{worktree}}`
- Branch (already checked out, from main): `{{branch}}`
- Module: `{{module}}`
- Public entry: {{entry}}

## The function

{{row}}

## The module (from spec/ARCHITECTURE.md)

`````markdown
{{module_section}}
`````

## Allowed paths

You may create or edit ONLY these paths. Every other file is off limits.

{{allowed_paths}}

Never edit `tests/acceptance/`, `spec/` or `overhaul.toml`. They are the contract. A change to any of them
fails the build on sight, and so does any file outside the allowed paths.
land.py also refuses any branch that edits them, so such an edit can never reach main.

## Acceptance test (read-only, already on main, currently FAILING)

{{acceptance_source}}

## Previous attempt

{{previous}}

## Commands (run from `{{worktree}}`; each must exit 0)

{{commands}}

## What to do

1. Implement the function inside the allowed paths so the acceptance test above passes through the REAL entry,
   {{entry}}. The test drives only the public entry, so build the real behavior behind it.
2. Never make the test pass by special-casing it: no branching on test names, fixtures, paths or inputs the
   test happens to use, no hard-coded expected outputs, no stand-ins, stubs, mocks or fakes in product code.
   A blind reviewer reads your diff and fails any of these.
3. Add unit tests for the new code under `tests/unit/` (inside the allowed unit-test path above). They test the
   module's own behavior and must pass.
4. Keep the code readable and free of duplication: reuse what the module and its allowed dependencies already
   offer instead of copying it.
5. Run the commands above and make them all pass. Do not stop while one fails.
6. If the acceptance test is already green before you change anything, change nothing, commit nothing and say
   "acceptance already green before change" in your final message.
7. Commit all your work on `{{branch}}` in `{{worktree}}` (`git add -A` then `git commit`). Do not push, do not
   merge, do not switch branches, do not touch any other worktree or the main checkout.

## Budget

Stop at about {{max_calls}} tool calls. Work in small steps and commit as you go. Keep tool output small
(tail or grep long logs, no whole-file dumps). If the same step fails twice, stop and report that instead of
repeating it.

## Final message

Exactly 2 lines, nothing else:

1. `DONE {{id}}` or `BLOCKED {{id}}`, then the commit sha of your last commit.
2. One sentence: what you built, or the single reason you are blocked.
