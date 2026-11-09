# Review checklist v3

checklist-version: v3

Fixed yes/no items built from `quality-standard.md` (the `Q` numbers) plus the function's contract (`K` items).
Each is answered `yes`, `no` or `unknown`, always with `file:line` evidence. Blocking means P0 or P1. A NO blocks
only when a verifier reproduces it with a failing test or an exact line and input. Nits are capped at 5 per pass,
logged, never dispatched. Items change only between runs (bump the version); a finding class seen twice becomes an
item or a gate, never a fix pass. Each item line is `- <id> [<severity>] <Q rules> | <question> | <how to decide>`;
drive.py parses these lines, so keep the shape. `R` in the rules field asks the item only of a row whose `rules`
column cites project rules ([paths] patterns and standards). `W` marks a whole-tree item: the polish audit asks it,
a function review does not.

## Contract (every function review)

- K1 [P0] - | Does the change implement the row's behaviour through the entry the row names? | NO when the entry in the row is not the code path the diff adds, or the acceptance test can pass while the entry does something else. Evidence: entry file:line.
- K2 [P0] - | Is the behaviour real: no stub, mock, canned data, placeholder return, `TODO`, `pass` body, and no branch on test names, fixtures or the acceptance test's exact inputs? | NO with the file:line of the stub or the input-specific branch; reproduce with a second input the test does not use.
- K3 [P0] - | Is every changed file inside the allowed paths? | NO naming the file; outside-path needs go on `plan_gap`, not as a NO.
- K4 [P1] - | Does every error the row or Errors policy names reach the user as the policy's error, the same way as the module's other errors? | NO with the input that gives a traceback, a silent success or the wrong exit code.

## Structure

- C01 [P1] Q1 | Is the module graph still acyclic? | NO when `check_cycles.py` reports a cycle through a changed file.
- C02 [P1] Q2,Q5 | Does every cross-module import use only a public name (no `_name`, no private submodule, no private library API)? | NO with the import line.
- C03 [P1] Q3 | Does each domain rule the change touches (limit, status test, formula, validation) have one owner that others call? | NO with both definition lines (`git grep` the literal or condition).
- C04 [P1] Q4,Q6 | Is the change free of upward knowledge (caller schema keys, messages or types in a core module) and of domain logic in an interface module? | NO with the line and the owner it belongs to.

## Duplication (also the polish dedup role)

- C05 [P1] Q7,Q8 | Is every new helper or 4+ line block the only copy of its computation in the tree? | NO with both locations; `git grep` the core expression.
- C06 [nit] Q9 | Is there no second type with the same fields and no helper that undoes another's output? | NO with both definitions.
- C07 [P1] Q10 | Is each user message and literal defined once (same meaning, one text)? | NO with both lines, or `dup_literals.py` output.
- C08 [nit] Q11 | Is there no magic tuple index or repeated `f(x)[n]`? | NO with the line.

## Lint and size

- C09 [P1] Q12 | Are the configured linters and the strict set clean on the changed files? | NO with the linter line; reproduce by running it.
- C10 [P1] Q13 | Is there no dead code: unused parameter, import, attribute, unreachable branch or fallback? | NO with the line and why no input reaches it.
- C11 [nit] Q14,Q15 | Are builtins unshadowed, public functions annotated and module constants at the top? | NO with the line.
- C12 [P1] Q16,Q17 | Is every changed function 30 lines or fewer, complexity 10 or less, and one job? | NO with the def line and its length or its jobs.

## Errors and inputs

- C13 [P0] Q18 | Is every input validated where it enters (type, range, sign, length)? | NO with the entry and one bad input that is accepted or crashes.
- C14 [P0] Q19 | Is every OSError, decode error, RecursionError, ValueError and overflow user input can cause mapped to the user error? | NO with the input or path that gives a traceback.
- C15 [P1] Q20 | Are errors chosen by type, never by message text? | NO with the `str(e)`, `.args[0]` or `startswith` line.
- C16 [P1] Q21 | Is there no broad `except` outside the one top-level handler? | NO with the line.
- C17 [nit] Q22 | Is there no round trip through another type and no needless re-sort or re-read? | NO with the line.
- C18 [P0] Q23 | Is every stored record checked on read against its full schema? | NO with the field and the hand-edited value that is accepted.

## Tests

