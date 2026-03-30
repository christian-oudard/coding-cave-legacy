"""cav reject: reject the tip commit of a shared branch."""

from pathlib import Path

from . import config, git


def reject(base: Path, agent_id: str | None = None, yes: bool = False):
    branch = git.current_branch(cwd=base)
    if config.parse_agent_branch(branch):
        raise SystemExit(
            f"Cannot reject from agent branch '{branch}'.\n"
            f"Switch to a shared branch first (e.g. 'git checkout main')."
        )

    git.git("fetch", "--prune", config.SHARED_REMOTE, cwd=base, **config.v())

    remote_ref = f"{config.SHARED_REMOTE}/{branch}"
    try:
        tip_sha = git.git_output("rev-parse", remote_ref, cwd=base)
    except Exception:
        raise SystemExit(f"No remote branch '{remote_ref}'.")

    local_sha = git.git_output("rev-parse", "HEAD", cwd=base)
    if local_sha != tip_sha:
        raise SystemExit(
            f"Local '{branch}' is not in sync with '{remote_ref}'.\n"
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
    approval = config.lookup_approval_by_after(slug, branch, tip_sha)
    if not approval:
        raise SystemExit(
            f"No approval record found for the tip of '{branch}'.\n"
            f"Only approved commits can be rejected."
        )

    before_sha = approval["before_sha"]
    original_agent_id = approval["agent_id"]

    target_agent = _resolve_target_agent(base, branch, agent_id, original_agent_id)

    commits = git.commit_oneline(f"{before_sha}..{tip_sha}", cwd=base)
    n = len(commits)
    word = "commit" if n == 1 else "commits"
    target_name = config.display_name(base, target_agent)
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

    # Create agent branch on cave (before resetting, so commits stay reachable)
    ab = config.agent_branch(branch, target_agent)
    git.git("push", "--force", config.SHARED_REMOTE,
            f"{tip_sha}:refs/heads/{ab}", cwd=base, **config.v())

    # Reset shared branch on cave
    git.git("push", "--force", config.SHARED_REMOTE,
            f"{before_sha}:refs/heads/{branch}", cwd=base, **config.v())

    # Update local branch
    git.git("reset", "--hard", before_sha, cwd=base, **config.v())

    config.delete_approval(approval["id"])

    print(f"\nRejected. Agent branch '{ab}' restored for rework.")


def _resolve_target_agent(base, branch, agent_id, original_agent_id):
    """Determine which agent should handle the rejected work."""
    if agent_id:
        if agent_id not in config.all_agent_ids():
            raise SystemExit(f"Agent {agent_id} not found in registry.")
        _check_no_unapproved(base, branch, agent_id)
        return agent_id

    if original_agent_id not in config.all_agent_ids():
        raise SystemExit(
            f"Original agent {original_agent_id} is no longer in the registry.\n"
            f"Start or continue an agent session, then re-run:\n"
            f"  cav reject <agent-id>"
        )

    _check_no_unapproved(base, branch, original_agent_id)
    return original_agent_id


def _check_no_unapproved(base, target_branch, agent_id):
    """Fail if the agent has unapproved work on the target branch."""
    ab = config.agent_branch(target_branch, agent_id)
    remote_agent = f"{config.SHARED_REMOTE}/{ab}"
    remote_target = f"{config.SHARED_REMOTE}/{target_branch}"
    try:
        git.git_output("rev-parse", remote_agent, cwd=base)
    except Exception:
        return  # branch doesn't exist, no conflict
    if not git.has_merge_base(remote_agent, remote_target, cwd=base):
        raise SystemExit(
            f"Agent {agent_id}'s branch '{ab}' has diverged from '{target_branch}'.\n"
            f"Choose an agent with no existing work on '{target_branch}'."
        )
    if git.cherry_undelivered(remote_target, remote_agent, cwd=base):
        raise SystemExit(
            f"Agent {agent_id} has unapproved work on '{ab}'.\n"
            f"Approve or remove it first, or choose a different agent."
        )
