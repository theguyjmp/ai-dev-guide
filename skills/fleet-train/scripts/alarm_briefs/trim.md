# Alarm {{key}}: a notes or code file is over its brevity budget

Alarm (from fleet_beat): {{facts}}

Run `py -3 {{home}}/brevity_check.py` for the full list. Trim each breach to its budget (brevity_budget.json) without losing
a rule or a fact anyone still needs: history to notes/archive/ or a commit message, repetition cut, dated comments moved
to the commit log. Never raise a budget. Commit only the files you changed in {{home}}.
