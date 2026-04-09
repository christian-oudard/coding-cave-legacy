"""cav approve: merge agent work into a base branch."""

from pathlib import Path

from . import config, git, push, registry


def approve(base: Path, agent_id: str | None = None, yes: bool = False):
    if not agent_id:
        raise SystemExit(
            "No agent to approve. Either:\n"
            "  cav approve <agent-id>\n"
            "  check out a dev branch first")

    slug = config.project_slug(base)
    name = registry.display_name(base, agent_id)

    git.git("fetch", "--prune", config.CAVE_SHARED_REMOTE, cwd=base,
            **config.v())

    # Find this agent's dev branches in the project.
    dev_branches = []
    for branch in git.remote_branches(config.CAVE_SHARED_REMOTE, cwd=base):
        parsed = config.parse_dev_branch(branch)
        if parsed and parsed[1] == agent_id:
            dev_branches.append((branch, config.resolve_base(branch)))

    if not dev_branches:
        raise SystemExit(f"No commits to approve for agent {agent_id} "
                         f"in {slug}.")

    if len(dev_branches) > 1:
        lines = "\n".join(f"  {b}" for b, _ in dev_branches)
        raise SystemExit(
            f"Agent {agent_id} has multiple dev branches in {slug}:\n"
            f"{lines}\n"
            f"Check out the branch to approve and run: cav approve")

    branch, base_name = dev_branches[0]
    remote_dev = f"{config.CAVE_SHARED_REMOTE}/{branch}"
    remote_base = f"{config.CAVE_SHARED_REMOTE}/{base_name}"

    try:
        after_sha = git.git_output("rev-parse", remote_dev, cwd=base)
    except Exception:
        raise SystemExit(f"No remote branch '{remote_dev}'.")

    r = git.git("rev-parse", "--verify", remote_base,
                cwd=base, check=False, capture=True, echo=False)
    creating = r.returncode != 0
    before_sha = None if creating else r.stdout.strip()

    if not creating:
        if before_sha == after_sha:
            raise SystemExit(f"No commits to approve for agent {agent_id} "
                             f"in {slug}.")
        r = git.git("merge-base", "--is-ancestor", before_sha, after_sha,
                    cwd=base, check=False, capture=True, echo=False)
        if r.returncode != 0:
            raise SystemExit(
                f"Cannot approve: {branch} is not a fast-forward from "
                f"{base_name}.\n"
                f"The agent must rebase onto {base_name} first.")

    if creating:
        commits = git.commit_oneline(remote_dev, cwd=base)
    else:
        commits = git.commit_oneline(f"{remote_base}..{remote_dev}", cwd=base)
    n = len(commits)
    word = "commit" if n == 1 else "commits"
    print(f"\n{n} {word} from {name} to merge into {slug}/{base_name}:\n")
    for line in commits:
        print(f"  {line}")

    if not yes:
        try:
            answer = input(f"\nApprove? [Y/n] ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nAborted.")
        if answer.strip().lower() in ("n", "no"):
            raise SystemExit("Aborted.")

    _do_approve(slug, base, branch, base_name, agent_id,
                before_sha, after_sha, creating)

    print(f"Approved.")


def _do_approve(slug, project_base, branch, base_name, agent_id,
                before_sha, after_sha, creating):
    """Push approval and record it for one project/branch."""
    push.push_base(project_base, base_name,
                   refspec=f"{after_sha}:refs/heads/{base_name}")

    # Record one approval per commit.
    if creating:
        range_spec = after_sha
    else:
        range_spec = f"{before_sha}..{after_sha}"
    shas = git.git_output(
        "rev-list", "--reverse", range_spec, cwd=project_base
    ).splitlines()
    NULL_SHA = "0" * 40
    prev = before_sha or NULL_SHA
    for sha in shas:
        registry.record_approval(slug, base_name, agent_id, prev, sha)
        prev = sha

    # Courtesy: sync local base branch (best-effort).
    current = git.current_branch(cwd=project_base)
    if creating:
        git.git("checkout", "-B", base_name, after_sha,
                cwd=project_base, echo=False, capture=True, check=False)
    elif current == base_name:
        r = git.git("merge", "--ff-only", after_sha,
                    cwd=project_base, echo=False, capture=True, check=False)
        if r.returncode != 0:
            print(f"Approved, but local '{base_name}' could not "
                  f"be updated. Run 'cav pull' to sync.")
    else:
        git.git("branch", "-f", base_name, after_sha,
                cwd=project_base, echo=False, capture=True, check=False)
        git.git("checkout", base_name,
                cwd=project_base, echo=False, capture=True, check=False)
    git.git("branch", "-D", branch,
            cwd=project_base, echo=False, capture=True, check=False)
