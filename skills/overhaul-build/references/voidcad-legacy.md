# VOIDCAD legacy: where the v1 machinery lives

Pointers only. v2 builds none of this; read it for history or to compare runs.

- v1 skill as it ran: `git show 86668be:skills/overhaul-build/SKILL.md` in ai-dev-guide; its references `heartbeat.md`, `heartbeat-brief.md`, `loop.md`, `m0.md`, `red-fix-brief.md`, `rulings.md` and its scripts (`tick.py`, `verdict.py`, `burn.py`, `burn-guard.py`, `master-status.py`, `master-stop-guard.py`, `packet_check.py`, `gate-fresh.py`, `dashhistory.py`, `skill_lint.py`, `hidden.vbs`) are at the same commit.
- Unlanded rewrite draft: branch `wip/overhaul-build-2a`.
- Fleet folder: `Z:/Documents/AppDevelopment/VOIDCAD-fleet`. Machinery in `bin/` (`health.py`, `dispatch.py`, `land-keeper.py`, `heartbeat.py`, `wait.sh`, `gatebot.cmd`, `pull-main.cmd`, `gate-fresh.py`, `dashdata.py`, `gen_dash.py`, `audit12.py`, `owners_check.py`); paper in `COORD-STATE.md`, `DISPATCH.md`, `RULINGS.md`, `HEALTH.tsv`, `handbacks/`, `briefs/`, `templates/`, `dash/`.
- Spec the v1 master ran by: `Z:/Documents/AppDevelopment/VOIDCAD/spec/` (`MASTER-AGENT.md`, `PLAN.md`, `MACHINES.md`, `STANDARDS.md`, `FUNCTIONS.md`, `ARCHITECTURE.md`, `PATTERNS.md`); the `vc` check CLI is described in `MACHINES.md`.
- Evidence behind v2: `VOIDCAD-fleet/notes/` `failure-analysis-2026-11-07.md`, `proof-check-2026-11-07.md`, `code-quality-2026-11-07.md`, `build-vs-buy-2026-11-07.md`, `redesign-2026-11-07.md`.
