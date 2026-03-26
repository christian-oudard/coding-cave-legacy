"""cav pull: fetch from cave and fast-forward merge."""

import subprocess
from pathlib import Path

from . import config, git


def pull(base: Path, force: bool = False):
    git.git("fetch", "--prune", config.SHARED_REMOTE, cwd=base)

    local_branch = git.current_branch(cwd=base)
    remote_ref = f"{config.SHARED_REMOTE}/{local_branch}"

    try:
        git.git_output("rev-parse", remote_ref, cwd=base)
    except Exception:
        print(f"No remote branch '{remote_ref}'.")
        return

    local_sha = git.git_output("rev-parse", local_branch, cwd=base)
    remote_sha = git.git_output("rev-parse", remote_ref, cwd=base)

    if local_sha == remote_sha:
        print(f"Already up to date.")
        return

    # Show incoming commits
    ahead = git.git_output(
        "log", "--oneline", f"{local_branch}..{remote_ref}", cwd=base)
    count = len(ahead.splitlines())
    n = "commit" if count == 1 else "commits"
    print(f"\n{count} new {n}:")
    print(ahead)

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

    log = git.git_output("log", "--oneline", f"{local_sha}..{remote_sha}", cwd=base)
    print(log)
