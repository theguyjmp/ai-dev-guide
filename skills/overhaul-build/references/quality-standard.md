# Quality standard

One fixed bar for every step: the plan's Policies cite it, each builder brief and review cites the rules for its
function, the polish audit and the final judge audit against it. A finding cites a rule number (`Q12`). A defect
no rule covers is a NEW-RULE candidate: report it separately, and add a rule here so the bar grows, never moves.
Each rule is a check, not a taste: "Check" says how to measure it.

## 1 Structure

- Q1. The module graph is acyclic. Check: `check_cycles.py --repo .` is clean.
- Q2. Code outside a module imports only its public API (`__init__`/`__all__`, `pub`, `export`). Check: grep
  cross-module imports for private submodules or `_names`.
- Q3. Each domain rule has one owner (a limit, a status test such as "is paid", a tax or total formula, a
  validation). Every other module calls that owner. Check: grep the rule's literal or condition; more than one
  definition fails.
- Q4. A lower layer knows nothing of a higher one: no schema keys, messages or types of callers hard-coded in a
  core module. Check: grep the core module for names owned elsewhere.
- Q5. No private library API in signatures or imports (`argparse._SubParsersAction`, `_internal`). Check: grep
  `\._[A-Za-z]` in imports and annotations.
- Q6. Domain logic does not live in an interface module (CLI, HTTP handler, view). Check: arithmetic or rules in
  `cli`/`server` files that a test of the domain cannot reach.

## 2 Duplication

- Q7. No copied block of 4 or more lines. Check: `pylint --disable=all --enable=duplicate-code
  --min-similarity-lines=4 src` (or the language's equivalent) is clean.
- Q8. No re-implemented helper: one function per computation (totals, plurals, formatting, row validation,
  parsing). Check: for each helper, grep its core expression; a second copy fails.
- Q9. No two types with the same fields, and no inverse helper that undoes another's output (strip what a
  formatter added). Check: compare dataclass/struct field lists; grep callers that post-process a helper.
- Q10. Each user message and literal is defined once as a constant; the same meaning has one text. Check:
  `dup_literals.py`, and grep near-identical messages.
- Q11. No magic tuple index or repeated `f(x)[n]`; return a named type. Check: grep `)\[[0-9]\]`.

## 3 Lint

- Q12. The configured linters are clean, plus `ruff check --select E,F,W,B,SIM,UP,C90 --line-length 88` (or the
  language's strict set). Check: run them.
- Q13. No dead code: no unused parameter, attribute, import, branch or fallback that cannot run. Check: AST census
  of unused names; trace each `or`/`else` fallback to a reachable input.
- Q14. No shadowed builtins; every public function has type annotations. Check: ruff `A`, `ANN`.
- Q15. Module constants sit at the top of the module. Check: AST, no top-level assignment after the first def.

## 4 Size

- Q16. No function over 30 lines or complexity over 10. Check: AST census.
- Q17. A function does one job: no look-up, validate, merge and save in one body. Check: a function that both
  parses input and writes a store fails.

## 5 Readability and error handling

- Q18. Every input is validated at the boundary where it enters (CLI argument, HTTP header and body, file, env):
  type, range, sign and length. Check: feed negative, zero, huge, empty, wrong-type and non-UTF-8 values to each
  entry; each gives a user error, never a traceback or silent acceptance.
- Q19. Every OSError, decode error (JSON, Unicode), RecursionError, ValueError and overflow that user input or a
  file can cause is mapped to the project's user error with a stable message and exit code. Check: point each
  path at a directory, a missing drive, a reserved name (`con`), a deeply nested file, a bad date; zero
  tracebacks.
- Q20. Errors are chosen by type, never by message text: no `startswith`/`in` on an error string to pick a status
  or branch. Check: grep `str(e)`, `.args[0]`, `message.startswith`.
- Q21. No broad `except` (`except Exception`, bare `except`) except at the one top-level handler, which logs and
  maps. Check: AST census.
- Q22. No round trip through another type (`parse(str(n))`), no needless re-sort or re-read. Check: grep.
- Q23. Every stored record is checked on read against its full schema: types, required fields, value ranges,
  date formats, enums. A file with the right shape but bad values is a corrupt-data error. Check: hand-edit each
  field of each store to a wrong type, a negative, a bad date; each is reported as corrupt.

## 6 Tests

- Q24. Acceptance tests drive the exact public entry their FUNCTIONS row names. Check: `entry_lint.py` and
  `plan_validate.py` are clean.
- Q25. Tests are mutation-resistant: every comparison, boundary and branch is pinned. Check: about 18 hand-made
  mutants (flip `<`/`<=`, drop a validation, change a constant); every one is killed.
- Q26. Every rule in this standard that the code relies on has a test that fails without it (each Q18, Q19, Q23
  case, each invariant of Q29 to Q31). Check: list the rule cases; each has a test.
- Q27. One shared fixture (`conftest.py` or the language's equivalent); no fixture copied into test files. Check:
  grep fixture definitions; more than one copy fails.
- Q28. The suite is deterministic: no order dependence, real clock, shared temp path or port. Check: run it 5
  times in a fresh copy and in random order; all pass.

## 7 Concurrency and data safety

- Q29. Stored ids are unique, and a counter such as `next_id` is greater than every stored id; a read that breaks
  either is a corrupt-data error. Check: hand-write duplicate ids and a low counter; the next write must not
  overwrite or drop a record.
- Q30. Stored totals obey the same limits as inputs (a cap on one add also caps the stored sum); no value can be
  stored that the program cannot later read or change. Check: repeat the largest add; it fails at the cap.
- Q31. Stored text has one encoding constant, and every write is atomic (temp file, flush, fsync, replace). Check:
  grep literal encodings and plain `open(..., "w")` on a store.
- Q32. An operation that changes more than one store is one atomic unit (one file, or a journal), or its writes are
  ordered so a crash leaves a state the next run detects and repairs, and that recovery is documented and tested.
  Check: fail the second write; the stores must not disagree and a retry must not apply the first change twice.
- Q33. Every read-modify-write holds the store lock for the whole operation, in-process and across processes.
  Check: 1, 2, 4 and 8 concurrent clients on each write path; status codes and stored counts are exact.
- Q34. A stale lock is recovered: the lock records its owner (PID, start time) and a lock whose owner is gone or
  that is older than a bound is taken over. Lock waits back off and give up with a user error; a non-contention
  OSError is not retried forever. Check: kill a process holding the lock; the next command succeeds.
- Q35. Every network read has a timeout and a size cap: socket timeout, max body, `Content-Length` checked for
  sign, format and maximum; the number of open connections is bounded. Check: send headers and no body, a
  negative and a huge length, 200 stalled connections; each is closed or refused within the timeout.
- Q36. Exported data is safe for its reader: CSV cells starting with `= + - @` are escaped, and the export file is
  written atomically. Check: a customer named `=1+1` exports as text.
