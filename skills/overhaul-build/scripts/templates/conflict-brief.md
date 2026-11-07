# Resolve conflict {{id}} (pass {{pass}})

main moved while this function was built, so land.py could not merge it into this branch. You fix only that.
You work alone in the function's own git worktree and hand back 2 lines. Do not rebuild the function.

- Worktree (your working directory): `{{worktree}}`
- Branch (already checked out, holds the finished and reviewed work): `{{branch}}`
- Merge into it: `{{main}}`
- Module: `{{module}}`

## Why you are here

land.py refused the branch: `{{reason}}`

## The function (its behaviour must survive the merge)

{{row}}

## Allowed paths

The conflicts are inside these paths. Edit only conflicted files, and only these paths.

{{allowed_paths}}

Never edit `tests/acceptance/`, `spec/` or `overhaul.toml`. If the merge brings in {{main}}'s version of them,
keep {{main}}'s version exactly.

## What to do

1. In `{{worktree}}` run `git merge {{main}}`. It stops on the conflicts; `git status` lists them.
2. Resolve every conflict so BOTH survive: what {{main}} added or changed since this branch started, and what
   this function does. Keep both sides' code and tests. Never drop one side to make a conflict go away, and
   leave no conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) in any file.
3. Run the commands below and make them all pass on the merged tree. Do not stop while one fails.
4. Commit the merge on `{{branch}}` (`git add -A` then `git commit`). Do not push, do not switch branches, do not
   touch any other worktree or the main checkout.

## Commands (run from `{{worktree}}`; each must exit 0)

{{commands}}

## Budget

Stop at about {{max_calls}} tool calls. Keep tool output small (tail or grep long logs, no whole-file dumps).
If the same step fails twice, stop and report that instead of repeating it.

## Final message

Exactly 2 lines, nothing else:

1. `DONE {{id}}` or `BLOCKED {{id}}`, then the commit sha of your last commit.
2. One sentence: how you resolved the conflicts, or the single reason you are blocked.
