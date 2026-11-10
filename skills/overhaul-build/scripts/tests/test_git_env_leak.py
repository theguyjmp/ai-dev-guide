"""An inherited GIT_DIR must not let a throwaway repo's `git init`/`git config` write into the caller's repo."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent


def git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=True).stdout.strip()


class GitEnvLeakTest(unittest.TestCase):
    def test_inherited_git_dir_does_not_reach_caller_repo(self):
        root = Path(tempfile.mkdtemp(prefix="gitleak_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        host, wt, toy = root / "host", root / "wt", root / "toy"
        clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        git(root, "init", "-q", str(host), env=clean)
        for a in (("config", "user.email", "t@t"), ("config", "user.name", "t"),
                  ("commit", "-q", "--allow-empty", "-m", "i"), ("worktree", "add", "-q", str(wt))):
            git(host, *a, env=clean)
        gitdir = git(wt, "rev-parse", "--absolute-git-dir", env=clean)  # a worktree gitdir: `git init` guesses bare
        toy.mkdir()
        code = ("import subprocess, sys; sys.path.insert(0, sys.argv[1]); import land; "
                "[subprocess.run(['git', *a], check=True, capture_output=True) for a in "
                "(['init', '-q'], ['config', 'user.name', 'toy'])]")
        subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], cwd=toy, check=True, env={**clean, "GIT_DIR": gitdir})
        self.assertEqual(git(host, "config", "--get", "core.bare", env=clean), "false")
        self.assertEqual(git(host, "config", "--get", "user.name", env=clean), "t")
        self.assertTrue((toy / ".git").is_dir())


if __name__ == "__main__":
    unittest.main()
