Plan module `{module}` of the rebuild at `{rebuild}`: write its stubs, acceptance tests and property tests, nothing else.
Cap: 40 tool calls. Read only spec/ARCHITECTURE.md, tests/acceptance/_entry.py and tests/conftest.py; never read
the old project, other modules' files or other rows. Your rows of spec/FUNCTIONS.md:

{rows}

Write, for each row:
1. Stub: every file in its `files` cell, inside the module's Paths: the signatures, types and docstrings of what the
   row's entry needs, every body `raise NotImplementedError`. It must import (`py -3 -c "import {package}"`), so no
   import of a module that does not exist yet; a dep's API comes from ARCHITECTURE's Public line.
2. Acceptance: its `acceptance` test(s) in tests/acceptance/, named exactly as in the row, through `_entry.py` only:
   no product imports, mocks, fakes or monkeypatching. Own `tmp_path` data dir; assert what the user sees (output,
   exit code, HTTP body) plus at least one error case; read results back only through this entry or a dep's entry.
   More cases are `@pytest.mark.parametrize` cases of the one test. Fixtures come from tests/conftest.py; add none.
3. Property: tests/property/test_<id>.py with 1 to 3 Hypothesis `@given` tests through `_entry.py`, red now.

Never edit spec/, overhaul.toml, conftest.py, _entry.py or another module's files: report a needed change instead.
Check before handing back: `py -3 -m pytest --collect-only -q tests` exits 0, and your tests fail (the product is
missing), never error on a broken test. Hand back at most 5 lines: files written, collect result, any plan gap.
