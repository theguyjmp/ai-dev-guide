# Heartbeat (every 20 minutes, every check; owner 2026-11-06)
FLEET = <harness>, repo <repo>. Run every check below, in order, one foreground call each, small output only. First measure the pipeline: run <FLEET>/bin/dashdata.py and read <FLEET>/dash/pipeline.json; the first line of the result and of the hand-back is always `proven X, landed Y, waiting-land Z, in review W` (functions; add `building V` and the packet counts after it). Never launch agents, never edit briefs, code, thresholds or protected files.
1. Health: `py -3 <repo>/tools/health/health.py`; list the newest run's red tripwires with their action column.
2. Machines and burn: `py -3 <FLEET>/bin/heartbeat.py`; list its new red rows.
3. Loop status: `py -3 <FLEET>/bin/master-status.py`; copy its lines.
4. Bottleneck: read the `top` of <FLEET>/dash/bottleneck.json and the last RULINGS.md line naming that stage; say whether the stage's wait dropped since that fix.
5. Management: do <FLEET>/templates/management.md over briefs and FAIL reviews written since the newest <FLEET>/reviews/management-*.md; write its file there.
6. Dashboard: do <FLEET>/briefs/dash-refresh.md (dashdata.py, dashhistory.py, upload changed datasets incl. history, write dash/.uploaded last).
Write the whole result to <FLEET>/heartbeat/<UTC yyyy-mm-ddTHHMM>.md. Hand back at most 7 lines: the pipeline line first, then one per check, red items first, each with the action it calls for. Call cap 45.
