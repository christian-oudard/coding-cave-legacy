"""cav push: push current branch to shared remote."""

import subprocess
import sys
from pathlib import Path

from . import config, git, registry


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

    parsed = config.parse_dev_branch(branch)
    try:
        if parsed:
            r = _push_dev(base, branch, parsed[1], force)
        else:
            r = push_base(base, branch, force=force)
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


def push_base(base: Path, base_branch: str, refspec: str | None = None,
              force: bool = False):
    """Push to a base branch on the shared remote and rebase dependent agents."""
    if refspec is None:
        refspec = base_branch
    args = ["push", "--no-set-upstream", config.CAVE_SHARED_REMOTE, refspec]
    if force:
        args.insert(1, "--force")
    r = git.git(*args, cwd=base, **config.v())
    _rebase_agents(base, base_branch)
    return r


def _push_dev(base: Path, branch: str, agent_id: str, force: bool):
    """Push a dev branch and sync the agent worktree."""
    args = ["push", "--no-set-upstream", config.CAVE_SHARED_REMOTE, branch]
    if force:
        args.insert(1, "--force")
    r = git.git(*args, cwd=base, **config.v())

    slug = config.project_slug(base)
    ws = config.worktree_dir(slug, agent_id)
    if not ws.exists():
        return r

    shared = config.shared_repo_path(base)
    git.git("fetch", str(shared),
            f"+refs/heads/{branch}:refs/remotes/{config.HOST_SHARED_REMOTE}/{branch}",
            cwd=ws, echo=False, check=False, capture=True)

    try:
        current = git.current_branch(cwd=ws)
    except Exception:
        return r
    if current == branch:
        git.git("reset", "--hard",
                f"{config.HOST_SHARED_REMOTE}/{branch}",
                cwd=ws, echo=False, check=False, capture=True)
    return r


def _rebase_agents(base: Path, base_branch: str):
    """Rebase agent dev branches with pending work onto the updated base."""
    slug = config.project_slug(base)
    shared = config.shared_repo_path(base)
    if not shared.exists():
        return

    for agent_id in registry.all_agent_ids(project=slug):
        ws = config.worktree_dir(slug, agent_id)
        if not ws.exists():
            continue

        try:
            branch = git.current_branch(cwd=ws)
        except Exception:
            continue
        if config.resolve_base(branch) != base_branch:
            continue

        try:
            ahead = git.commits_ahead(
                f"refs/heads/{branch}", f"refs/heads/{base_branch}", cwd=shared)
        except Exception:
            continue
        if ahead == 0:
            continue

        git.git("fetch", str(shared),
                f"+refs/heads/{base_branch}:refs/remotes/{config.HOST_SHARED_REMOTE}/{base_branch}",
                cwd=ws, echo=False, check=False, capture=True)

        r = git.git("rebase", f"{config.HOST_SHARED_REMOTE}/{base_branch}",
                    cwd=ws, check=False, capture=True, echo=False)
        if r.returncode != 0:
            git.git("rebase", "--abort", cwd=ws,
                    check=False, capture=True, echo=False)
            print(f"Warning: rebase of {branch} onto {base_branch} failed (conflicts).")
            continue

        git.git("fetch", str(ws),
                f"+refs/heads/{branch}:refs/heads/{branch}",
                cwd=shared, echo=False, check=False, capture=True)

        print(f"Rebased {branch} onto {base_branch}.")
