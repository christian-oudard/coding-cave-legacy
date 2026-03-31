"""cav checkout: retarget an agent to deliver to a different branch."""

from pathlib import Path

from . import config, git


def checkout(base: Path, name: str, agent_id: str | None = None):
    """Retarget an agent to deliver to a different shared branch.

    Inside the cave: infers agent from current branch.
    On the host: infers from current branch or explicit agent_id.
    Creates the target branch on cave if it doesn't exist.
    """
    if config.parse_agent_branch(name):
        raise SystemExit(
            f"Cannot target agent branch '{name}'.\n"
            f"Use a shared branch name."
        )

    current = git.current_branch(cwd=base)
    parsed = config.parse_agent_branch(current)

    if parsed:
        old_target, aid = parsed
        if agent_id and agent_id != aid:
            raise SystemExit(
                f"On branch for agent {aid}, but targeting agent {agent_id}.\n"
                f"Switch to the correct agent branch first."
            )
        agent_id = aid
    elif agent_id:
        old_target = _find_agent_target(base, agent_id)
    else:
        raise SystemExit(
            "Not on an agent branch.\n"
            "Usage: cav checkout <branch> <agent-id>"
        )

    if old_target == name:
        raise SystemExit(f"Agent {agent_id} already targeting '{name}'.")

    old_branch = config.agent_branch(old_target, agent_id)
    new_branch = config.agent_branch(name, agent_id)

    _ensure_target_exists(base, name, old_target)

    if parsed:
        _retarget_local(base, name, old_branch, new_branch, agent_id)
    else:
        _retarget_remote(base, name, old_branch, new_branch, agent_id)

    print(f"Retargeted agent {agent_id}: {old_target} -> {name}")


def _find_agent_target(base: Path, agent_id: str) -> str:
    """Find the current target for an agent from remote branches."""
    git.git("fetch", "--prune", config.SHARED_REMOTE, cwd=base, **config.v())
    branches = git.remote_branches(config.SHARED_REMOTE, cwd=base)
    for b in branches:
        parsed = config.parse_agent_branch(b)
        if parsed and parsed[1] == agent_id:
            return parsed[0]
    raise SystemExit(f"No agent branch found for agent {agent_id}.")


def _ensure_target_exists(base: Path, name: str, old_target: str):
    """Create the target branch on cave if it doesn't exist yet."""
    git.git("fetch", config.SHARED_REMOTE, cwd=base, **config.v())
    remote_ref = f"{config.SHARED_REMOTE}/{name}"
    exists = git.git(
        "rev-parse", "--verify", remote_ref,
        cwd=base, check=False, capture=True, echo=False,
    ).returncode == 0
    if exists:
        return

    # Create from the old target branch.
    old_ref = f"{config.SHARED_REMOTE}/{old_target}"
    try:
        sha = git.git_output("rev-parse", old_ref, cwd=base)
    except Exception:
        raise SystemExit(f"Cannot resolve '{old_ref}' to create '{name}'.")

    git.git("push", config.SHARED_REMOTE,
            f"{sha}:refs/heads/{name}", cwd=base, **config.v())


def _retarget_local(base: Path, new_target: str,
                    old_branch: str, new_branch: str, agent_id: str):
    """Retarget from inside the cave (agent has local checkout)."""
    git.git("branch", "-m", old_branch, new_branch, cwd=base)

    # Try rebase onto new target. Not fatal if it fails.
    remote_target = f"{config.SHARED_REMOTE}/{new_target}"
    r = git.git("rebase", remote_target, cwd=base,
                check=False, capture=True, echo=False)
    if r.returncode != 0:
        git.git("rebase", "--abort", cwd=base,
                check=False, capture=True, echo=False)

    # Push new branch, delete old.
    git.git("push", "--force", config.SHARED_REMOTE,
            f"HEAD:refs/heads/{new_branch}",
            f":refs/heads/{old_branch}",
            cwd=base, **config.v())


def _retarget_remote(base: Path, new_target: str,
                     old_branch: str, new_branch: str, agent_id: str):
    """Retarget from the host (modify bare repo and workspace directly)."""
    shared = config.shared_repo_path(base)
    git.git("branch", "-m", old_branch, new_branch, cwd=shared)

    slug = config.project_slug(base)
    ws = config.worktree_dir(slug, agent_id)
    if ws.exists():
        git.git("branch", "-m", old_branch, new_branch, cwd=ws)
