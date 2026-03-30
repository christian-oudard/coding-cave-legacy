"""cav push: push current branch to cave."""

import subprocess
from pathlib import Path

from . import config, git


def push(base: Path, force: bool = False, verbose: bool = False):
    branch = git.current_branch(cwd=base)
    args = ["push", "--no-set-upstream", config.SHARED_REMOTE, branch]
    if force:
        args.insert(1, "--force")
    try:
        git.git(*args, cwd=base, echo=verbose, capture=not verbose)
    except subprocess.CalledProcessError:
        if not force:
            print(f"\n  cav push --force                # overwrite cave with local branch")
        raise SystemExit(1)
