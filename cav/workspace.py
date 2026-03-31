"""Agent workspace management.

Each agent gets a lightweight clone (--shared) of the cave bare repo.
Objects are shared via alternates, so disk cost is minimal. The clone has its
own .git, making it self-contained for bwrap mounting.
"""

import subprocess
from pathlib import Path

from . import config, git


def create(base: Path, agent_id: str) -> Path:
    """Create a workspace for an agent. Returns the clone path."""
    if not config.AGENT_ID_RE.match(agent_id):
        raise SystemExit(
            f"Invalid agent ID '{agent_id}'. Must be a number (e.g. 1, 2, 3)."
        )

    user_branch = git.current_branch(cwd=base)
    target = config.resolve_target(user_branch)

    slug = config.project_slug(base)
    ws_path = config.worktree_dir(slug, agent_id)

    if ws_path.exists():
        return ws_path

    # New workspace: only allowed from shared branches.
    # "HEAD" means detached HEAD, which is not a valid target.
    if target == "HEAD" or target != user_branch:
        raise SystemExit(
            f"Cannot launch a new agent from agent branch '{user_branch}'.\n"
            f"Switch to a shared branch first (e.g. 'git checkout {target}')."
        )

    ws_path.parent.mkdir(parents=True, exist_ok=True)
    shared = config.shared_repo_path(base)

    # Lightweight clone sharing objects with the bare repo
    git.git("clone", "--shared", str(shared), str(ws_path), echo=False)

    # Rename origin -> cave
    git.git("remote", "rename", "origin", config.SHARED_REMOTE, cwd=ws_path, echo=False)

    ab = config.agent_branch(target, agent_id)
    remote_ref = f"refs/remotes/{config.SHARED_REMOTE}/{ab}"

    # If the agent branch already exists on the remote (re-creating a
    # workspace for a continuing agent), check it out with tracking.
    # Otherwise create a fresh branch and push it.
    try:
        git.git_output("rev-parse", "--verify", remote_ref, cwd=ws_path)
        git.git("checkout", ab, cwd=ws_path, echo=False)
    except subprocess.CalledProcessError:
        git.git("checkout", "-b", ab, cwd=ws_path, echo=False)
        git.git("push", config.SHARED_REMOTE, ab,
                f"--push-option=agent={agent_id}", cwd=ws_path,
                echo=False, capture=True)

    return ws_path
