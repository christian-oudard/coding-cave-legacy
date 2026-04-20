"""Git subprocess wrappers and shared-repo plumbing."""

# Re-export function names so `from cav.git import current_branch`
# works. The `git` submodule stays reachable as `cav.git.git` (set
# automatically by the submodule import below) for callers that use
# `from cav.git import git; git.current_branch(...)`.
from .git import (
    cherry_undelivered,
    commit_oneline,
    commit_timestamp,
    commits_ahead,
    current_branch,
    git_output,
    has_merge_base,
    remote_branches,
    repo_root,
    update_agent_tip,
)

