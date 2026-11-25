---
name: devping
description: Put a project on the owner's Discord dashboard (one card per project, edited in place) and show owner-important alerts on it. Use when the owner says "devping", "add this project to discord", "notify me", or when a project chat starts or ends a dev cycle, has stopped work, or has something the owner must know quickly.
---
> **RULE ZERO (the most important rule, ever):** the work must be valuable, well made, clean code that can be built
> out and extended. A very solid foundation beats functionality depth: missing depth is acceptable, a weak foundation
> is not. One owner per capability, typed data models, stable ids, clear extension seams, no second copy of anything;
> never trade code quality, tests or gates for speed or feature count. Every brief this skill writes for an agent
> quotes this rule at its top.


# devping

One Discord card per project, edited in place, shows how the project is doing. Alerts are lines on that card
("last alert ..."), only for things the owner must act on or know soon; devping never posts a second message, and a
reposted card is sent silently. A scheduled job runs `tick` every 5 minutes for every registered project.

Tool (written `devping` below):
- Windows: `py -3 ~/.claude/skills/devping/scripts/devping.py` (in cmd: `py -3 %USERPROFILE%/.claude/skills/devping/scripts/devping.py`)
- macOS / Linux: `python3 ~/.claude/skills/devping/scripts/devping.py`

The tool lives in the skill folder, which skill updates replace whole. Everything the user owns lives in the data
dir `~/.claude/devping/`: `config.json` (webhook, projects) and `state/` (card ids, alert history, `devping.log`).
Never put either inside the skill folder, never commit them, never print the webhook: it is a secret.

## One-time setup (per machine)
1. In Discord: channel settings, Integrations, Webhooks, New Webhook, Copy Webhook URL.
2. `devping setup --webhook <webhook URL>`. It creates the data dir and prints the
   exact schedule command for this machine. Run that command once:
   - Windows (Command Prompt): `schtasks /Create /F /TN devping /SC MINUTE /MO 5 /TR "\"<path>\pythonw.exe\" \"<path>\devping.py\" tick"`.
     It must be `pythonw.exe` (python.org installs ship it): no console window may ever pop up on the owner's screen.
   - macOS / Linux: `crontab -e`, add `*/5 * * * * "<python3>" "<path>/devping.py" tick >/dev/null 2>&1`.
3. Optional: `--bot-token <token> --channel-id <id>` lets devping see other people's messages, so the card is
   reposted (silently) when talk buries it under 3 or more messages; without them it is only edited in place.

The webhook comes only from `config.json` or `--webhook`; there is no default. Errors from scheduled runs go to
`~/.claude/devping/state/devping.log`.

## Register this project (once)
`devping add <Name> --dir <project dir> [--stats-cmd "<cmd>"] [--publish-cmd "<cmd>"] [--chat-dir <dir>] [--webhook <url>] [--idle-min 20]`
- The name is the project's stable id: its card, state and alerts hang off it. Do not rename; `remove` and `add`.
- `--chat-dir` defaults to Claude Code's transcripts folder for a chat started in `--dir`
  (`~/.claude/projects/<slug>`). If the project chat runs from another folder, pass that folder's transcripts dir.
- `--webhook` only when the owner wants this project in its own channel.
- Without `--stats-cmd` the card shows chat activity and the cycle only. With it, see the adapter section.
- `--publish-cmd` (optional) is started, not awaited, after every tick: for a project that refreshes its own
  dashboard whenever the card does.
- Check it: `devping tick <Name> --dry` prints the card as JSON and sends nothing.

## During a cycle
- `devping start <Name>` when a dev cycle begins; `devping stop <Name>` when it ends (grey card, no alerts).
- `devping alert <Name> "<one sentence>" --key <stable-key>` ONLY for an owner-important event: blocked on the
  owner (a decision, credentials, a payment), work stopped, money or data at risk, the cycle done. It shows on the
  card as "last alert" for 6 hours. The same key shows again at most once per `--cooldown` minutes (default 30);
  at most 4 alerts per project per hour, the rest are held back and counted on the card.
- `devping note <Name> "<short line>"` for anything else worth seeing: a standing line on the card.
  `note <Name> ""` clears it.
- Routine progress never becomes an alert: the card already shows it. One card per project, edited in place;
  never post a second status message.
