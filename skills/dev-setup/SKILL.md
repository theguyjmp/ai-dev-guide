---
name: dev-setup
description: Set up a computer and a new project for agent development from zero. It installs Git and the GitHub CLI if missing, gets the owner signed in, installs the ai-dev-guide skills, writes the global agent rules, creates the project folder in Documents/AppDevelopment/<name>, then walks the owner through dev-prep, dev-plan and dev-build. Use when the owner says "dev-setup", "set me up", "set up a new project", "get started", or is new to this workflow.
---

# dev-setup

You get a new person and a new project ready, then hand them to dev-prep, dev-plan and dev-build. Talk in plain, friendly English: the owner may never have used Git or a terminal. One step at a time. Before each step that changes the computer, say in one line what it does. If a step is already done, say so and skip it. Never repeat a finished step on a rerun.

## Step 1. Git
1. Run `git --version`. If it works, skip to step 2.
2. Install it:
   - Windows: `winget install --id Git.Git -e --source winget`. If winget is missing, send the owner to https://git-scm.com/install/windows and wait until they say it is done.
   - macOS: `xcode-select --install` (or `brew install git` when Homebrew exists).
   - Linux: the system package manager (`sudo apt install git`, `sudo dnf install git`).
3. Run `git --version` again. On Windows, a new terminal may be needed: tell the owner to restart the chat if it is still not found.

## Step 2. GitHub sign-in
1. Run `gh --version`. If it is missing, install the GitHub CLI: Windows `winget install --id GitHub.cli -e`, macOS `brew install gh`, Linux from https://cli.github.com.
2. Run `gh auth status`. If they are signed in, skip to step 3.
3. If they have no GitHub account, send them to https://github.com/signup and wait.
4. Sign-in needs their browser, so ask the owner to type this in the chat box and follow the prompts (choose GitHub.com, HTTPS, and log in with the web browser):
   `! gh auth login --web`
   Then run `gh auth status` to confirm.
5. Run `gh auth setup-git` so Git uses that sign-in.
6. Set the commit name and email when they are empty (`git config --global user.name` and `user.email`). Use the GitHub login and the private no-reply address: `gh api user --jq '.id, .login'`, then `<id>+<login>@users.noreply.github.com`. Never put their real email anywhere.

## Step 3. The skills
Check that ~/.claude/skills holds dev-prep, dev-plan, dev-build, overhaul-check, overhaul-plan, overhaul-build and overhaul-difference. Install any that are missing (and update old ones) from https://github.com/theguyjmp/ai-dev-guide: clone it to a temp folder and copy each folder in skills/ into ~/.claude/skills/. Keep any skill folder the owner has that is not in the repo.

## Step 4. Global agent rules
Write the block below into the owner's global agent file: ~/.claude/CLAUDE.md for Claude Code, and also ~/.codex/AGENTS.md if that folder exists. Put it between the two marker lines. If the markers are already there, replace only what is between them. Never touch the rest of the file, and never add a second copy.

```
<!-- ai-dev-guide rules: start -->
## Agent rules (from ai-dev-guide)
- Commit and push after every change so the project is always backed up.
- Before building anything, search the project for something that already does it and extend that. Never build a second version.
- Keep the code clean, organized and indexed so agents don't waste context searching.
- Prove it works: run the app, click through the change, take screenshots. Add a test for every bug fixed.
- Keep replies to a few short lines. Long details go in a file.
- Don't stop to ask me which option. Pick your recommendation, note why, keep going. Ask only before spending money, deleting, publishing or changing an account.
- Keep sub-agents short: one goal each, about 100 steps, then hand back. Start a fresh one for the next part.
- Give agents call limits, checks and an order of work, never deadlines or time limits.
- Keep tool output small: tail or grep logs, never print whole files, logs or diffs.
- If the same task fails a review twice, stop and change the approach. Never run a third identical attempt.
- Use the cheap model (Sonnet) for checking, re-testing and merging. Use Opus only for real building and debugging.
- Catch stuck agents (waiting and waking over and over). Save their work, stop them, start a fresh one.
- Keep a state file current so a long chat can be compacted without losing anything. Compaction is automatic: don't ask me to run it.
- Turn on prompt caching for any app code that calls an AI model repeatedly.
- Close programs only by process ID, never by name.
<!-- ai-dev-guide rules: end -->
```

## Step 5. The project folder
1. Ask the owner for the project's name in plain chat (a short name, for example "recipe-app"). Turn it into a safe folder name: lowercase, letters, digits and dashes.
2. Find the real Documents folder (on Windows it may sit inside OneDrive): `powershell -NoProfile -Command "[Environment]::GetFolderPath('MyDocuments')"`. Elsewhere use ~/Documents.
3. Create `<Documents>/AppDevelopment/<name>`. If it already exists and is not empty, ask whether to use it or pick another name.
4. Inside it: `git init -b main`, a .gitattributes with `* text=auto eol=lf`, and a short CLAUDE.md: "Project <name>. Follow the global agent rules. Paper goes in prep/ and the fleet folder the plan names." Commit it.
5. Tell the owner the folder path in one line.

## Step 6. Walk them through
1. Say: "Next we'll work out everything your app should do." Invoke dev-prep with the project folder as PROJECT. dev-prep asks its own questions and ends by asking whether to keep brainstorming or start planning.
2. When they choose planning, dev-prep invokes dev-plan. dev-plan runs on its own and ends by asking whether to start the build.
3. The build must run from a chat opened on the project folder, so builders can work in parallel and the chat stays small. When they say yes, tell them:
   - Open the Claude desktop app, choose **Code**, start a new chat in **Local** mode (not cloud), and click the folder icon to pick `<Documents>/AppDevelopment/<name>`.
   - Type `/dev-build` there.
4. Tip for later, in one line: `/remote-control` lets them watch the build from their phone.

## Finish
Report in 4 lines at most: what was installed or already there, the project folder, and the next command they will type.

## Never
- Put the owner's email, tokens or passwords in any file or command output.
- Install anything except Git, the GitHub CLI and the ai-dev-guide skills.
- Overwrite the owner's existing CLAUDE.md or AGENTS.md text outside the marked block.
- Delete or overwrite an existing project folder.
- Start dev-build from this chat when it is not opened on the project folder.
