"""cav pull: pull dev branch from shared remote."""

from pathlib import Path

from . import config, git


def pull(base: Path, force: bool = False):
    local_branch = git.current_branch(cwd=base)
    parsed = config.parse_dev_branch(local_branch)

    if not parsed:
        raise SystemExit(
            f"Not on a dev branch (current: '{local_branch}').\n"
            f"cav pull is only valid on dev branches."
        )

    base_name, agent_id = parsed
    slug = config.project_slug(base)

    # Step 1: Sync agent worktree tip to shared remote.
    # Objects are shared via alternates; this is mostly a ref update.
    _sync_agent_to_shared(base, slug, local_branch, agent_id, force)

    # Step 2: Fetch from shared remote to host.
    git.git("fetch", "--prune", config.CAVE_SHARED_REMOTE, cwd=base, **config.v())

    remote_dev = f"{config.CAVE_SHARED_REMOTE}/{local_branch}"
    remote_base = f"{config.CAVE_SHARED_REMOTE}/{base_name}"

    try:
        git.git_output("rev-parse", remote_dev, cwd=base)
    except Exception:
        print(f"No remote branch '{remote_dev}'.")
        return

    # Courtesy: update local base branch if it exists
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

    # Step 3: Reset host dev branch to remote.
    local_sha = git.git_output("rev-parse", local_branch, cwd=base)
    remote_sha = git.git_output("rev-parse", remote_dev, cwd=base)

    if local_sha == remote_sha:
        print("Already up to date.")
        return

    git.git("reset", "--hard", remote_dev, cwd=base, **config.v())
    print(f"Reset '{local_branch}' to '{remote_dev}'.")

    _show_ahead(base, remote_base, remote_dev, base_name)


def _sync_agent_to_shared(base: Path, slug: str, dev_branch: str,
                           agent_id: str, force: bool):
    """Fetch agent worktree's dev branch into the shared remote (bare repo).

    Uses git fetch in the bare repo from the worktree path. No hooks fire
    for fetches, and alternates mean minimal object transfer.
    """
    ws = config.worktree_dir(slug, agent_id)
    if not ws.exists():
        return

    shared = config.shared_repo_path(base)
    if not shared.exists():
        return

    try:
        git.git_output("rev-parse", "--verify",
                       f"refs/heads/{dev_branch}", cwd=ws)
    except Exception:
        return

    prefix = "+" if force else ""
    refspec = f"{prefix}refs/heads/{dev_branch}:refs/heads/{dev_branch}"

    r = git.git("fetch", str(ws), refspec,
                cwd=shared, check=False, capture=True, echo=False)
    if r.returncode != 0 and not force:
        raise SystemExit(
            f"Agent has rewritten history on '{dev_branch}'.\n"
            f"Use 'cav pull --force' to accept the rewrite."
        )


def _show_ahead(base, remote_base, remote_dev, base_name):
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
