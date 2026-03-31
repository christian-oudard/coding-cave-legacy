"""cav pull: fetch from cave and fast-forward merge."""

import subprocess
from pathlib import Path

from . import config, git


def pull(base: Path, force: bool = False, branch: str | None = None):
    git.git("fetch", "--prune", config.CAVE_REMOTE, cwd=base, **config.v())

    local_branch = git.current_branch(cwd=base)
    parsed = config.parse_agent_branch(local_branch)

    if parsed and not branch:
        # On an agent branch: always reset to remote (agent is authoritative)
        target = config.resolve_target(local_branch)
        _pull_agent(base, local_branch, target)
    else:
        # On a shared branch (or explicit branch arg): ff-merge
        target_branch = branch or local_branch
        if force and not config.parse_agent_branch(target_branch):
            raise SystemExit(
                f"--force is only allowed on agent branches, not '{target_branch}'."
            )
        _pull_shared(base, local_branch, target_branch, force)


def _pull_agent(base: Path, local_branch: str, target: str):
    """Reset agent branch to match remote. Agent branches are ephemeral on the
    host, the cave agent is always authoritative."""
    remote_agent = f"{config.CAVE_REMOTE}/{local_branch}"
    remote_target = f"{config.CAVE_REMOTE}/{target}"

    # Update local target branch if it exists
    try:
        git.git_output("rev-parse", remote_target, cwd=base)
        try:
            git.git_output("rev-parse", target, cwd=base)
            git.git("branch", "-f", "--no-track", target, remote_target,
                    cwd=base, echo=False, check=False, capture=True)
        except Exception:
            pass
    except Exception:
        pass

    # Reset agent branch to remote
    try:
        git.git_output("rev-parse", remote_agent, cwd=base)
    except Exception:
        print(f"No remote branch '{remote_agent}'.")
        return

    local_sha = git.git_output("rev-parse", local_branch, cwd=base)
    remote_sha = git.git_output("rev-parse", remote_agent, cwd=base)

    if local_sha == remote_sha:
        print("Already up to date.")
        return

    git.git("reset", "--hard", remote_agent, cwd=base, **config.v())
    print(f"Reset '{local_branch}' to '{remote_agent}'.")

    # Show what's on the agent branch relative to target
    try:
        log = git.git_output(
            "log", "--oneline", f"{remote_target}..{remote_agent}", cwd=base)
    except Exception:
        log = None
    if log:
        count = len(log.splitlines())
        n = "commit" if count == 1 else "commits"
        print(f"{count} {n} ahead of {target}:")
        print(log)


def _pull_shared(base: Path, local_branch: str, target_branch: str, force: bool):
    """Fast-forward merge a shared branch."""
    remote_ref = f"{config.CAVE_REMOTE}/{target_branch}"

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

    if not ahead:
        print("Already up to date.")
        return

    count = len(ahead.splitlines())

    if force:
        git.git("reset", "--hard", remote_ref, cwd=base, **config.v())
    else:
        try:
            git.git("merge", "--ff-only", remote_ref, cwd=base, **config.v())
        except subprocess.CalledProcessError:
            print(f"\nOn branch {local_branch}")
            print(f"Diverged from {remote_ref}.")
            if config.parse_agent_branch(local_branch):
                print(f"  ** cav pull --force                # accept remote version **")
            else:
                print(f"  ** cav push --force                # push local version to cave **")
            raise SystemExit(1)

    n = "commit" if count == 1 else "commits"
    print(f"\n{count} new {n} on {target_branch}:")
    print(ahead)
