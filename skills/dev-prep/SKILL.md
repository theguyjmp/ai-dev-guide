---
name: dev-prep
description: Brainstorm a new app or project from nothing with the owner, in rounds of multiple-choice questions plus research into similar apps and open-source projects, until every function the owner wants now, later or maybe is written into a feature map (prep/FEATURES.md, DECISIONS.md, OPEN.md) that dev-plan turns into a build spec. Use when the owner says "new app", "new project", "start a project from scratch", "brainstorm features", "what should my app do", or "dev-prep". The first of three skills: dev-prep, dev-plan, dev-build.
---

# dev-prep

You find every function the owner wants before any plan exists. You ask; the owner decides. You write no plan, no code and no repo here. This is the only one of the three dev skills that asks the owner questions.

## Rules for every round
- Ask with AskUserQuestion: 1 to 4 questions a round, multiple choice wherever the answer can be one, multi-select when more than one answer can be true. Each option is a short label plus a one-line description. The owner can always type their own answer.
- One area per round. Never ask what the owner's rules, memory or an earlier answer already settle.
- Never invent a feature. A function you thought of, or one you found in research, is a question until the owner picks it.
- No times, deadlines, budgets in hours or effort estimates, in questions or files.
- Chat text around a question is 2 lines at most. Findings go in the files, not in chat.

## Step 1. Before the first question
1. Read the owner's rules: `~/.claude/CLAUDE.md`, the project's CLAUDE.md if one exists, and the memory index with the memory files it links that touch products, privacy, machines, accounts or style. Write each rule that settles a product question into prep/DECISIONS.md, marked `owner rules` (for example: nothing leaves the machine unless allowed, model calls use prompt caching, humanoid assets clothed).
2. Round 1 asks the project's name, its folder (the current folder, or a new folder named after the project beside it), the product goal in one sentence (offer 3 or 4 readings of what the owner said, plus their own words) and the main users.
3. Create `<PROJECT>/prep/` with the four files in Step 4, empty but for their headers and the owner-rule decisions.

## Step 2. The areas
Cover these, one per round, in this order the first time through; after that, take the weakest area next (the one with the most open questions, then the fewest answered rounds):
1. Product goal and users.
2. Core workflows: the 3 to 6 things a user does start to finish.
3. Must-haves for the first version.
4. Nice-to-haves.
5. The future roadmap, so the plan leaves seams for it.
6. Platforms and devices (desktop, web, phone, tablet, headset; operating systems; input: mouse, touch, pen, controller).
7. Data and files: what the app reads, writes, imports and exports, and the file formats.
8. Integrations: other apps, services, APIs, hardware.
9. Accounts, roles and billing.
10. Offline use, privacy, and what may leave the machine.
11. Scale: users, data size, files per project.
12. Accessibility and languages.
13. Look and feel: style, density, light and dark, brand.
14. The owner's own rules that are not yet written down (ask only what Step 1 did not settle).

## Step 3. Research (after round 2, and again for any area where it helps)
1. Load WebSearch and WebFetch with ToolSearch. Search for 3 to 6 similar apps and open-source projects. Name each one, with its link.
2. For each, extract what it does (its functions), what users praise and what they complain about (reviews, forums, issue trackers). Write one line per app into prep/RESEARCH.md: `| App | Link | Open source | Functions | Praised | Complained |`.
3. Ask the owner about the functions they have not yet chosen, at most 4 functions per round, as three multi-select questions over the same functions: "Apps like X and Y have A, B, C and D. Which do you want in the first version?", "Which later?" and "Which maybe?". A function picked in none is a no. A function picked twice takes the earliest.
4. A complaint the owner cares about becomes a Notes line on the function it touches (for example "users of X complain sync loses edits"), so dev-plan can turn it into a bug pattern.

## Step 4. The files (rewrite them after every round)
- prep/FEATURES.md: one table, `| System | Function | When (now, later, maybe) | Source (owner, app name) | Notes |`. One row per function a user can see or call. System names the area of the product (for example Editor, Sync, Accounts). Source is `owner` for anything the owner said unprompted, otherwise the app it came from.
- prep/DECISIONS.md: one line per decision: `D<n>: <decision> (<owner rules | round n>)`. Every "no" goes here (`No: <function> (<source>)`), so it is never asked again.
- prep/OPEN.md: first the areas list with each area's rounds and open-question count, then one line per open question, with the area it belongs to.
- prep/RESEARCH.md: the research table from Step 3.

## Step 5. After each round
1. Update the files.
2. Ask one AskUserQuestion: "Keep brainstorming, or start planning?" Options: "Keep brainstorming" (the next round takes the weakest area, named in the option's description), "Start planning".
3. On "Start planning": if prep/OPEN.md holds a question that blocks a "now" function, ask that one round first. Then write the areas never covered into prep/OPEN.md as `dev-plan decides`, and invoke the dev-plan skill.

## Never
- Write a plan, a spec, code or a repo.
- Add a function the owner did not pick, or put a "no" into FEATURES.md.
- Ask more than 4 questions in a round, or ask what the owner's rules already answer.
- Ask about or write times, deadlines or effort.
- Paste research, tables or file contents into chat.
- Start planning without the owner choosing "Start planning".
