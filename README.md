# ai-dev-guide

Claude Code skills for rebuilding a project with a fleet of AI agents.

- `skills/overhaul-check`: scores a codebase and decides whether it needs nothing, refactors, a partial rewrite, or a full overhaul.
- `skills/overhaul-plan`: turns an overhaul verdict into a short, audited rebuild spec timed in agent-hours, with a live dashboard.
- `skills/overhaul-build`: runs the master agent that carries out that spec: M0 machinery, a risk slice, soft launches, tripwires and kill checkpoints.

Install: copy the folders in `skills/` into `~/.claude/skills/`.
