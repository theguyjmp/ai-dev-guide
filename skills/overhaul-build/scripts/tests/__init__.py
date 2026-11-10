"""Tests build throwaway repos with plain `git init`/`git config`. Run from a git hook, `git rebase -x` or a worktree
shell with GIT_DIR exported, those calls hit the caller's repo instead: core.bare=true and a toy identity were written
into ai-dev-guide/.git/config (2026-11-10). Drop every repo-locating git variable before any test runs."""
import os

for _var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
             "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_NO_REPLACE_OBJECTS",
             "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX", "GIT_SHALLOW_FILE", "GIT_NAMESPACE",
             "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
    os.environ.pop(_var, None)
