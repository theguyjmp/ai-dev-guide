# Alarm {{key}}: fix a test red on main

Alarm (from fleet_beat): {{facts}}

The test {{subject}} fails on main or sinks train batches. If a fix request or brief for it exists (notes/requests/fix-test-*.md
or the brief the alarm names), follow it. Otherwise run it alone on main first, find why it fails and fix the CODE, keeping
every assertion: never raise a timeout, weaken, skip or delete a test. Remove it from state/accept-known-red.json only after
the fix lands (h.lock("state")). Work in a clean slot: `py -3 {{home}}/harness.py clean-start --slot {{slot}} --name {{name}}` (CARGO_BUILD_JOBS=2, the env it prints, one foreground cargo command at a time); `harness.py quality-check`; `git merge-tree --write-tree main HEAD` exits 0; `harness.py clean-submit --slot {{slot}} --name {{name}}`; move that queue entry to the front (under h.lock("state") set its ts in state/queue.json to the oldest ts - 1). Delete the slot-{{slot}} target dir when done.
