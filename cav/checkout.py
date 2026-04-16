"""cav checkout: retarget an agent to deliver to a different base branch."""

from pathlib import Path

from . import config, git, workspace
from .constants import CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE


def checkout(base: Path, name: str, agent_id: str | None = None):
    """Retarget an agent to deliver to a different base branch.

    Inside the cave: infers agent from CODING_CAVE_AGENT_NAME.
    On the host: infers from current branch (if it's a dev branch) or
    from explicit agent_id. Creates the base branch on the shared remote
    if it doesn't exist.
    """
    if config.parse_dev_branch(name):
        raise SystemExit(
            f"Cannot use dev branch '{name}' as base.\n"
            f"Use a base branch name (e.g. main, develop)."
        )

    current = git.current_branch(cwd=base)

    if config.inside_cave():
        aid = config.self_agent_id()
        if aid is None:
            raise SystemExit(
                "Cannot determine this cave's agent id "
                "(CODING_CAVE_AGENT_NAME not set).")
        if agent_id and agent_id != aid:
            raise SystemExit(
                f"This cave is agent {aid}, but targeting agent {agent_id}."
            )
        agent_id = aid
        # Inside a cave, the local branch IS the base name.
        old_base = current
    else:
        parsed = config.parse_dev_branch(current)
        if parsed:
            old_base, aid = parsed
            if agent_id and agent_id != aid:
                raise SystemExit(
                    f"On branch for agent {aid}, but targeting agent "
                    f"{agent_id}.\nSwitch to the correct dev branch first."
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

    if config.inside_cave():
        _ensure_base_exists(base, name, old_base, HOST_SHARED_REMOTE)
        _retarget_local(base, old_base, name,
                        old_branch, new_branch, agent_id)
    else:
        _ensure_base_exists(base, name, old_base, CAVE_SHARED_REMOTE)
        _retarget_remote(base, old_base, name,
                         old_branch, new_branch, agent_id)

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


def _retarget_local(base: Path, old_base: str, new_base: str,
                    old_branch: str, new_branch: str, agent_id: str):
    """Retarget from inside the cave. The cave's local branch is the
    base name; rename it and update the push refspec so subsequent
    pushes land on agent-<id>/<new_base>.
    """
    # Drop any stale local <new_base> so the rename won't collide.
    git.git("branch", "-D", new_base, cwd=base,
            echo=False, check=False, capture=True)
    git.git("branch", "-m", old_base, new_base, cwd=base)
    workspace.set_agent_push_refspec(base, new_base, agent_id)

    # Try rebase onto new base. Not fatal if it fails.
    remote_base = f"{HOST_SHARED_REMOTE}/{new_base}"
    r = git.git("rebase", remote_base, cwd=base,
                check=False, capture=True, echo=False)
    if r.returncode != 0:
        git.git("rebase", "--abort", cwd=base,
                check=False, capture=True, echo=False)

    # Track the new base for ergonomic `git rebase` with no args.
    git.git("branch", "--set-upstream-to", remote_base, new_base,
            cwd=base, echo=False, check=False, capture=True)

    # Publish the renamed branch and delete the old agent ref upstream.
    git.git("push", "--force", HOST_SHARED_REMOTE,
            f"refs/heads/{new_base}:refs/heads/{new_branch}",
            f":refs/heads/{old_branch}",
            cwd=base, **config.v())


def _retarget_remote(base: Path, old_base: str, new_base: str,
                     old_branch: str, new_branch: str, agent_id: str):
    """Retarget from the host: rename the agent ref in the bare repo
    and reconfigure the workspace's local branch + push refspec.
    """
    shared = config.shared_repo_path(base)
    git.git("branch", "-m", old_branch, new_branch, cwd=shared)

    slug = config.project_slug(base)
    ws = config.worktree_dir(slug, agent_id)
    if not ws.exists():
        return

    # Pick up the new base ref so we can retarget tracking.
    git.git("fetch", HOST_SHARED_REMOTE, cwd=ws,
            echo=False, check=False, capture=True)
    git.git("branch", "-D", new_base, cwd=ws,
            echo=False, check=False, capture=True)
    git.git("branch", "-m", old_base, new_base, cwd=ws)
    workspace.set_agent_push_refspec(ws, new_base, agent_id)
    git.git("branch", "--set-upstream-to",
            f"{HOST_SHARED_REMOTE}/{new_base}", new_base,
            cwd=ws, echo=False, check=False, capture=True)
