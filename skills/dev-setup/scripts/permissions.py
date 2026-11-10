"""Every permission (and a non-auto default mode) a project's agents will need, granted once at the start by one owner paste.

The agent cannot write its own permission settings (Claude Code's auto-mode classifier refuses it as
self-modification), and a fleet that discovers a missing permission mid-build stalls for hours. So at the start of a
project the agent prints ONE line for the owner to paste, and the owner pastes it once.

  py -3 permissions.py --repo <repo> [--dir <folder>]... [--host <ssh host>]... [--cli <tool>]... [--guard <script>]
                       [--stop-guard <script>]
      --guard adds <script> as a PreToolUse hook on Agent|Task|Workflow (overhaul-build's burn-guard.py);
      --stop-guard adds <script> as a Stop hook (overhaul-build's master-stop-guard.py)
      prints the line to paste (the same command with --apply) and exits 1 when rules are missing, 0 when all are set
  ... --apply
      merges the rules into <repo>/.claude/settings.local.json (keeps every existing rule and hook) and prints
      "permissions set: ..." so the owner sees it worked
"""
import argparse, json, os, sys

TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Artifact", "Agent", "WebFetch", "WebSearch"]
COMMANDS = ["gh pr", "gh api", "gh run", "gh repo", "gh issue",
            "git push", "git merge", "git fetch", "git pull", "git worktree", "git branch", "git checkout",
            "git switch", "git commit", "git add", "git rm", "git mv", "git rebase", "git reset", "git tag",
            "git -C", "git -c", "scp", "py -3", "python", "python3", "node", "npm", "npx", "schtasks"]


def rules(a):
    cmds = COMMANDS + [f"ssh {h}" for h in a.host] + a.cli
    return TOOLS + [f"{shell}({c}:*)" for shell in ("Bash", "PowerShell") for c in cmds]


def settings_path(repo):
    return os.path.join(repo, ".claude", "settings.local.json")


def load(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def merged(a):
    s = load(settings_path(a.repo))
    perm = s.setdefault("permissions", {})
    perm["defaultMode"] = a.mode  # never "auto": its classifier overrides allow rules (merges, own settings)
    allow = perm.setdefault("allow", [])
    allow += [r for r in rules(a) if r not in allow]
    dirs = perm.setdefault("additionalDirectories", [])
    dirs += [d.replace("\\", "/") for d in a.dir if d.replace("\\", "/") not in dirs]
    for event, matcher, cmd in hooks(a):
        entries = s.setdefault("hooks", {}).setdefault(event, [])
        if not has_hook(entries, cmd):
            entry = {"matcher": matcher} if matcher else {}
            entries.append({**entry, "hooks": [{"type": "command", "command": cmd}]})
    return s


def hooks(a):
    out = []
    if a.guard:
        out.append(("PreToolUse", "Agent|Task|Workflow", f'py -3 "{a.guard}"'))
    if a.stop_guard:
        out.append(("Stop", None, f'py -3 "{a.stop_guard}"'))
    return out


def has_hook(entries, cmd):
    return any(h.get("command") == cmd for e in entries for h in e.get("hooks", []))


def missing(a):
    s = load(settings_path(a.repo))
    perm = s.get("permissions", {})
    if perm.get("defaultMode") != a.mode:
        return ["defaultMode=" + a.mode]
    have = set(perm.get("allow", [])) | {d.replace("\\", "/") for d in perm.get("additionalDirectories", [])}
    want = rules(a) + [d.replace("\\", "/") for d in a.dir]
    gone = [w for w in want if w not in have]
    return gone + [f"{ev} hook {cmd}" for ev, _, cmd in hooks(a) if not has_hook(s.get("hooks", {}).get(ev, []), cmd)]


def paste_line(a):
    q = lambda v: '"' + v + '"'
    parts = ["py -3", q(os.path.abspath(__file__)), "--repo", q(a.repo)]
    for flag, vals in (("--dir", a.dir), ("--host", a.host), ("--cli", a.cli)):
        for v in vals:
            parts += [flag, q(v)]
    if a.guard:
        parts += ["--guard", q(a.guard)]
    if a.stop_guard:
        parts += ["--stop-guard", q(a.stop_guard)]
    return " ".join(parts + ["--apply"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--dir", action="append", default=[])
    p.add_argument("--host", action="append", default=[])
    p.add_argument("--cli", action="append", default=[])
    p.add_argument("--guard")
    p.add_argument("--stop-guard")
    p.add_argument("--mode", default="acceptEdits", choices=["acceptEdits", "default"])
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    if a.apply:
        s = merged(a)
        path = settings_path(a.repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(s, f, indent=2)
        perm = s["permissions"]
        nh = sum(len(e.get("hooks", [])) for v in s.get("hooks", {}).values() for e in v)
        print(f"permissions set: {len(perm['allow'])} allow rules, {len(perm['additionalDirectories'])} folders, {nh} hooks in {path}")
        return 0
    gone = missing(a)
    if not gone:
        print("permissions: all set")
        return 0
    print(f"permissions: {len(gone)} missing. Owner, paste this once in PowerShell or cmd:")
    print(paste_line(a))
    return 1


if __name__ == "__main__":
    sys.exit(main())
