"""cav approve: merge agent work into a shared branch."""

from pathlib import Path

from . import config, git


def approve(base: Path, agent_id: str | None = None, yes: bool = False):
    current = git.current_branch(cwd=base)
    if agent_id:
        # Explicit agent ID: resolve target from current branch
        parsed = config.parse_agent_branch(current)
        target = parsed[0] if parsed else current
        branch = config.agent_branch(target, agent_id)
    else:
        # No agent ID: must be on an agent branch
        branch = current
        parsed = config.parse_agent_branch(branch)
        if not parsed:
            raise SystemExit(
                f"Not on an agent branch (current: '{branch}').\n"
                f"Usage: cav approve <agent-id>  (from any branch)\n"
                f"   or: switch to an agent branch first."
            )
        target = config.resolve_target(branch)
        agent_id = parsed[1]
    name = config.display_name(base, agent_id)

    git.git("fetch", "--prune", config.SHARED_REMOTE, cwd=base, **config.v())

    remote_agent = f"{config.SHARED_REMOTE}/{branch}"
    remote_target = f"{config.SHARED_REMOTE}/{target}"

    try:
        after_sha = git.git_output("rev-parse", remote_agent, cwd=base)
    except Exception:
        raise SystemExit(f"No remote branch '{remote_agent}'.")
    try:
        before_sha = git.git_output("rev-parse", remote_target, cwd=base)
    except Exception:
        raise SystemExit(f"No remote branch '{remote_target}'.")

    # Local target must not be ahead of cave target (would create
    # unresolvable divergence since we don't rebase on the host side)
    r = git.git("rev-parse", "--verify", target,
                cwd=base, check=False, capture=True, echo=False)
    if r.returncode == 0:
        local_sha = r.stdout.strip()
        if local_sha != before_sha:
            is_behind = git.git(
                "merge-base", "--is-ancestor", local_sha, before_sha,
                cwd=base, check=False, capture=True, echo=False,
            ).returncode == 0
            if not is_behind:
                is_ahead = git.git(
                    "merge-base", "--is-ancestor", before_sha, local_sha,
                    cwd=base, check=False, capture=True, echo=False,
                ).returncode == 0
                if is_ahead:
                    raise SystemExit(
                        f"Local '{target}' is ahead of '{remote_target}'.\n"
                        f"Push local commits first: git push {config.SHARED_REMOTE} {target}"
                    )
                else:
                    raise SystemExit(
                        f"Local '{target}' has diverged from '{remote_target}'.\n"
                        f"Run 'cav pull' first to sync with cave."
                    )

    if before_sha == after_sha:
        print(f"No commits to approve on {branch}.")
        return

    # Check fast-forward
    r = git.git("merge-base", "--is-ancestor", before_sha, after_sha,
                cwd=base, check=False, capture=True, echo=False)
    if r.returncode != 0:
        raise SystemExit(
            f"Cannot approve: {branch} is not a fast-forward from {target}.\n"
            f"The agent must rebase first:\n"
            f"  git fetch cave && git rebase cave/{target}"
        )

    commits = git.commit_oneline(f"{remote_target}..{remote_agent}", cwd=base)
    n = len(commits)
    word = "commit" if n == 1 else "commits"
    print(f"\n{n} {word} from {name} to approve into {target}:\n")
    for line in commits:
        print(f"  {line}")

    if not yes:
        try:
            answer = input(f"\nApprove and merge into {target}? [Y/n] ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nAborted.")
        if answer.strip().lower() in ("n", "no"):
            raise SystemExit("Aborted.")

    # Push to cave (the authoritative operation)
    git.git("push", config.SHARED_REMOTE,
            f"{after_sha}:refs/heads/{target}", cwd=base, **config.v())

    # Update local target branch (safe: we verified local is not ahead)
    if current == target:
        git.git("merge", "--ff-only", after_sha, cwd=base, echo=False, capture=True)
    else:
        git.git("branch", "-f", target, after_sha,
                cwd=base, echo=False, capture=True, check=False)
        git.git("checkout", target, cwd=base, **config.v())
    git.git("branch", "-D", branch,
            cwd=base, echo=False, capture=True, check=False)

    # Record one approval per commit (enables per-commit rejection)
    slug = config.project_slug(base)
    shas = git.git_output(
        "rev-list", "--reverse", f"{before_sha}..{after_sha}", cwd=base
    ).splitlines()
    prev = before_sha
    for sha in shas:
        config.record_approval(slug, target, agent_id, prev, sha)
        prev = sha

    if not config.is_agent_running(agent_id):
        from .status import _remove_agent
        branches = git.remote_branches(config.SHARED_REMOTE, cwd=base)
        agent_branches = [
            b for b in branches
            if (p := config.parse_agent_branch(b)) and p[1] == agent_id
        ]
        _remove_agent(base, agent_id, agent_branches)
        print(f"\nApproved and removed {name}.")
    else:
        print(f"\nApproved.")
