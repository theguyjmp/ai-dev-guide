"""Every permission a project's agents will need, granted once at the start by one owner paste.

The agent cannot write its own permission settings (Claude Code's auto-mode classifier refuses it as
self-modification), and a fleet that discovers a missing permission mid-build stalls for hours. So at the start of a
project the agent prints ONE line for the owner to paste, and the owner pastes it once.

  py -3 permissions.py --repo <repo> [--dir <folder>]... [--host <ssh host>]... [--cli <tool>]... [--guard <script>]
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
    allow = perm.setdefault("allow", [])
    allow += [r for r in rules(a) if r not in allow]
    dirs = perm.setdefault("additionalDirectories", [])
    dirs += [d.replace("\\", "/") for d in a.dir if d.replace("\\", "/") not in dirs]
    if a.guard:
        cmd = f'py -3 "{a.guard}"'
        pre = s.setdefault("hooks", {}).setdefault("PreToolUse", [])
        if not any(h.get("command") == cmd for e in pre for h in e.get("hooks", [])):
            pre.append({"matcher": "Agent|Task|Workflow", "hooks": [{"type": "command", "command": cmd}]})
    return s


def missing(a):
    s = load(settings_path(a.repo))
    perm = s.get("permissions", {})
    have = set(perm.get("allow", [])) | {d.replace("\\", "/") for d in perm.get("additionalDirectories", [])}
    want = rules(a) + [d.replace("\\", "/") for d in a.dir]
    return [w for w in want if w not in have]


def paste_line(a):
    q = lambda v: '"' + v + '"'
    parts = ["py -3", q(os.path.abspath(__file__)), "--repo", q(a.repo)]
    for flag, vals in (("--dir", a.dir), ("--host", a.host), ("--cli", a.cli)):
        for v in vals:
            parts += [flag, q(v)]
    if a.guard:
        parts += ["--guard", q(a.guard)]
    return " ".join(parts + ["--apply"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--dir", action="append", default=[])
    p.add_argument("--host", action="append", default=[])
    p.add_argument("--cli", action="append", default=[])
    p.add_argument("--guard")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    if a.apply:
        s = merged(a)
        path = settings_path(a.repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(s, f, indent=2)
        perm = s["permissions"]
        print(f"permissions set: {len(perm['allow'])} allow rules, {len(perm['additionalDirectories'])} folders in {path}")
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
