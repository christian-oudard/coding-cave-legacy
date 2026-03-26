"""cav status: show what's happening in the cave."""

import shutil
import time
from pathlib import Path

from . import config, git


def _format_age(timestamp: float) -> str:
    """Format a timestamp as a human-readable age like '3h ago'."""
    delta = int(time.time() - timestamp)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _remove_agent(base: Path, agent_id: str, branches: list[str]):
    """Remove an agent's remote branches and local state."""
    for branch in branches:
        git.git("push", config.SHARED_REMOTE, f":refs/heads/{branch}",
                cwd=base, echo=False, capture=True)
    slug = config.project_slug(base)
    for path in [
        config.agent_lock_dir(slug) / f"{agent_id}.lock",
        config.agent_session_file(slug, agent_id),
    ]:
        if path.exists():
            path.unlink()
    ws = config.worktree_dir(slug, agent_id)
    if ws.exists():
        shutil.rmtree(ws)


def status(base: Path, verbose: bool = False, agent_filter: str | None = None):
    git.git("fetch", "--prune", config.SHARED_REMOTE, cwd=base)
    print()

    if not agent_filter:
        local_branch = git.current_branch(cwd=base)
        remote_ref = f"{config.SHARED_REMOTE}/{local_branch}"

        # Shared branch status
        try:
            local_sha = git.git_output("rev-parse", local_branch, cwd=base)
            remote_sha = git.git_output("rev-parse", remote_ref, cwd=base)
        except Exception:
            remote_sha = local_sha = None

        if local_sha and remote_sha:
            if local_sha == remote_sha:
                print(f"{local_branch} is up to date with {remote_ref}.")
            else:
                ahead = git.commits_ahead(remote_ref, local_branch, cwd=base)
                if ahead > 0:
                    n = "commit" if ahead == 1 else "commits"
                    print(f"{local_branch} is behind {remote_ref} by {ahead} {n}.")
                    for line in git.commit_oneline(f"{local_branch}..{remote_ref}", cwd=base):
                        print(f"  {line}")
            print()

    # Agent branch activity
    branches = git.remote_branches(config.SHARED_REMOTE, cwd=base)
    agent_branches = []
    for branch in branches:
        parsed = config.parse_agent_branch(branch)
        if not parsed:
            continue
        target = config.resolve_target(branch)
        _, agent_id = parsed
        if agent_filter and agent_id != agent_filter:
            continue
        agent_branches.append((branch, target, agent_id))

    if not agent_branches:
        if agent_filter:
            print(f"No branches for agent {agent_filter}.")
        else:
            print("No agent activity.")
        return

    if agent_filter:
        verbose = True

    # Group branches by agent ID
    by_agent = {}
    for branch, target, agent_id in sorted(agent_branches):
        by_agent.setdefault(agent_id, []).append((branch, target))

    hidden = []
    pruned = []
    for agent_id in sorted(by_agent, key=int):
        branches = by_agent[agent_id]
        name = config.display_name(base, agent_id)
        running = config.is_agent_running(base, agent_id)

        undelivered = []
        orphaned = []
        for branch, target in branches:
            remote_agent = f"{config.SHARED_REMOTE}/{branch}"
            target_ref = f"{config.SHARED_REMOTE}/{target}"
            try:
                if git.has_merge_base(remote_agent, target_ref, cwd=base):
                    lines = git.cherry_undelivered(target_ref, remote_agent, cwd=base)
                    if lines:
                        undelivered.append((branch, len(lines), lines))
                else:
                    orphaned.append(branch)
            except Exception:
                pass

        # Auto-prune orphaned non-running agents
        if orphaned and not running and not undelivered:
            _remove_agent(base, agent_id, orphaned)
            pruned.append(name)
            continue

        total_ahead = sum(a for _, a, _ in undelivered)

        if running:
            state = "running"
        elif total_ahead > 0:
            exit_t = config.agent_exit_time(base, agent_id)
            if exit_t:
                state = f"paused {_format_age(exit_t)}"
            else:
                state = "paused"
        else:
            state = "idle"

        if not undelivered and not running:
            hidden.append(name)
            continue

        if not undelivered:
            print(f"{name}  ({state})")
        else:
            for branch, ahead, lines in undelivered:
                n = "commit" if ahead == 1 else "commits"
                print(f"{name}  ({state}) {ahead} undelivered {n} on {branch}")
                for line in lines:
                    print(f"  {line}")

    if hidden:
        if verbose:
            for name in hidden:
                print(f"{name}  (idle)")
        else:
            n = len(hidden)
            s = "session" if n == 1 else "sessions"
            print(f"{n} idle {s} not shown. Use -v to list.")

    if pruned:
        n = len(pruned)
        s = "session" if n == 1 else "sessions"
        print(f"Pruned {n} orphaned {s}: {', '.join(pruned)}")
