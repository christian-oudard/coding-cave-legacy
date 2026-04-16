"""cav pull: pull dev branch from shared remote."""

from pathlib import Path

from . import config, git
from .constants import CAVE_SHARED_REMOTE


def pull(base: Path, force: bool = False):
    local_branch = git.current_branch(cwd=base)

    if config.inside_cave():
        # Inside a cave, the checked-out branch IS the cave's dev branch.
        # Its upstream dev ref lives in the parent's shared repo at
        # agent-<self>/<base>.
        aid = config.self_agent_id()
        if aid is None:
            raise SystemExit(
                "Cannot determine this cave's agent id "
                "(CODING_CAVE_AGENT_NAME not set).")
        _pull_cave_self(base, local_branch, aid, force)
        return

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
    _sync_agent_to_shared(base, slug, local_branch, agent_id)

    remote_dev = f"{CAVE_SHARED_REMOTE}/{local_branch}"
    remote_base = f"{CAVE_SHARED_REMOTE}/{base_name}"

    # Step 2: Fetch from shared remote to host.
    git.git("fetch", "--prune", CAVE_SHARED_REMOTE, cwd=base, **config.v())

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

    # Refuse to overwrite user commits. Commits that exist in the shared
    # bare repo came from the agent (prior pushes); commits that only exist
    # locally were made by the user on the host.
    local_only = git.git_output(
        "log", "--oneline", f"{remote_dev}..{local_branch}", cwd=base)
    if local_only and not force:
        shared = config.shared_repo_path(base)
        user_lines = []
        for line in local_only.splitlines():
            sha = line.split()[0]
            r = git.git("cat-file", "-t", sha,
                        cwd=shared, check=False, capture=True, echo=False)
            if r.returncode != 0:
                user_lines.append(line)
        local_only = "\n".join(user_lines) if user_lines else ""
    if local_only and not force:
        count = len(local_only.splitlines())
        n = "commit" if count == 1 else "commits"
        raise SystemExit(
            f"Local {n} on '{local_branch}' would be overwritten:\n"
            f"{local_only}\n"
            f"Use 'cav pull --force' to discard them."
        )

    git.git("reset", "--hard", remote_dev, cwd=base, **config.v())
    print(f"Reset '{local_branch}' to '{remote_dev}'.")

    _show_ahead(base, remote_base, remote_dev, base_name)


def _pull_cave_self(base: Path, local_branch: str, agent_id: str, force: bool):
    """Inside a cave, pull the agent's upstream dev state into local <base>.

    The cave's local branch is <base>; its upstream dev ref lives in the
    parent's shared repo at agent-<id>/<base>. Fetch it, warn on any
    local-only commits unless --force, then reset.
    """
    from .constants import HOST_SHARED_REMOTE
    remote_branch = config.dev_branch(local_branch, agent_id)
    git.git("fetch", "--prune", HOST_SHARED_REMOTE, cwd=base, **config.v())

    remote_dev = f"{HOST_SHARED_REMOTE}/{remote_branch}"
    try:
        remote_sha = git.git_output("rev-parse", remote_dev, cwd=base)
    except Exception:
        print(f"No upstream dev ref '{remote_dev}'.")
        return

    local_sha = git.git_output("rev-parse", local_branch, cwd=base)
    if local_sha == remote_sha:
        print("Already up to date.")
        return

    local_only = git.git_output(
        "log", "--oneline", f"{remote_dev}..{local_branch}", cwd=base)
    if local_only and not force:
        count = len(local_only.splitlines())
        n = "commit" if count == 1 else "commits"
        raise SystemExit(
            f"Local {n} on '{local_branch}' would be overwritten:\n"
            f"{local_only}\n"
            f"Use 'cav pull --force' to discard them."
        )

    git.git("reset", "--hard", remote_dev, cwd=base, **config.v())
    print(f"Reset '{local_branch}' to '{remote_dev}'.")


def _sync_agent_to_shared(base: Path, slug: str, dev_branch: str,
                           agent_id: str):
    """Fetch agent worktree's work into the shared remote dev ref.

    Inside the cave, the agent's work lives on its base branch (e.g.
    `main`) by default; nested agent branches live under their full name.
    Mirror whichever ref exists locally into `agent-<id>/<base>` on the
    shared remote.

    Always force-fetches so agent history rewrites are accepted. User commits
    are protected separately in the main pull path.
    """
    ws = config.worktree_dir(slug, agent_id)
    if not ws.exists():
        return

    shared = config.shared_repo_path(base)
    if not shared.exists():
        return

    # Prefer the dev branch itself if the ws has it (e.g. nested dev
    # branches); otherwise fall back to the base branch.
    local_ref = None
    for candidate in (dev_branch, config.resolve_base(dev_branch)):
        r = git.git("rev-parse", "--verify", f"refs/heads/{candidate}",
                    cwd=ws, check=False, capture=True, echo=False)
        if r.returncode == 0:
            local_ref = candidate
            break
    if local_ref is None:
        return

    refspec = f"+refs/heads/{local_ref}:refs/heads/{dev_branch}"
    r = git.git("fetch", str(ws), refspec,
                cwd=shared, check=False, capture=True, echo=False)
    if r.returncode == 0:
        git.update_agent_tip(shared, dev_branch)



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
