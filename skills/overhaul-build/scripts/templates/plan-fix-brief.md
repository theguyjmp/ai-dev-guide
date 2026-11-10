# Plan fix {{id}} (Exit 3, round {{round}})

drive.py parked `{{id}}` as needs-plan. You make ONE plan fix for it, by the rules in `{{skill}}/SKILL.md` section
"Exit 3: needs-plan functions" (read that section only) and overhaul-plan's row rules (`{{plan_skill}}/SKILL.md`, only
the parts on rows, acceptance and ARCHITECTURE). supervise.py started you; other plan-fix agents run at the same time,
one per needs-plan function, and one integrator merges every branch afterwards.

- Worktree (your working directory): `{{wt}}`
- Branch (already checked out, at `{{main}}`): `{{branch}}`
- Live repo, read only: `{{repo}}` (never edit its checkout or `.overhaul/`)
- New ids reserved for you: {{ids}}
- Plan fixes this function already had: {{resets}}
- drive.py's last reason: {{reason}}
- Report: `{{report}}`

You are not the manager: never run drive.py, `--reset`, land.py, a merge or a push, and never switch branches. Cap 40
tool calls; small output (grep, tail, line ranges; no file over ~300 lines whole). Run no cargo, product binary or test:
the supervisor runs every test once after the merge. The same step fails twice: stop and write the report.

1. Read only: the row of `{{id}}` in `{{functions}}`, `{{repo}}/.overhaul/reviews/{{id}}.txt` (if present),
   `grep '"{{id}}"' {{repo}}/.overhaul/log.jsonl | tail -n 5`, and the acceptance test file the row names. Never a diff.
2. Decide the one fix that removes the cause:
   (1) split: new rows, each one module, one entry and its own acceptance test that fails now. Use only the ids reserved
       above (they are unique across every agent of this round), keep deps in phase order, and turn the old row into
       `deferred` with a note naming its replacements;
   (2) fix its acceptance: it cannot go red before and green after through the public entry (already green on main,
       reaches an internal, flaky, wrong fixture);
   (3) fix the plan: ARCHITECTURE.md blocks it (a missing dep, a public API it cannot reach, a file its builder must
       touch outside its `files` cell: add that file to the cell).
   {{mode}}
3. Change nothing else: no other function's row or test, no raised limit or budget, no loosened check. Product code is
   never edited; a new module or wiring a builder cannot reach goes into the row's `files` cell instead. A decision
   record you add takes the next free number on `{{main}}`; the integrator renumbers clashes.
4. `py -3 {{validate}} --repo {{wt}}` (no `--run`) prints PLAN OK. Commit on your branch with the subject
   `plan: {{id}} split|acceptance|architecture: <why>` (the subject must start with `plan`: main's guard lets only plan
   commits through). Do not push, do not merge.

Write the report: line 1 `PLAN-FIX {{id}} <split|acceptance|architecture|none> <PLAN OK|PLAN FAIL ...>`, then the cause
in one line, the rows, tests and files changed, the new ids used, and the commit sha. Hand back that line 1 only.
