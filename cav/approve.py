"""cav approve: merge agent work into a base branch."""

from pathlib import Path

from . import config, git, push, registry


def approve(base: Path, agent_id: str | None = None, yes: bool = False):
    current = git.current_branch(cwd=base)
    if agent_id:
        # Explicit agent ID: resolve base branch from current branch
        parsed = config.parse_dev_branch(current)
        base_name = parsed[0] if parsed else current
        branch = config.dev_branch(base_name, agent_id)
    else:
        # No agent ID: must be on a dev branch
        branch = current
        parsed = config.parse_dev_branch(branch)
        if not parsed:
            raise SystemExit(
                f"Not on a dev branch (current: '{branch}').\n"
                f"Usage: cav approve <agent-id>  (from any branch)\n"
                f"  or switch to a dev branch first."
            )
        base_name = config.resolve_base(branch)
        agent_id = parsed[1]
    name = registry.display_name(base, agent_id)

    git.git("fetch", "--prune", config.CAVE_SHARED_REMOTE, cwd=base, **config.v())

    remote_dev = f"{config.CAVE_SHARED_REMOTE}/{branch}"
    remote_base = f"{config.CAVE_SHARED_REMOTE}/{base_name}"

    try:
        after_sha = git.git_output("rev-parse", remote_dev, cwd=base)
    except Exception:
        raise SystemExit(f"No remote branch '{remote_dev}'.")

    # Base branch may not exist yet (first commit in repo)
    r = git.git("rev-parse", "--verify", remote_base,
                cwd=base, check=False, capture=True, echo=False)
    creating_branch = r.returncode != 0
    before_sha = None if creating_branch else r.stdout.strip()

    if not creating_branch:
        if before_sha == after_sha:
            print(f"No commits to approve on {branch}.")
            return

        # Dev branch must be a fast-forward from the base
        r = git.git("merge-base", "--is-ancestor", before_sha, after_sha,
                    cwd=base, check=False, capture=True, echo=False)
        if r.returncode != 0:
            raise SystemExit(
                f"Cannot approve: {branch} is not a fast-forward from {base_name}.\n"
                f"The agent must rebase onto {base_name} first."
            )

    if creating_branch:
        commits = git.commit_oneline(remote_dev, cwd=base)
    else:
        commits = git.commit_oneline(f"{remote_base}..{remote_dev}", cwd=base)
    n = len(commits)
    word = "commit" if n == 1 else "commits"
    print(f"\n{n} {word} from {name} to approve into {base_name}:\n")
    for line in commits:
        print(f"  {line}")

    if not yes:
        try:
            answer = input(f"\nApprove and merge into {base_name}? [Y/n] ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nAborted.")
        if answer.strip().lower() in ("n", "no"):
            raise SystemExit("Aborted.")

    # Push to shared remote (the approval) and rebase other agents.
    push.push_base(base, base_name, refspec=f"{after_sha}:refs/heads/{base_name}")

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
        registry.record_approval(slug, base_name, agent_id, prev, sha)
        prev = sha

    # Courtesy: sync local base branch (best-effort)
    if creating_branch:
        git.git("checkout", "-B", base_name, after_sha,
                cwd=base, echo=False, capture=True, check=False)
    elif current == base_name:
        r = git.git("merge", "--ff-only", after_sha,
                    cwd=base, echo=False, capture=True, check=False)
        if r.returncode != 0:
            print(f"Approved and merged to shared remote, but local '{base_name}' could not be updated.")
            print(f"Run 'cav pull' to sync.")
    else:
        git.git("branch", "-f", base_name, after_sha,
                cwd=base, echo=False, capture=True, check=False)
        git.git("checkout", base_name,
                cwd=base, echo=False, capture=True, check=False)
    git.git("branch", "-D", branch,
            cwd=base, echo=False, capture=True, check=False)

    print(f"Approved.")
