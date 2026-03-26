"""cav pull: fetch from cave and fast-forward merge."""

import subprocess
from pathlib import Path

from . import config, git


def pull(base: Path, force: bool = False, branch: str | None = None):
    git.git("fetch", "--prune", config.SHARED_REMOTE, cwd=base)

    local_branch = git.current_branch(cwd=base)
    parsed = config.parse_agent_branch(local_branch)

    if parsed and not branch:
        # On an agent branch: pull the target and rebase
        target = parsed[0]
        _pull_agent(base, local_branch, target, force)
    else:
        # On a shared branch (or explicit branch arg): ff-merge
        target_branch = branch or local_branch
        if force and not config.parse_agent_branch(target_branch):
            raise SystemExit(
                f"--force is only allowed on agent branches, not '{target_branch}'."
            )
        _pull_shared(base, local_branch, target_branch, force)


def _pull_agent(base: Path, local_branch: str, target: str, force: bool):
    """Pull target branch updates and rebase the agent branch onto it."""
    remote_ref = f"{config.SHARED_REMOTE}/{target}"

    try:
        git.git_output("rev-parse", remote_ref, cwd=base)
    except Exception:
        print(f"No remote branch '{remote_ref}'.")
        return

    # Also fast-forward the local target branch if it exists
    try:
        git.git_output("rev-parse", target, cwd=base)
        git.git("branch", "-f", target, remote_ref, cwd=base, echo=False,
                check=False, capture=True)
    except Exception:
        pass

    # Check if already up to date
    local_sha = git.git_output("rev-parse", local_branch, cwd=base)
    try:
        merge_base = git.git_output("merge-base", local_branch, remote_ref, cwd=base)
    except subprocess.CalledProcessError:
        merge_base = None

    remote_sha = git.git_output("rev-parse", remote_ref, cwd=base)
    if merge_base == remote_sha:
        print("Already up to date.")
        return

    # Count incoming commits on target
    ahead = None
    try:
        ahead = git.git_output(
            "log", "--oneline", f"{merge_base}..{remote_ref}", cwd=base)
    except Exception:
        pass

    if force:
        git.git("reset", "--hard", remote_ref, cwd=base)
    else:
        git.git("rebase", remote_ref, cwd=base)

    if ahead:
        count = len(ahead.splitlines())
        n = "commit" if count == 1 else "commits"
        print(f"\n{count} new {n} on {target}:")
        print(ahead)


def _pull_shared(base: Path, local_branch: str, target_branch: str, force: bool):
    """Fast-forward merge a shared branch."""
    remote_ref = f"{config.SHARED_REMOTE}/{target_branch}"

    try:
        git.git_output("rev-parse", remote_ref, cwd=base)
    except Exception:
        print(f"No remote branch '{remote_ref}'.")
        return

    local_sha = git.git_output("rev-parse", local_branch, cwd=base)
    remote_sha = git.git_output("rev-parse", remote_ref, cwd=base)

    if local_sha == remote_sha:
        print("Already up to date.")
        return

    # Show incoming commits
    ahead = git.git_output(
        "log", "--oneline", f"{local_branch}..{remote_ref}", cwd=base)
    count = len(ahead.splitlines())

    if force:
        git.git("reset", "--hard", remote_ref, cwd=base)
    else:
        try:
            git.git("merge", "--ff-only", remote_ref, cwd=base)
        except subprocess.CalledProcessError:
            print(f"\nOn branch {local_branch}")
            print(f"Diverged from {remote_ref}.")
            print(f"  cav pull --force                # accept remote version")
            raise SystemExit(1)

    n = "commit" if count == 1 else "commits"
    print(f"\n{count} new {n} on {target_branch}:")
    print(ahead)
