"""cav approve: merge agent work into a shared branch."""

from pathlib import Path

from . import config, git, registry


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
                f"  or switch to an agent branch first."
            )
        target = config.resolve_target(branch)
        agent_id = parsed[1]
    name = registry.display_name(base, agent_id)

    git.git("fetch", "--prune", config.CAVE_REMOTE, cwd=base, **config.v())

    remote_agent = f"{config.CAVE_REMOTE}/{branch}"
    remote_target = f"{config.CAVE_REMOTE}/{target}"

    try:
        after_sha = git.git_output("rev-parse", remote_agent, cwd=base)
    except Exception:
        raise SystemExit(f"No remote branch '{remote_agent}'.")

    # Target branch may not exist yet (first commit in repo)
    r = git.git("rev-parse", "--verify", remote_target,
                cwd=base, check=False, capture=True, echo=False)
    creating_branch = r.returncode != 0
    before_sha = None if creating_branch else r.stdout.strip()

    if not creating_branch:
        if before_sha == after_sha:
            print(f"No commits to approve on {branch}.")
            return

        # Agent branch must be a fast-forward from the target
        r = git.git("merge-base", "--is-ancestor", before_sha, after_sha,
                    cwd=base, check=False, capture=True, echo=False)
        if r.returncode != 0:
            raise SystemExit(
                f"Cannot approve: {branch} is not a fast-forward from {target}.\n"
                f"The agent must rebase first:\n"
                f"  git fetch cave && git rebase cave/{target}"
            )

    if creating_branch:
        commits = git.commit_oneline(remote_agent, cwd=base)
    else:
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

    # Push to cave (the approval)
    git.git("push", config.CAVE_REMOTE,
            f"{after_sha}:refs/heads/{target}", cwd=base, **config.v())

    # Record one approval per commit (enables per-commit rejection)
    slug = config.project_slug(base)
    if creating_branch:
        range_spec = after_sha
    else:
        range_spec = f"{before_sha}..{after_sha}"
    shas = git.git_output(
        "rev-list", "--reverse", range_spec, cwd=base
    ).splitlines()
    NULL_SHA = "0" * 40
    prev = before_sha or NULL_SHA
    for sha in shas:
        registry.record_approval(slug, target, agent_id, prev, sha)
        prev = sha

    # Courtesy: sync local target branch (best-effort)
    if creating_branch:
        git.git("checkout", "-B", target, after_sha,
                cwd=base, echo=False, capture=True, check=False)
    elif current == target:
        r = git.git("merge", "--ff-only", after_sha,
                    cwd=base, echo=False, capture=True, check=False)
        if r.returncode != 0:
            print(f"Approved and merged to cave, but local '{target}' could not be updated.")
            print(f"Run 'cav pull' to sync.")
    else:
        git.git("branch", "-f", target, after_sha,
                cwd=base, echo=False, capture=True, check=False)
        git.git("checkout", target,
                cwd=base, echo=False, capture=True, check=False)
    git.git("branch", "-D", branch,
            cwd=base, echo=False, capture=True, check=False)

    if not registry.is_agent_running(agent_id):
        from .cli import _auto_cleanup_if_clean
        _auto_cleanup_if_clean(base, agent_id)
        if agent_id not in [a for a in registry.all_agent_ids()]:
            print(f"Approved and removed {name}.")
        else:
            print(f"Approved (agent has work in other projects).")
    else:
        print(f"Approved.")
