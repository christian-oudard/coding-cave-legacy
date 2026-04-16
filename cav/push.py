"""cav push: push current branch to shared remote."""

import subprocess
import sys
from pathlib import Path

from . import config, git, registry
from .constants import CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE


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
        if config.inside_cave():
            # Inside a cave, the local branch is the cave's own base; we
            # publish it upstream as agent-<id>/<base>.
            aid = config.self_agent_id()
            if aid is None:
                raise SystemExit(
                    "Cannot determine this cave's agent id "
                    "(CODING_CAVE_AGENT_NAME not set).")
            r = _push_cave_self(base, branch, aid, force)
        elif parsed:
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


def _push_cave_self(base: Path, local_branch: str, agent_id: str, force: bool):
    """Publish the cave's local <base> upstream as agent-<id>/<base>.

    Used from inside a cave via `cav push`. The cave's sandbox git wrapper
    already auto-pushes on commit/rebase; this path supports explicit
    pushes and forces. The host-cave remote URL points at the parent's
    bare repo (via the per-agent daemon).
    """
    remote_branch = config.dev_branch(local_branch, agent_id)
    refspec = f"refs/heads/{local_branch}:refs/heads/{remote_branch}"
    args = ["push", HOST_SHARED_REMOTE, refspec]
    if force:
        args.insert(1, "--force")
    return git.git(*args, cwd=base, **config.v())


def push_base(base: Path, base_branch: str, refspec: str | None = None,
              force: bool = False):
    """Push to a base branch on the shared remote and rebase dependent agents."""
    if refspec is None:
        refspec = base_branch
    args = ["push", "--no-set-upstream", CAVE_SHARED_REMOTE, refspec]
    if force:
        args.insert(1, "--force")
    r = git.git(*args, cwd=base, **config.v())
    _rebase_agents(base, base_branch)
    return r


def _push_dev(base: Path, branch: str, agent_id: str, force: bool):
    """Push a dev branch and sync the agent worktree.

    The host-side branch is `agent-<id>/<base>`. Inside the agent's
    worktree the corresponding local branch is just `<base>` (see
    workspace.verify_layout).
    """
    args = ["push", "--no-set-upstream", CAVE_SHARED_REMOTE, branch]
    if force:
        args.insert(1, "--force")
    r = git.git(*args, cwd=base, **config.v())

    slug = config.project_slug(base)
    ws = config.worktree_dir(slug, agent_id)
    if not ws.exists():
        return r

    shared = config.shared_repo_path(base)
    # Fetch the host-side dev branch into the ws's remote-tracking ref.
    git.git("fetch", str(shared),
            f"+refs/heads/{branch}:refs/remotes/{HOST_SHARED_REMOTE}/{branch}",
            cwd=ws, echo=False, check=False, capture=True)

    ws_branch = config.resolve_base(branch)
    try:
        current = git.current_branch(cwd=ws)
    except Exception:
        return r
    if current == ws_branch:
        _sync_agent_worktree(ws, branch, force)

    # Agent worktree now has the pushed state, so update agent-tip
    # to let the agent amend freely from this point.
    git.update_agent_tip(shared, branch)
    return r


def _sync_agent_worktree(ws: Path, branch: str, force: bool):
    """Update the agent worktree to match the pushed dev branch.

    Preserves uncommitted work via stash. If the stash pop conflicts,
    the conflict markers are left for the agent to resolve.
    """
    remote_ref = f"{HOST_SHARED_REMOTE}/{branch}"
    q = dict(echo=False, check=False, capture=True)

    # Stash uncommitted changes
    has_changes = bool(git.git_output("status", "--porcelain", cwd=ws))
    if has_changes:
        git.git("stash", cwd=ws, **q)

    if force:
        git.git("reset", "--hard", remote_ref, cwd=ws, **q)
    else:
        # Non-force push was FF to shared remote, so this should be FF too.
        r = git.git("merge", "--ff-only", remote_ref, cwd=ws, **q)
        if r.returncode != 0:
            # Agent has unpushed commits (e.g. daemon was down). Try rebase.
            r = git.git("rebase", remote_ref, cwd=ws, **q)
            if r.returncode != 0:
                git.git("rebase", "--abort", cwd=ws, **q)
                git.git("reset", "--hard", remote_ref, cwd=ws, **q)
                print(f"Warning: could not rebase agent worktree, reset to pushed state.")

    if has_changes:
        r = git.git("stash", "pop", cwd=ws, **q)
        if r.returncode != 0:
            print(f"Warning: agent worktree has merge conflicts from pushed changes.")


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
                f"+refs/heads/{base_branch}:refs/remotes/{HOST_SHARED_REMOTE}/{base_branch}",
                cwd=ws, echo=False, check=False, capture=True)

        r = git.git("rebase", f"{HOST_SHARED_REMOTE}/{base_branch}",
                    cwd=ws, check=False, capture=True, echo=False)
        if r.returncode != 0:
            git.git("rebase", "--abort", cwd=ws,
                    check=False, capture=True, echo=False)
            print(f"Warning: rebase of {branch} onto {base_branch} failed (conflicts).")
            continue

        git.git("fetch", str(ws),
                f"+refs/heads/{branch}:refs/heads/{branch}",
                cwd=shared, echo=False, check=False, capture=True)
        git.update_agent_tip(shared, branch)

        print(f"Rebased {branch} onto {base_branch}.")
