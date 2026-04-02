"""cav pull: pull dev branch from shared remote."""

import subprocess
from pathlib import Path

from . import config, git


def pull(base: Path, force: bool = False, branch: str | None = None):
    git.git("fetch", "--prune", config.CAVE_SHARED_REMOTE, cwd=base, **config.v())

    local_branch = git.current_branch(cwd=base)
    parsed = config.parse_dev_branch(local_branch)

    if parsed and not branch:
        # On a dev branch: always reset to remote (agent is authoritative)
        base_name = config.resolve_base(local_branch)
        _pull_dev(base, local_branch, base_name)
    else:
        # On a base branch (or explicit branch arg): ff-merge
        base_branch = branch or local_branch
        if force and not config.parse_dev_branch(base_branch):
            raise SystemExit(
                f"--force is only allowed on dev branches, not '{base_branch}'."
            )
        _pull_base(base, local_branch, base_branch, force)


def _pull_dev(base: Path, local_branch: str, base_name: str):
    """Reset dev branch to match remote. Dev branches are ephemeral on the
    host, the cave agent is always authoritative."""
    remote_dev = f"{config.CAVE_SHARED_REMOTE}/{local_branch}"
    remote_base = f"{config.CAVE_SHARED_REMOTE}/{base_name}"

    # Update local base branch if it exists
    try:
        git.git_output("rev-parse", remote_base, cwd=base)
        try:
            git.git_output("rev-parse", base_name, cwd=base)
            git.git("branch", "-f", "--no-track", base_name, remote_base,
                    cwd=base, echo=False, check=False, capture=True)
        except Exception:
            pass
    except Exception:
        pass

    # Reset dev branch to remote
    try:
        git.git_output("rev-parse", remote_dev, cwd=base)
    except Exception:
        print(f"No remote branch '{remote_dev}'.")
        return

    local_sha = git.git_output("rev-parse", local_branch, cwd=base)
    remote_sha = git.git_output("rev-parse", remote_dev, cwd=base)

    if local_sha == remote_sha:
        print("Already up to date.")
        return

    git.git("reset", "--hard", remote_dev, cwd=base, **config.v())
    print(f"Reset '{local_branch}' to '{remote_dev}'.")

    # Show what's on the dev branch relative to base
    try:
        log = git.git_output(
            "log", "--oneline", f"{remote_base}..{remote_dev}", cwd=base)
    except Exception:
        log = None
    if log:
        count = len(log.splitlines())
        n = "commit" if count == 1 else "commits"
        print(f"{count} {n} ahead of {base_name}:")
        print(log)


def _pull_base(base: Path, local_branch: str, base_branch: str, force: bool):
    """Fast-forward merge a base branch."""
    remote_base = f"{config.CAVE_SHARED_REMOTE}/{base_branch}"

    try:
        git.git_output("rev-parse", remote_base, cwd=base)
    except Exception:
        print(f"No remote branch '{remote_base}'.")
        return

    local_sha = git.git_output("rev-parse", local_branch, cwd=base)
    remote_sha = git.git_output("rev-parse", remote_base, cwd=base)

    if local_sha == remote_sha:
        print("Already up to date.")
        return

    # Show incoming commits
    ahead = git.git_output(
        "log", "--oneline", f"{local_branch}..{remote_base}", cwd=base)

    if not ahead:
        print("Already up to date.")
        return

    count = len(ahead.splitlines())

    if force:
        git.git("reset", "--hard", remote_base, cwd=base, **config.v())
    else:
        try:
            git.git("merge", "--ff-only", remote_base, cwd=base, **config.v())
        except subprocess.CalledProcessError:
            print(f"\nOn branch {local_branch}")
            print(f"Diverged from {remote_base}.")
            raise SystemExit(1)

    n = "commit" if count == 1 else "commits"
    print(f"\n{count} new {n} on {base_branch}:")
    print(ahead)
