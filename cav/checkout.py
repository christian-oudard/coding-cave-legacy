"""cav checkout: retarget an agent to deliver to a different base branch."""

from pathlib import Path

from . import config, git
from .constants import CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE


def checkout(base: Path, name: str, agent_id: str | None = None):
    """Retarget an agent to deliver to a different base branch.

    Inside the cave: infers agent from current branch.
    On the host: infers from current branch or explicit agent_id.
    Creates the base branch on the shared remote if it doesn't exist.
    """
    if config.parse_dev_branch(name):
        raise SystemExit(
            f"Cannot use dev branch '{name}' as base.\n"
            f"Use a base branch name (e.g. main, develop)."
        )

    current = git.current_branch(cwd=base)
    parsed = config.parse_dev_branch(current)

    if parsed:
        old_base, aid = parsed
        if agent_id and agent_id != aid:
            raise SystemExit(
                f"On branch for agent {aid}, but targeting agent {agent_id}.\n"
                f"Switch to the correct dev branch first."
            )
        agent_id = aid
    elif agent_id:
        old_base = _find_agent_base(base, agent_id)
    else:
        raise SystemExit(
            "Not on a dev branch.\n"
            "Usage: cav checkout <branch> <agent-id>"
        )

    if old_base == name:
        raise SystemExit(f"Agent {agent_id} already targeting '{name}'.")

    old_branch = config.dev_branch(old_base, agent_id)
    new_branch = config.dev_branch(name, agent_id)

    if parsed:
        # Self-retargeting from inside a cave: push to host-cave (parent).
        _ensure_base_exists(base, name, old_base, HOST_SHARED_REMOTE)
        _retarget_local(base, name, old_branch, new_branch, agent_id)
    else:
        # Host-side retargeting: push to shared remote (local bare repo).
        _ensure_base_exists(base, name, old_base, CAVE_SHARED_REMOTE)
        _retarget_remote(base, name, old_branch, new_branch, agent_id)

    print(f"Retargeted agent {agent_id}: {old_base} -> {name}")


def _find_agent_base(base: Path, agent_id: str) -> str:
    """Find the current base for an agent from remote branches."""
    git.git("fetch", "--prune", CAVE_SHARED_REMOTE, cwd=base, **config.v())
    branches = git.remote_branches(CAVE_SHARED_REMOTE, cwd=base)
    for b in branches:
        parsed = config.parse_dev_branch(b)
        if parsed and parsed[1] == agent_id:
            return parsed[0]
    raise SystemExit(f"No dev branch found for agent {agent_id}.")


def _ensure_base_exists(base: Path, name: str, old_base: str,
                          remote: str = CAVE_SHARED_REMOTE):
    """Create the base branch on the remote if it doesn't exist yet."""
    git.git("fetch", remote, cwd=base, **config.v())
    remote_ref = f"{remote}/{name}"
    exists = git.git(
        "rev-parse", "--verify", remote_ref,
        cwd=base, check=False, capture=True, echo=False,
    ).returncode == 0
    if exists:
        return

    # Create from the old base branch.
    old_ref = f"{remote}/{old_base}"
    try:
        sha = git.git_output("rev-parse", old_ref, cwd=base)
    except Exception:
        raise SystemExit(f"Cannot resolve '{old_ref}' to create '{name}'.")

    git.git("push", remote,
            f"{sha}:refs/heads/{name}", cwd=base, **config.v())


def _retarget_local(base: Path, new_base: str,
                    old_branch: str, new_branch: str, agent_id: str):
    """Retarget from inside the cave (agent has local checkout).

    Pushes to host-cave (the shared remote), not cave (subcaves).
    """
    git.git("branch", "-m", old_branch, new_branch, cwd=base)

    # Try rebase onto new base. Not fatal if it fails.
    remote_base = f"{HOST_SHARED_REMOTE}/{new_base}"
    r = git.git("rebase", remote_base, cwd=base,
                check=False, capture=True, echo=False)
    if r.returncode != 0:
        git.git("rebase", "--abort", cwd=base,
                check=False, capture=True, echo=False)

    # Push new branch, delete old.
    git.git("push", "--force", HOST_SHARED_REMOTE,
            f"HEAD:refs/heads/{new_branch}",
            f":refs/heads/{old_branch}",
            cwd=base, **config.v())


def _retarget_remote(base: Path, new_base: str,
                     old_branch: str, new_branch: str, agent_id: str):
    """Retarget from the host (modify bare repo and workspace directly)."""
    shared = config.shared_repo_path(base)
    git.git("branch", "-m", old_branch, new_branch, cwd=shared)

    slug = config.project_slug(base)
    ws = config.worktree_dir(slug, agent_id)
    if ws.exists():
        git.git("branch", "-m", old_branch, new_branch, cwd=ws)