- Built-in alarm: when the project chat writes nothing for `--idle-min` minutes in an active cycle (after the
  first 15 minutes) and the adapter does not say `quiet_ok`, the card shows "the project chat has been quiet".

Other commands: `devping list`, `devping remove <Name>` (the Discord card stays), `devping tick [<Name>]`.
`--dry` (or `--dry-run`) on `start`, `stop`, `alert`, `note` and `tick` prints each Discord payload as JSON and
saves nothing.

## Stats adapter (--stats-cmd)
A small read-only command in the project that prints ONE JSON object to stdout, run by `tick` in `--dir`. Keep it
fast (under 60 s; killed at 180 s) and windowless. Every key is optional; unknown keys are ignored:

```json
{"health": "green|amber|red", "headline": "...", "progress": [done, total],
 "fields": [{"name": "...", "value": "...", "inline": true, "short": "..."}], "links": [{"name": "...", "url": "..."}],
 "tiles": [["name", "value"]], "alerts": [{"key": "...", "text": "...", "cooldown_min": 120}], "quiet_ok": false}
```

- `progress`: two whole numbers, `0 <= done <= total`, or `null` when unknown.
- `fields`: the first 12 show as a compact two-column table (`short`, when given, replaces the name there; bold is
  stripped); a value holding a Discord time like `<t:1700000000:R>` goes on the card's time line instead.
- `tiles`: `[name, value]` pairs; when present they replace `fields`, each as one full-width Discord field (up to
  23, plus "Last edited"), and the description keeps only the progress bar, the note and the last alert.
- `alerts`: what the adapter sees as owner-important; recorded on the card with the same dedup, cooldown and cap
  as `devping alert`.
- `quiet_ok`: true lets the chat sit idle without the alarm; devping ignores it while health is red.
- Output that breaks the contract, a crash or a timeout shows as a red card with the reason.

Rules every adapter keeps (each one was learned from a card that lied):
1. **Total counts the WHOLE platform:** every planned row, plus roadmap rows not moved into the plan yet, plus any
   separate track. Never only the current plan's rows: counting one plan overstated progress.
2. **Each row counts once:** a roadmap row the plan already holds, or one marked as moved or split into the plan,
   is not counted again; a repeated id counts once.
3. **The card survives re-planning:** nothing may drop to 0 because the plan was redrawn.
   - Done counts the plan's units plus the older packets their `replaces` cells absorb; an absorbed packet is done
     when it, or any packet absorbing it, landed.
   - The rate counts every fix push on main, not only the new plan's ids.
   - Problem counts come from the class gate's own ratchet counts. Until the gate lands they stay at the plan's
     day-one baseline, shown as "not measured", never 0.
4. **Report `main` and `verified` apart:** what landed and what the full verification passed are different facts.
5. **`quiet_ok` only while work is measurably moving** (main moved recently, a verifier is gating), never on red.
6. **A probe error reads as unknown, never as 0:** a failed git call, a missing file or a dead process shows as
   "unknown" (and amber), never as an empty count.

Reference adapter: `scripts/stats_example.py` (tested by `py -3 scripts/tests/test_stats_example.py`, which prints
ALL PASS). It reads, all from git at `--main`:
- the plan (`--functions`, default `spec/FUNCTIONS.md`): markdown table rows with an `id` and a `status` column;
  `--done-status` (default `proven`) rows are proven; an optional `replaces` column lists older ids the row absorbs;
- the roadmap (`--roadmap`, optional): rows with an `id` column; a table with a `track` column is the separate
  track; a row with a cell saying "moved to <plan file stem>" (for `spec/FUNCTIONS.md`: "moved to FUNCTIONS") is
  already in the plan under other ids, and any other wording still counts;
- pushes: first-parent commits whose subject tags an id in brackets, `[F01]` (`--tag-re` to change);
- `--verified <ref>` (optional) and `--problems <gate list of "path count" lines> --problems-baseline <N>`.

Register it with, for example:
`devping add MyApp --dir . --stats-cmd "py -3 %USERPROFILE%/.claude/skills/devping/scripts/stats_example.py --roadmap spec/ROADMAP.md --verified verified"`
(macOS / Linux: `python3 ~/.claude/skills/devping/scripts/stats_example.py ...`). A project whose plan or
landing records look different writes its own adapter in the project, against the same contract and rules.
