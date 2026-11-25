# ai-dev-guide

Claude Code skills for rebuilding a project, or building a new one, with a fleet of AI agents.

- `skills/overhaul-check`: scores a codebase and decides whether it needs nothing, refactors, a partial rewrite, or a full overhaul.
- `skills/overhaul-plan`: turns an overhaul verdict into a short, audited rebuild spec timed in agent-hours, with a live dashboard.
- `skills/overhaul-build`: runs the master agent that carries out that spec: M0 machinery, a risk slice, soft launches, tripwires and kill checkpoints.
- `skills/fleet-train`: runs short-lived builder agents on planned rows and lands their work merge-first (a push reaches main after its affected checks; a sharded verifier runs the full suite behind, bisects and auto-reverts, and releases come from the `verified` ref), with work lists fixed by problem class and claims that never overlap. Replaces drive.py for the build phase.
- `skills/overhaul-difference`: compares the rebuilt project with the original, from measurements only, and publishes the difference as a dashboard.
- `skills/dev-setup`: installs Git and the GitHub CLI, signs you in, installs these skills and the agent rules, creates your project folder, then walks you through dev-prep, dev-plan and dev-build.
- `skills/dev-prep`: brainstorms a new project with the owner in multiple-choice rounds plus research into similar apps, and writes a feature map of what to build now, later or maybe.
- `skills/dev-plan`: turns that feature map into an audited build spec, following overhaul-plan without the old-project steps.
- `skills/dev-build`: runs the master agent on that spec, following overhaul-build without salvage or quarantine.
- `skills/devping`: puts a project on the owner's Discord as one card edited in place; owner-important events (blocked on the owner, work stopped, cycle done) show on it, routine progress never does.

Install: copy the folders in `skills/` into `~/.claude/skills/` (or say "dev-setup" in Claude Code and it does it).

The path for a new project: `dev-setup` (tools, sign-in, skills, rules, folder) -> `dev-prep` (feature map) ->
`dev-plan` (audited spec, repo, dashboard) -> `dev-build` (M0 and the risk slice) -> `fleet-train` for the build
phase with many agents (merge first: a push reaches `main` after its affected checks, the full suite runs behind it
and moves `verified`; audit findings are fixed by problem class; see `skills/fleet-train/CYCLE.md`, "The minimum
cycle") -> `devping` for the owner's card. Python 3.10+ and git 2.38+ are needed for the scripts.