- C19 [P1] Q24 | Do the function's acceptance tests drive the exact public entry the row names? | NO with the test line and the entry it drives.
- C20 [P1] Q25,Q26 | Is each comparison, boundary, branch and relied-on rule pinned by a test that fails without it? | NO with the mutant (line and change) that survives.
- C21 [nit] Q27 | Is there one shared fixture, none copied into test files? | NO with both definitions.
- C22 [P1] Q28 | Is the suite deterministic (no order dependence, real clock, shared temp path or port)? | NO with the line and the run that differs.

## Data safety and concurrency

- C23 [P0] Q29,Q30 | Are stored ids unique, counters above every id, and stored totals within input limits? | NO with the store and the hand-written state that breaks it.
- C24 [P0] Q31 | Is every store write atomic (unique temp in the same dir, flush, fsync, replace) with one encoding constant? | NO with the `open(..., "w")` or fixed temp-name line.
- C25 [P0] Q32 | Is a change to more than one store one atomic unit or crash-ordered with tested recovery? | NO with the two writes and the crash point.
- C26 [P0] Q33 | Does every read-modify-write hold the store lock for the whole operation, through the Persistence owner's API? | NO with the unlocked load-modify-save; reproduce with concurrent writers.
- C27 [P1] Q34 | Is a stale lock recovered and a lock wait bounded? | NO with the lock code line and the killed-holder case.
- C28 [P0] Q35 | Does every network read have a timeout and a size cap? | NO with the read line and a stalled or oversized request.
- C29 [P1] Q36 | Are exported cells escaped and the export written atomically? | NO with the export line and a `=1+1` cell.

## Polish audit lessons (blind audits found these P0s past the items above)

- C31 [P0] Q33 | Is every product call of a low-level write primitive (file write, atomic replace, rename into place) inside the persistence owner's lock-held edit API, or on a short allowlist with the lock taken on the line before? | Grep the callers of the write primitives outside the owner's edit API; NO with each hit off the allowlist (a load-modify-save outside the lock loses a concurrent update).
- C32 [P0] Q35 | Does every server socket read (request line, headers, body) run under a read timeout, and does the stalled-body test declare a length above the HTTP library's in-memory pre-read buffer while checking another client is still served? | Read the library's buffer size; open a request that declares a body larger than it and sends nothing, then a health request must answer within 5 s. NO with the read line that has no timeout, or the stall test whose length fits the buffer.
- C33 [P1] Q36 | Does every verb that writes a user-named output path refuse an existing target unless an explicit overwrite flag is given, never overwrite a file in the product's own format, and compare same-file guards by canonical identity, not path text? | Point the output at an existing product document, also as `x/../name` and as an absolute path: each must be refused and the file left byte-identical. NO with the write line and the form that got through.
- C34 [P1] Q30 | Is every growing stored collection (journal, history, notes, log, any append-only list) capped so the largest storable record stays under the read/decode limit and an edit's cost does not grow with history? | Repeat the cheapest edit until the cap: the write must be refused before a read refuses the file, and an edit at the cap stays within 2x of one on an empty record. NO with the collection that has no cap.
- C35 [P1] Q19 | Does each OS error kind map to the user error of the same meaning (permission denied to permission, already exists or sharing violation to conflict, not found to not found), never all to one code, with the OS text kept out of the stable message? | Give each write verb a read-only file, a directory in place of the file and a locked file; check the error code and exit status. NO with the mapping line that folds kinds together.
- C36 [P1] Q35 | Do network error bodies (HTTP, RPC, MCP) leave out absolute host paths and raw OS error text, naming paths relative to the data directory? | Trigger a corrupt-store, a lock and an I/O error over the network, then grep each body for the data directory's absolute path. NO with the line that puts the path in the response.
- C37 [P1] Q12,Q16 W | Does every threshold in the lint configuration (complexity, function lines, arguments, line length) equal or tighten the standard's number? | Diff each lint config value (clippy.toml, ruff and the like) against the standard: Q16 says 30 lines and complexity 10. NO with the config line and the standard's number.

## Project rules (only for a row with a rules column entry)

- C30 [P1] R | Does each project rule the row cites (quoted under "Project rules this row cites") hold in the change, and does the acceptance test name and prove each pattern (P) id? | NO with the rule id and the file:line that breaks it, or the P id the acceptance test never exercises.
