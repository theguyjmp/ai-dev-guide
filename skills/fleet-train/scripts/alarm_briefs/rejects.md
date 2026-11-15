# Alarm {{key}}: many rejects, few landings

Alarm (from fleet_beat): {{facts}}

Read the reject reasons of the last 2 h (`reject` events in state/events.jsonl, grep; state/rejects.json). Find the shared
cause (one test, one gate rule, one file every row touches, a harness bug) and fix it once: a code fix through a clean slot
over 300 and clean-submit, a harness fix by the freeze rule (change a copy, test it, swap once), or a ruling in
notes/rulings/<row>.md for rows rejected wrongly. Never loosen a gate or rule to let rows through.
