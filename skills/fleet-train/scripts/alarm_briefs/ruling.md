# Alarm {{key}}: rows rejected 4+ times need a ruling

Alarm (from fleet_beat): {{facts}}

For each row listed, read its reject history (state/rejects.json, `reject` events) and its spec row (spec/FUNCTIONS.md in
{{repo}}). Decide ONE of: innocent (another row or main broke it: requeue as is), narrow (too big: split it into smaller
rows, say which), pause (blocked on something you name). Write notes/rulings/<row>.md (at most 400 words; first line
`<row>: innocent|narrow|pause - <why>`) and add {"by": "ruling", "reason": ...} to its state/rejects.json list under
h.lock("state"). No build pass from here.
