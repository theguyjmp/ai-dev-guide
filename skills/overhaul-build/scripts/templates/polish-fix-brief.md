# Polish fix {{id}} round {{round}}

You fix code-quality issues a blind audit found in module `{{module}}`. Every function is already proven;
your change must keep every acceptance test green and change no behaviour.

- Module: `{{module}}`
- Branch: `{{branch}}` (your working directory is its worktree)
- Budget: about {{max_calls}} tool calls. Commit your work on the branch before you stop.

## Issues to fix

{{issues}}

## You may edit only

{{allowed_paths}}

Never edit tests/acceptance, spec/ or overhaul.toml.

## Policies

{{policies}}

## Commands (run from the worktree)

{{commands}}

Fix each issue at its cause, run the commands until they pass, commit, and end with one line: DONE or
STUCK <reason>.
