"""cav push: push current branch to shared remote."""

import subprocess
import sys
from pathlib import Path

from . import config, git


def push(base: Path, force: bool = False):
    branch = git.current_branch(cwd=base)
    if branch == "HEAD":
        git_dir = Path(git.git_output("rev-parse", "--git-dir", cwd=base))
        if not git_dir.is_absolute():
            git_dir = base / git_dir
        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            raise SystemExit(
                "Rebase in progress. Complete or abort it before pushing:\n"
                "  git rebase --continue\n"
                "  git rebase --abort"
            )
        raise SystemExit("Detached HEAD. Check out a branch before pushing.")

    args = ["push", "--no-set-upstream", config.CAVE_SHARED_REMOTE, branch]
    if force:
        args.insert(1, "--force")
    try:
        r = git.git(*args, cwd=base, **config.v())
    except subprocess.CalledProcessError as e:
        if e.stderr:
            print(e.stderr.rstrip(), file=sys.stderr)
        if not force:
            print(f"\n  cav push --force                # overwrite shared remote with local branch")
        raise SystemExit(1)

    if not config.verbose:
        stderr = getattr(r, "stderr", "") or ""
        if "up-to-date" in stderr:
            print(f"Already up to date.")
        else:
            print(f"Pushed {branch} to shared remote.")

    parsed = config.parse_dev_branch(branch)
    if parsed:
        _sync_shared_to_agent(base, branch, parsed[1])
    else:
        _rebase_agent_branches(base, branch)


def _sync_shared_to_agent(base: Path, dev_branch: str, agent_id: str):
    """After pushing a dev branch, update the agent worktree to match."""
    slug = config.project_slug(base)
    ws = config.worktree_dir(slug, agent_id)
    if not ws.exists():
        return

    shared = config.shared_repo_path(base)

    # Fetch the pushed ref into the worktree's remote tracking ref.
    git.git("fetch", str(shared),
            f"+refs/heads/{dev_branch}:refs/remotes/{config.HOST_SHARED_REMOTE}/{dev_branch}",
            cwd=ws, echo=False, check=False, capture=True)

    # Reset worktree if it's on this branch.
    try:
        current = git.current_branch(cwd=ws)
    except Exception:
        return
    if current == dev_branch:
        git.git("reset", "--hard",
                f"{config.HOST_SHARED_REMOTE}/{dev_branch}",
                cwd=ws, echo=False, check=False, capture=True)


def _rebase_agent_branches(base: Path, base_branch: str):
    """After pushing a base branch, rebase agent dev branches onto it."""
    slug = config.project_slug(base)
    shared = config.shared_repo_path(base)
    if not shared.exists():
        return

    # Fetch to update tracking refs so we can list remote branches.
    git.git("fetch", config.CAVE_SHARED_REMOTE, cwd=base,
            echo=False, check=False, capture=True)

    for branch in git.remote_branches(config.CAVE_SHARED_REMOTE, cwd=base):
        parsed = config.parse_dev_branch(branch)
        if not parsed:
            continue
        if config.resolve_base(branch) != base_branch:
            continue

        _, agent_id = parsed
        ws = config.worktree_dir(slug, agent_id)
        if not ws.exists():
            continue

        try:
            current = git.current_branch(cwd=ws)
        except Exception:
            continue
        if current != branch:
            continue  # Can only rebase the checked-out branch

        # Fetch updated base into the agent worktree.
        git.git("fetch", str(shared),
                f"+refs/heads/{base_branch}:refs/remotes/{config.HOST_SHARED_REMOTE}/{base_branch}",
                cwd=ws, echo=False, check=False, capture=True)

        # Rebase dev branch onto updated base.
        r = git.git("rebase", f"{config.HOST_SHARED_REMOTE}/{base_branch}",
                    cwd=ws, check=False, capture=True, echo=False)
        if r.returncode != 0:
            git.git("rebase", "--abort", cwd=ws,
                    check=False, capture=True, echo=False)
            print(f"Warning: rebase of {branch} onto {base_branch} failed (conflicts).")
            continue

        # Push rebased branch back to shared remote.
        git.git("fetch", str(ws),
                f"+refs/heads/{branch}:refs/heads/{branch}",
                cwd=shared, echo=False, check=False, capture=True)

        print(f"Rebased {branch} onto {base_branch}.")
