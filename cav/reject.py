"""cav reject: reject approved commits from a base branch."""

from pathlib import Path

from . import config, git, registry
from .constants import CAVE_SHARED_REMOTE


def reject(base: Path, agent_id: str | None = None, yes: bool = False,
           commit: str | None = None):
    branch = git.current_branch(cwd=base)
    if config.parse_dev_branch(branch):
        raise SystemExit(
            f"Cannot reject from dev branch '{branch}'.\n"
            f"Switch to a base branch first (e.g. 'git checkout main')."
        )

    git.git("fetch", "--prune", CAVE_SHARED_REMOTE, cwd=base, **config.v())

    remote_base = f"{CAVE_SHARED_REMOTE}/{branch}"
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

    slug = config.project_slug(base)

    if commit:
        # Deep reject: walk approval chain from tip back to the target.
        target_sha = _resolve_reject_target(commit, tip_sha, cwd=base)
        approvals = _collect_approval_chain(slug, branch, tip_sha, target_sha)
    else:
        # Collect all consecutive approvals from the tip.
        all_approvals = _collect_all_approvals(slug, branch, tip_sha)
        if not all_approvals:
            raise SystemExit(
                f"No approval record found for the tip of '{branch}'.\n"
                f"Only approved commits can be rejected.")
        if len(all_approvals) > 1 and not yes:
            approvals = _pick_reject(all_approvals, base, branch)
        else:
            approvals = [all_approvals[0]]

    before_sha = approvals[-1]["before_sha"]
    original_agent_id = approvals[-1]["agent_id"]

    # Check none of the rejected commits have been pushed to origin.
    try:
        r = git.git("merge-base", "--is-ancestor", tip_sha, f"origin/{branch}",
                     cwd=base, check=False, capture=True, echo=False)
        if r.returncode == 0:
            raise SystemExit(
                f"Cannot reject: commits are already on origin/{branch}.\n"
                f"Use 'git revert' instead."
            )
    except SystemExit:
        raise
    except Exception:
        pass  # origin doesn't exist or branch not tracked, OK to proceed

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
        git.git("push", "--force", CAVE_SHARED_REMOTE,
                f"{tip_sha}:refs/heads/{ab}", cwd=base, **config.v())

    # Reset base branch on shared remote
    git.git("push", "--force", CAVE_SHARED_REMOTE,
            f"{before_sha}:refs/heads/{branch}", cwd=base, **config.v())

    # Update local branch
    git.git("reset", "--hard", before_sha, cwd=base, **config.v())

    for a in approvals:
        registry.delete_approval(a["id"])

    print(f"\nRejected. Agent branch '{ab}' restored for rework.")


def _collect_all_approvals(slug, branch, tip_sha):
    """Collect all consecutive approval records from tip backwards."""
    approvals = []
    current = tip_sha
    while True:
        approval = registry.lookup_approval_by_after(slug, branch, current)
        if not approval:
            break
        approvals.append(approval)
        current = approval["before_sha"]
        if current == "0" * 40:
            break
    return approvals


def _pick_reject(approvals, base, branch):
    """Interactive picker showing approved commits to reject."""
    print(f"\nApproved commits on {branch}:\n")
    for i, approval in enumerate(approvals, 1):
        sha = approval["after_sha"]
        aid = approval["agent_id"]
        name = registry.display_name(base, aid)
        msg = git.git_output("log", "--oneline", "-1", sha, cwd=base)
        print(f"  {i}) {msg}  ({name})")

    try:
        answer = input(
            f"\nReject from? [1=tip only, "
            f"{len(approvals)}=all] ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\nAborted.")
    try:
        idx = int(answer.strip())
        if 1 <= idx <= len(approvals):
            return approvals[:idx]
    except ValueError:
        pass
    raise SystemExit("Invalid selection.")


def _resolve_reject_target(sha_prefix, tip_sha, cwd):
    """Resolve a commit ref and validate it's on the branch before the tip."""
    try:
        full_sha = git.git_output("rev-parse", "--verify", sha_prefix, cwd=cwd)
    except Exception:
        raise SystemExit(f"Cannot resolve commit: {sha_prefix}")
    r = git.git("merge-base", "--is-ancestor", full_sha, tip_sha,
                cwd=cwd, check=False, capture=True, echo=False)
    if r.returncode != 0:
        raise SystemExit(f"Commit {sha_prefix} is not on this branch.")
    if full_sha == tip_sha:
        raise SystemExit(
            f"Commit {sha_prefix} is the current tip. "
            f"Use 'cav reject' without --commit.")
    return full_sha


def _collect_approval_chain(slug, branch, tip_sha, target_sha):
    """Collect approval records from tip back to target (inclusive)."""
    approvals = []
    current = tip_sha
    while True:
        approval = registry.lookup_approval_by_after(slug, branch, current)
        if not approval:
            raise SystemExit(
                f"No approval record for {current[:8]}.\n"
                f"Cannot reject past unapproved commits.")
        approvals.append(approval)
        if current == target_sha:
            break
        current = approval["before_sha"]
        if current == "0" * 40:
            raise SystemExit(
                f"Commit {target_sha[:8]} not found in approval chain.\n"
                f"Only approved commits can be rejected.")
    return approvals


def _agent_covers_tip(base, agent_branch_name, tip_sha):
    """True if the dev branch already contains tip_sha (from a prior rejection)."""
    remote_agent = f"{CAVE_SHARED_REMOTE}/{agent_branch_name}"
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
    remote_agent = f"{CAVE_SHARED_REMOTE}/{ab}"
    remote_base = f"{CAVE_SHARED_REMOTE}/{base_branch}"
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
