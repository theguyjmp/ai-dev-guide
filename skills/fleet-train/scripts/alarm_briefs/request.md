# Alarm {{key}}: an open request nobody picked up

Alarm (from fleet_beat): {{facts}}

Read and do the request {{subject}} (under {{home}}): it says what to fix and how. Fix the CODE; keep every assertion, never
raise a timeout, weaken, skip or delete a test. Work in a clean slot: `py -3 {{home}}/harness.py clean-start --slot {{slot}} --name {{name}}` (CARGO_BUILD_JOBS=2, the env it prints, one foreground cargo command at a time); `harness.py quality-check`; `git merge-tree --write-tree main HEAD` exits 0; `harness.py clean-submit --slot {{slot}} --name {{name}}`; move that queue entry to the front (under h.lock("state") set its ts in state/queue.json to the oldest ts - 1). Delete the slot-{{slot}} target dir when done.
Once the fix is submitted, move the request to `notes/archive/requests/` (create the folder; move it, then `git add -A` both paths and commit in {{home}}) so it is never dispatched twice. If you cannot fix it, leave it in place and say why in your last words.
