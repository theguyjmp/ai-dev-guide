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

## Policies (from spec/ARCHITECTURE.md; every module follows them)

`````markdown
{{policies}}
`````

## Quality rules for this function (from the quality standard `{{standard_path}}`)

The audit that ends the build checks these by number. Run each rule's Check on your change before you finish.

{{standard}}
{{project_rules}}

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

## Proven tests your change can break (run from `{{worktree}}`; each must exit 0 before DONE)

Already proven rows in your footprint: your module, every module that depends on it, and rows of modules
ARCHITECTURE.md does not list, picked the way land.py picks them. drive.py runs these same commands after your
DONE, before review; a red one fails the attempt with the test named, and land.py fails the land on it.

{{footprint}}

## Duplication check (the reviewer's top blocking item, C05)

Most failed reviews are C05: a helper, constant, message or 4+ line block that already exists elsewhere in the
tree. Run these on your diff (`git diff --stat main...HEAD` lists your files) before DONE; fix every hit in
your files by importing the existing copy:

{{dup_check}}

## What to do

1. Implement the function inside the allowed paths so the acceptance test above passes through the REAL entry,
   {{entry}}. The test drives only the public entry, so build the real behavior behind it.
2. Never make the test pass by special-casing it: no branching on test names, fixtures, paths or inputs the
   test happens to use, no hard-coded expected outputs, no stand-ins, stubs, mocks or fakes in product code.
   A blind reviewer reads your diff and fails any of these.
3. Add unit tests for the new code under `tests/unit/` (inside the allowed unit-test path above). They test the
   module's own behavior and must pass. Rust: unit tests are an in-crate `#[cfg(test)] mod tests` in the
   function's own files instead.
4. Follow the Policies above. Shared state goes through the persistence owner's API (its lock and atomic
   write), and errors are raised, reported and logged the way the Errors policy says. If you write shared state
   that two requests or processes can reach at once, add a unit test that writes it from several threads and
   checks no write was lost.
5. No duplication. Before you write a helper, constant or message, search for one (`git grep -n "<words>"`) and
   import it through the owning module's public API (its `Public:` line or `__init__` exports). Never import a
   name from inside another module's package. A message or format string is written once, as a named constant.
6. Before DONE, all of these pass, in this order: (a) your own acceptance test and the Commands above, lint
   (line length included), dup and literals too; (b) every command under "Proven tests your change can break";
   (c) the Duplication check, with no hit left in your files. Do not stop while one fails. drive.py runs cycles,
   your acceptance and (b) itself before review: a failure there is a failed attempt. Once your module's package
   exists, every name on its Public line must exist in it. A proven test you cannot keep green without editing
   outside the allowed paths is a defect outside them: rule 9 (BLOCKED-BY), never a workaround.
7. If the acceptance test is already green before you change anything, change nothing, commit nothing and say
   "acceptance already green before change" in your final message.
8. Commit all your work on `{{branch}}` in `{{worktree}}` (`git add -A` then `git commit`). Then merge main
   into it (`git merge main`), resolve any conflict inside the allowed paths, and rerun the commands above and
   the proven tests until they pass, so a land conflict can only come from the seconds between. drive.py merges
   main and reruns the gate again before review. Do not push, do not switch branches, do not touch any other
   worktree or the main checkout.
   Code that calls a dependency still raising NotImplementedError (Rust: `todo!()` or `unimplemented!()`) builds
   against its stub's signature and docstring; do not implement another function's stub.
9. If the acceptance test, or a proven test of rule 6 (b), fails because code OUTSIDE the allowed paths is
   wrong (a landed dependency's bug, not a stub), do not work around it and do not edit it. Prove it with the failing test output, then end your
   final message with a third line `BLOCKED-BY: <path>:<line> <the input, the wrong result, the right result>`.
   drive.py sends that defect to the owning module's fix pass and rebuilds this function after it lands; the
   attempt is not counted.

## Budget

Stop at about {{max_calls}} tool calls. Work in small steps and commit as you go. Keep tool output small
(tail or grep long logs, no whole-file dumps). If the same step fails twice, stop and report that instead of
repeating it.

## Final message

Exactly 2 lines (3 with a BLOCKED-BY line, rule 9), nothing else:

1. `DONE {{id}}` or `BLOCKED {{id}}`, then the commit sha of your last commit.
2. One sentence: what you built, or the single reason you are blocked.
