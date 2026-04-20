"""cav approve: merge agent work into a base branch."""

from pathlib import Path

from . import push
from .. import config, registry
from ..constants import CAVE_SHARED_REMOTE
from ..git import git


def approve(base: Path, agent_id: str | None = None, yes: bool = False,
            commit: str | None = None):
    slug = config.project_slug(base)

    git.git("fetch", "--prune", CAVE_SHARED_REMOTE, cwd=base,
            **config.v())

    # Build flat list of (branch, base_name, agent_id) candidates.
    candidates = []
    for rb in git.remote_branches(CAVE_SHARED_REMOTE, cwd=base):
        parsed = config.parse_dev_branch(rb)
        if parsed:
            aid = parsed[1]
            if agent_id and aid != agent_id:
                continue
            candidates.append((rb, config.resolve_base(rb), aid))

    if not candidates:
        if agent_id:
            raise SystemExit(
                f"No commits to approve for agent {agent_id} in {slug}.")
        raise SystemExit(f"No agent branches in {slug}.")

    # Disambiguate by current branch if checked out.
    if len(candidates) > 1:
        current = git.current_branch(cwd=base)
        matching = [c for c in candidates if c[0] == current]
        if matching:
            candidates = matching

    if len(candidates) == 1:
        branch, base_name, agent_id = candidates[0]
    else:
        branch, base_name, agent_id = _pick(
            candidates, base, slug, yes=yes)

    name = registry.display_name(base, agent_id)
    remote_dev = f"{CAVE_SHARED_REMOTE}/{branch}"
    remote_base = f"{CAVE_SHARED_REMOTE}/{base_name}"

    try:
        dev_tip = git.git_output("rev-parse", remote_dev, cwd=base)
    except Exception:
        raise SystemExit(f"No remote branch '{remote_dev}'.")

    r = git.git("rev-parse", "--verify", remote_base,
                cwd=base, check=False, capture=True, echo=False)
    creating = r.returncode != 0
    before_sha = None if creating else r.stdout.strip()

    if not creating:
        if before_sha == dev_tip:
            raise SystemExit(f"No commits to approve for agent {agent_id} "
                             f"in {slug}.")
        r = git.git("merge-base", "--is-ancestor", before_sha, dev_tip,
                    cwd=base, check=False, capture=True, echo=False)
        if r.returncode != 0:
            raise SystemExit(
                f"Cannot approve: {branch} is not a fast-forward from "
                f"{base_name}.\n"
                f"The agent must rebase onto {base_name} first.")

    # Resolve partial approval commit.
    if commit:
        after_sha = _resolve_partial(commit, before_sha, dev_tip, cwd=base)
    else:
        after_sha = dev_tip
    partial = after_sha != dev_tip

    if creating:
        all_commits = git.commit_oneline(remote_dev, cwd=base)
    else:
        all_commits = git.commit_oneline(
            f"{remote_base}..{remote_dev}", cwd=base)

    if partial:
        # Show only commits being approved.
        if creating:
            commits = git.commit_oneline(after_sha, cwd=base)
        else:
            commits = git.commit_oneline(
                f"{remote_base}..{after_sha}", cwd=base)
        remaining = len(all_commits) - len(commits)
        n = len(commits)
        word = "commit" if n == 1 else "commits"
        print(f"\nApproving {n} of {len(all_commits)} {word} from "
              f"{name} into {slug}/{base_name}:\n")
        for line in commits:
            print(f"  {line}")
        print(f"\n  ({remaining} remaining on {branch})")
    else:
        commits = all_commits
        n = len(commits)
        word = "commit" if n == 1 else "commits"
        print(f"\n{n} {word} from {name} to merge into "
              f"{slug}/{base_name}:\n")
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
                before_sha, after_sha, creating, partial=partial)

    print(f"Approved.")


def _pick(candidates, base, slug, yes):
    """Interactive picker when multiple agent/branch combos exist."""
    if yes:
        ids = sorted(set(aid for _, _, aid in candidates))
        raise SystemExit(
            f"Multiple choices in {slug}: agents {', '.join(ids)}\n"
            f"Specify which: cav approve <agent-id>")

    print(f"\nPending work in {slug}:\n")
    for i, (branch, base_name, aid) in enumerate(candidates, 1):
        name = registry.display_name(base, aid)
        remote_dev = f"{CAVE_SHARED_REMOTE}/{branch}"
        remote_base = f"{CAVE_SHARED_REMOTE}/{base_name}"
        r = git.git("rev-parse", "--verify", remote_base,
                    cwd=base, check=False, capture=True, echo=False)
        if r.returncode == 0:
            commits = git.commit_oneline(
                f"{remote_base}..{remote_dev}", cwd=base)
        else:
            commits = git.commit_oneline(remote_dev, cwd=base)
        n = len(commits)
        word = "commit" if n == 1 else "commits"
        print(f"  {i}) {name} \u2192 {base_name}    {n} {word}")
        for line in commits[:3]:
            print(f"     {line}")
        if n > 3:
            print(f"     ... and {n - 3} more")

    try:
        answer = input(f"\nApprove which? [1-{len(candidates)}] ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\nAborted.")
    try:
        idx = int(answer.strip()) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]
    except ValueError:
        pass
    raise SystemExit("Invalid selection.")


def _resolve_partial(sha_prefix, before_sha, dev_tip, cwd):
    """Resolve a commit ref within the approvable range."""
    try:
        full_sha = git.git_output("rev-parse", "--verify", sha_prefix, cwd=cwd)
    except Exception:
        raise SystemExit(f"Cannot resolve commit: {sha_prefix}")
    # Must be an ancestor of dev tip.
    r = git.git("merge-base", "--is-ancestor", full_sha, dev_tip,
                cwd=cwd, check=False, capture=True, echo=False)
    if r.returncode != 0:
        raise SystemExit(f"Commit {sha_prefix} is not on the dev branch.")
    # Must be a descendant of the base (if base exists).
    if before_sha:
        r = git.git("merge-base", "--is-ancestor", before_sha, full_sha,
                    cwd=cwd, check=False, capture=True, echo=False)
        if r.returncode != 0:
            raise SystemExit(
                f"Commit {sha_prefix} is not after the base branch tip.")
        if full_sha == before_sha:
            raise SystemExit("Nothing to approve: commit is the current base.")
    return full_sha


def _do_approve(slug, project_base, branch, base_name, agent_id,
                before_sha, after_sha, creating, partial=False):
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
    if not partial:
        git.git("branch", "-D", branch,
                cwd=project_base, echo=False, capture=True, check=False)
