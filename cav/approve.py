"""cav approve: merge agent work into a shared branch."""

from pathlib import Path

from . import config, git


def approve(base: Path, yes: bool = False):
    branch = git.current_branch(cwd=base)
    parsed = config.parse_agent_branch(branch)
    if not parsed:
        raise SystemExit(
            f"Not on an agent branch (current: '{branch}').\n"
            f"Switch to an agent branch first (e.g. 'git checkout agent-1/main')."
        )

    target = config.resolve_target(branch)
    _, agent_id = parsed
    name = config.display_name(base, agent_id)

    git.git("fetch", "--prune", config.SHARED_REMOTE, cwd=base)

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
            answer = input(f"\nApprove into {target}? [Y/n] ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nAborted.")
        if answer.strip().lower() in ("n", "no"):
            raise SystemExit("Aborted.")

    # Push agent branch to shared branch (as user, no agent identity)
    git.git("push", config.SHARED_REMOTE,
            f"{after_sha}:refs/heads/{target}", cwd=base)

    # Record one approval per commit (enables per-commit rejection)
    slug = config.project_slug(base)
    shas = git.git_output(
        "rev-list", "--reverse", f"{before_sha}..{after_sha}", cwd=base
    ).splitlines()
    prev = before_sha
    for sha in shas:
        config.record_approval(slug, target, agent_id, prev, sha)
        prev = sha

    # Update local target branch and switch to it
    git.git("branch", "-f", target, after_sha,
            cwd=base, echo=False, capture=True, check=False)
    git.git("checkout", target, cwd=base)
    git.git("branch", "-D", branch,
            cwd=base, echo=False, capture=True, check=False)

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
        print(f"\nApproved. {name} continues working.")
