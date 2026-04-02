"""cav reject: reject the tip commit of a base branch."""

from pathlib import Path

from . import config, git, registry


def reject(base: Path, agent_id: str | None = None, yes: bool = False):
    branch = git.current_branch(cwd=base)
    if config.parse_dev_branch(branch):
        raise SystemExit(
            f"Cannot reject from dev branch '{branch}'.\n"
            f"Switch to a base branch first (e.g. 'git checkout main')."
        )

    git.git("fetch", "--prune", config.CAVE_SHARED_REMOTE, cwd=base, **config.v())

    remote_base = f"{config.CAVE_SHARED_REMOTE}/{branch}"
    try:
        tip_sha = git.git_output("rev-parse", remote_base, cwd=base)
    except Exception:
        raise SystemExit(f"No remote branch '{remote_base}'.")

    local_sha = git.git_output("rev-parse", "HEAD", cwd=base)
    if local_sha != tip_sha:
        raise SystemExit(
            f"Local '{branch}' is not in sync with '{remote_base}'.\n"
            f"Run 'cav pull' first."
        )

    # Check tip hasn't been pushed to origin
    try:
        r = git.git("merge-base", "--is-ancestor", tip_sha, f"origin/{branch}",
                     cwd=base, check=False, capture=True, echo=False)
        if r.returncode == 0:
            raise SystemExit(
                f"Cannot reject: tip commit is already on origin/{branch}.\n"
                f"Use 'git revert' instead."
            )
    except SystemExit:
        raise
    except Exception:
        pass  # origin doesn't exist or branch not tracked, OK to proceed

    slug = config.project_slug(base)
    approval = registry.lookup_approval_by_after(slug, branch, tip_sha)
    if not approval:
        raise SystemExit(
            f"No approval record found for the tip of '{branch}'.\n"
            f"Only approved commits can be rejected."
        )

    before_sha = approval["before_sha"]
    original_agent_id = approval["agent_id"]

    base_agent = _resolve_target_agent(
        base, branch, agent_id, original_agent_id, tip_sha)

    commits = git.commit_oneline(f"{before_sha}..{tip_sha}", cwd=base)
    n = len(commits)
    word = "commit" if n == 1 else "commits"
    target_name = registry.display_name(base, base_agent)
    print(f"\nRejecting {n} {word} from tip of {branch}:\n")
    for line in commits:
        print(f"  {line}")
    print(f"\nWork will be assigned to {target_name}.")

    if not yes:
        try:
            answer = input("\nReject? [Y/n] ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nAborted.")
        if answer.strip().lower() in ("n", "no"):
            raise SystemExit("Aborted.")

    # Create dev branch on shared remote (before resetting, so commits stay reachable).
    # Skip if the dev branch already contains this commit (stacked rejection).
    ab = config.dev_branch(branch, base_agent)
    if not _agent_covers_tip(base, ab, tip_sha):
        git.git("push", "--force", config.CAVE_SHARED_REMOTE,
                f"{tip_sha}:refs/heads/{ab}", cwd=base, **config.v())

    # Reset base branch on shared remote
    git.git("push", "--force", config.CAVE_SHARED_REMOTE,
            f"{before_sha}:refs/heads/{branch}", cwd=base, **config.v())

    # Update local branch
    git.git("reset", "--hard", before_sha, cwd=base, **config.v())

    registry.delete_approval(approval["id"])

    print(f"\nRejected. Agent branch '{ab}' restored for rework.")


def _agent_covers_tip(base, agent_branch_name, tip_sha):
    """True if the dev branch already contains tip_sha (from a prior rejection)."""
    remote_agent = f"{config.CAVE_SHARED_REMOTE}/{agent_branch_name}"
    try:
        git.git_output("rev-parse", remote_agent, cwd=base)
    except Exception:
        return False
    r = git.git("merge-base", "--is-ancestor", tip_sha, remote_agent,
                cwd=base, check=False, capture=True, echo=False)
    return r.returncode == 0


def _resolve_target_agent(base, branch, agent_id, original_agent_id, tip_sha):
    """Determine which agent should handle the rejected work."""
    if agent_id:
        if agent_id not in registry.all_agent_ids():
            raise SystemExit(f"Agent {agent_id} not found in registry.")
        _check_no_unapproved(base, branch, agent_id, tip_sha)
        return agent_id

    if original_agent_id not in registry.all_agent_ids():
        raise SystemExit(
            f"Original agent {original_agent_id} is no longer in the registry.\n"
            f"Start or continue an agent session, then re-run:\n"
            f"  cav reject <agent-id>"
        )

    _check_no_unapproved(base, branch, original_agent_id, tip_sha)
    return original_agent_id


def _check_no_unapproved(base, base_branch, agent_id, tip_sha):
    """Fail if the agent has unapproved work on the base branch.

    Allows stacking: if tip_sha is already an ancestor of the dev branch
    (from a prior rejection in the same chain), the rejection proceeds.
    """
    ab = config.dev_branch(base_branch, agent_id)
    remote_agent = f"{config.CAVE_SHARED_REMOTE}/{ab}"
    remote_base = f"{config.CAVE_SHARED_REMOTE}/{base_branch}"
    try:
        git.git_output("rev-parse", remote_agent, cwd=base)
    except Exception:
        return  # branch doesn't exist, no conflict
    if not git.has_merge_base(remote_agent, remote_base, cwd=base):
        raise SystemExit(
            f"Agent {agent_id}'s branch '{ab}' has diverged from '{base_branch}'.\n"
            f"Choose an agent with no existing work on '{base_branch}'."
        )
    if git.cherry_undelivered(remote_base, remote_agent, cwd=base):
        if _agent_covers_tip(base, ab, tip_sha):
            return  # tip already on dev branch from prior rejection
        raise SystemExit(
            f"Agent {agent_id} has unapproved work on '{ab}'.\n"
            f"Approve or remove it first, or choose a different agent."
        )
