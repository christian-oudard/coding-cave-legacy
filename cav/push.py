"""cav push: push current branch to cave."""

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

    args = ["push", "--no-set-upstream", config.CAVE_REMOTE, branch]
    if force:
        args.insert(1, "--force")
    try:
        r = git.git(*args, cwd=base, **config.v())
    except subprocess.CalledProcessError as e:
        if e.stderr:
            print(e.stderr.rstrip(), file=sys.stderr)
        if not force:
            print(f"\n  cav push --force                # overwrite cave with local branch")
        raise SystemExit(1)

    if not config.verbose:
        stderr = getattr(r, "stderr", "") or ""
        if "up-to-date" in stderr:
            print(f"Already up to date.")
        else:
            print(f"Pushed {branch} to cave.")
