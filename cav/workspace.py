"""Agent workspace management.

Each agent gets a lightweight clone (--shared) of the cave bare repo.
Objects are shared via alternates, so disk cost is minimal. The clone has its
own .git, making it self-contained for bwrap mounting.
"""

import subprocess
from pathlib import Path

from . import config, git


def _repo_is_empty(path: Path) -> bool:
    """True if the repo has no commits."""
    r = git.git("rev-parse", "HEAD", cwd=path, check=False, capture=True, echo=False)
    return r.returncode != 0


def create(base: Path, agent_id: str) -> Path:
    """Create a workspace for an agent. Returns the clone path."""
    if not config.AGENT_ID_RE.match(agent_id):
        raise SystemExit(
            f"Invalid agent ID '{agent_id}'. Must be a number (e.g. 1, 2, 3)."
        )

    empty = _repo_is_empty(base)
    if empty:
        target = "main"
    else:
        user_branch = git.current_branch(cwd=base)
        target = config.resolve_target(user_branch)

    slug = config.project_slug(base)
    ws_path = config.worktree_dir(slug, agent_id)

    if ws_path.exists():
        return ws_path

    # "HEAD" means detached HEAD, which is not a valid target.
    if target == "HEAD":
        raise SystemExit(
            f"Cannot launch a new agent from detached HEAD.\n"
            f"Switch to a branch first (e.g. 'git checkout main')."
        )

    ws_path.parent.mkdir(parents=True, exist_ok=True)
    shared = config.shared_repo_path(base)

    # Sync user repo to shared repo so the clone has all branches.
    if not empty:
        git.git("push", "--no-set-upstream", config.SHARED_REMOTE, "--all",
                cwd=base, echo=False)

    # Lightweight clone sharing objects with the bare repo
    git.git("clone", "--shared", str(shared), str(ws_path), echo=False)

    # Rename origin -> cave
    git.git("remote", "rename", "origin", config.SHARED_REMOTE, cwd=ws_path, echo=False)

    ab = config.agent_branch(target, agent_id)

    if empty:
        # No commits yet, create an orphan branch so the agent can make the first commit.
        git.git("checkout", "--orphan", ab, cwd=ws_path, echo=False)
        return ws_path

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
