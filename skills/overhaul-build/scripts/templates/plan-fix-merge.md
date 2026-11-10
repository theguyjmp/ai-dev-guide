# Integrate the plan fixes of round {{round}} into ONE plan branch

supervise.py ran one plan-fix agent per needs-plan function, in parallel; each left one or more `plan:` commits on its
own branch from `{{main}}`. You merge them into one branch. supervise.py fast-forwards `{{main}}` to it afterwards, runs
`plan_validate --run` once and resets the fixed functions; you do none of that.

- Worktree (your working directory): `{{wt}}`
- Branch (already checked out, at `{{main}}`): `{{branch}}`
- Live repo, read only: `{{repo}}` (never edit its checkout or `.overhaul/`)
- Phase size cap: `[plan] max_phase_size` = {{max_phase}} (never raise it)
- Report: `{{report}}`

Branches to merge, in this order (each with its reserved ids and its agent's report):
{{branches}}

Cap 60 tool calls; small output. Run no cargo, product binary or test. The same step fails twice: stop and write the
report.

1. Cherry-pick each branch's commits (`git cherry-pick {{main}}..<branch>`) onto your branch, in the order above. A
   branch whose report line 1 does not say PLAN OK, or that has no commit, is skipped (say so). Never `git merge`: every
   commit on `{{main}}` must keep a subject starting with `plan` (main's guard refuses anything else).
2. Resolve conflicts in spec/FUNCTIONS.md, spec/ARCHITECTURE.md, decision records and roadmap notes by keeping every
   branch's change. The new ids were reserved per agent, so they never collide; if two branches still added the same
   id or decision number, renumber the later one everywhere it appears (rows, deps, test files and test names, the
   acceptance column, notes).
3. Phase cap: a phase over {{max_phase}} rows sends new rows to the next phase together with every row of that phase
   that depends on them, so deps stay in phase order. Move nothing else.
4. Change nothing beyond what the branches changed, the renumbering and these phase moves. After each branch,
   `py -3 {{validate}} --repo {{wt}}` (no `--run`); end with PLAN OK. Commit any adjustment as
   `plan: round {{round}} merge: <what moved or was renumbered>`. Do not push and do not touch `{{main}}`.

Write the report: line 1 `PLAN-MERGE round {{round}} <PLAN OK|PLAN FAIL ...> merged <ids> skipped <ids or ->`, then the
id map (old -> new) if anything was renumbered, the phase moves, and the branch sha. Hand back that line 1 only.
