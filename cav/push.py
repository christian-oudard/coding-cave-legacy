"""cav push: push current branch to cave."""

import subprocess
from pathlib import Path

from . import config, git


def push(base: Path, force: bool = False):
    branch = git.current_branch(cwd=base)
    args = ["push", config.SHARED_REMOTE, branch]
    if force:
        args.insert(1, "--force")
    try:
        git.git(*args, cwd=base)
    except subprocess.CalledProcessError:
        if not force:
            print(f"\n  cav push --force                # overwrite cave with local branch")
        raise SystemExit(1)
