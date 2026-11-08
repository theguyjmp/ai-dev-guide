# Polish fix {{id}} round {{round}}

You fix the verified checklist NOs (each reproduced by a verifier) found in module `{{module}}`. Every function is already proven;
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

Fix every issue in the list at its cause in this one pass, run the commands until they pass, commit, and end with one line: DONE or
STUCK <reason>.
