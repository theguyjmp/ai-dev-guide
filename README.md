# ai-dev-guide

Claude Code skills for rebuilding a project, or building a new one, with a fleet of AI agents.

- `skills/overhaul-check`: scores a codebase and decides whether it needs nothing, refactors, a partial rewrite, or a full overhaul.
- `skills/overhaul-plan`: turns an overhaul verdict into a short, audited rebuild spec timed in agent-hours, with a live dashboard.
- `skills/overhaul-build`: runs the master agent that carries out that spec: M0 machinery, a risk slice, soft launches, tripwires and kill checkpoints.
- `skills/overhaul-difference`: compares the rebuilt project with the original, from measurements only, and publishes the difference as a dashboard.
- `skills/dev-setup`: installs Git and the GitHub CLI, signs you in, installs these skills and the agent rules, creates your project folder, then walks you through dev-prep, dev-plan and dev-build.
- `skills/dev-prep`: brainstorms a new project with the owner in multiple-choice rounds plus research into similar apps, and writes a feature map of what to build now, later or maybe.
- `skills/dev-plan`: turns that feature map into an audited build spec, following overhaul-plan without the old-project steps.
- `skills/dev-build`: runs the master agent on that spec, following overhaul-build without salvage or quarantine.

Install: copy the folders in `skills/` into `~/.claude/skills/`.
