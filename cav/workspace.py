"""Agent workspace management.

Each agent gets a lightweight clone (--shared) of the cave bare repo.
Objects are shared via alternates, so disk cost is minimal. The clone has its
own .git, making it self-contained for bwrap mounting.
"""

from pathlib import Path

from . import config, git


def create(base: Path, agent_id: str) -> Path:
    """Create a workspace for an agent. Returns the clone path."""
    if not config.AGENT_ID_RE.match(agent_id):
        raise SystemExit(
            f"Invalid agent ID '{agent_id}'. Must be a number (e.g. 1, 2, 3)."
        )

    user_branch = git.current_branch(cwd=base)
    if config.parse_agent_branch(user_branch):
        raise SystemExit(
            f"Cannot launch an agent from agent branch '{user_branch}'.\n"
            f"Switch to a shared branch first (e.g. 'git checkout main')."
        )

    slug = config.project_slug(base)
    ws_path = config.worktree_dir(slug, agent_id)

    if ws_path.exists():
        return ws_path

    ws_path.parent.mkdir(parents=True, exist_ok=True)
    shared = config.shared_repo_path(base)

    # Lightweight clone sharing objects with the bare repo
    git.git("clone", "--shared", str(shared), str(ws_path), echo=False)

    # Rename origin -> cave
    git.git("remote", "rename", "origin", config.SHARED_REMOTE, cwd=ws_path, echo=False)

    # Create and checkout agent branch
    ab = config.agent_branch(user_branch, agent_id)
    git.git("checkout", "-b", ab, cwd=ws_path, echo=False)

    # Push agent branch immediately so it's visible in cav status
    git.git("push", config.SHARED_REMOTE, ab,
            f"--push-option=agent={agent_id}", cwd=ws_path, echo=False)

    return ws_path
