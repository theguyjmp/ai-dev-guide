# Alarm {{key}}: a harness script errors

Alarm (from fleet_beat): {{facts}}

Read the newest matching error events in {{home}}/state/events.jsonl (grep the event kind, the last 3 only) and the lines
their tracebacks name. Fix the cause by the freeze rule (change a copy, test it, swap once): change a copy, test the
copy (tests/ holds the harness tests; add one for this failure), swap once, commit only the files you changed. Never kill
the train; never disable or loosen the check that errors.
