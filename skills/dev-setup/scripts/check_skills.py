"""Keep the installed ai-dev-guide skills current, checking the repo at most once a day.

Prints one line. The day's result is stored in ~/.claude/skills/.ai-dev-guide-check.json, so every
later call that day costs no network and no git. --force checks now (dev-setup's install step).
"""
import datetime, json, os, shutil, subprocess, sys, tempfile

REPO = "https://github.com/theguyjmp/ai-dev-guide.git"
SKILLS = os.path.join(os.path.expanduser("~"), ".claude", "skills")
STAMP = os.path.join(SKILLS, ".ai-dev-guide-check.json")


def load():
    try:
        with open(STAMP, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(state):
    os.makedirs(SKILLS, exist_ok=True)
    with open(STAMP, "w", encoding="utf-8") as f:
        json.dump(state, f)


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120, check=True).stdout


def main():
    today = datetime.date.today().isoformat()
    state = load()
    if state.get("date") == today and "--force" not in sys.argv:
        print("skills: checked today: " + state.get("result", "?"))
        return
    try:
        remote = git("ls-remote", REPO, "refs/heads/main").split()[0]
    except (OSError, subprocess.SubprocessError, IndexError) as e:
        # Offline or no git: note it for today so the agent does not retry all day.
        state.update(date=today, result="check failed (" + type(e).__name__ + "), using installed skills")
        save(state)
        print("skills: " + state["result"])
        return
    old = state.get("sha")
    if old == remote:
        state.update(date=today, result="up to date at " + remote[:7])
        save(state)
        print("skills: " + state["result"])
        return
    tmp = tempfile.mkdtemp(prefix="ai-dev-guide-")
    try:
        git("clone", "-q", "--depth", "1", REPO, tmp)
        src = os.path.join(tmp, "skills")
        names = sorted(n for n in os.listdir(src) if os.path.isfile(os.path.join(src, n, "SKILL.md")))
        for n in names:
            # Replace each repo skill whole; skill folders the owner has that are not in the repo are kept.
            dst = os.path.join(SKILLS, n)
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(os.path.join(src, n), dst)
    except (OSError, subprocess.SubprocessError) as e:
        state.update(date=today, result="update failed (" + type(e).__name__ + "), using installed skills")
        save(state)
        print("skills: " + state["result"])
        return
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    state.update(date=today, sha=remote,
                 result="UPDATED " + (old or "unknown")[:7] + " -> " + remote[:7] + " (" + ", ".join(names) + "); re-read the SKILL.md you are running")
    save(state)
    print("skills: " + state["result"])


if __name__ == "__main__":
    main()
