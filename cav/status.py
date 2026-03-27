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
    config.remove_agent(agent_id)
    # Clean up workspaces for all projects (agent ID is globally unique)
    worktrees_dir = config.DATA_DIR / "worktrees"
    if worktrees_dir.exists():
        for slug_dir in worktrees_dir.iterdir():
            ws = slug_dir / agent_id
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

    # Group branches by agent ID
    by_agent = {}
    for branch, target, agent_id in sorted(agent_branches):
        by_agent.setdefault(agent_id, []).append((branch, target))

    # Include running agents that haven't pushed any branches yet
    for aid in config.running_agent_ids(base):
        if agent_filter and aid != agent_filter:
            continue
        by_agent.setdefault(aid, [])

    if not by_agent:
        if agent_filter:
            print(f"No branches for agent {agent_filter}.")
        else:
            print("No agent activity.")
        return

    if agent_filter:
        verbose = True

    MAX_COMMITS = 3

    # Collect agent info and sort by last activity (oldest first)
    agents = []
    for agent_id, branches in by_agent.items():
        name = config.display_name(base, agent_id)
        running = config.is_agent_running(agent_id)

        undelivered = []
        last_activity = 0.0
        for branch, target in branches:
            remote_agent = f"{config.SHARED_REMOTE}/{branch}"
            target_ref = f"{config.SHARED_REMOTE}/{target}"
            ts = git.commit_timestamp(remote_agent, cwd=base)
            if ts and ts > last_activity:
                last_activity = ts
            try:
                if git.has_merge_base(remote_agent, target_ref, cwd=base):
                    lines = git.cherry_undelivered(target_ref, remote_agent, cwd=base)
                    if lines:
                        undelivered.append((branch, len(lines), lines))
            except Exception:
                pass

        # Fall back to registry timestamps if no branches
        if not last_activity:
            exit_t = config.agent_exit_time(agent_id)
            if exit_t:
                last_activity = exit_t

        total_ahead = sum(a for _, a, _ in undelivered)

        if running:
            state = "running"
        elif total_ahead > 0:
            state = "paused"
        else:
            state = "idle"

        agents.append((last_activity, agent_id, name, running, state, undelivered))

    # Sort by last activity, oldest first
    agents.sort(key=lambda a: a[0])

    hidden = []
    for last_activity, agent_id, name, running, state, undelivered in agents:
        if not undelivered and not running:
            hidden.append((name, last_activity))
            continue

        age = _format_age(last_activity) if last_activity else ""
        header = f"{name}  ({state})" + (f"  {age}" if age else "")

        if not undelivered:
            print(header)
        else:
            print(header)
            for branch, ahead, lines in undelivered:
                n = "commit" if ahead == 1 else "commits"
                print(f"  {ahead} undelivered {n} on {branch}")
                for line in lines[:MAX_COMMITS]:
                    print(f"    {line}")
                if ahead > MAX_COMMITS:
                    print(f"    ... and {ahead - MAX_COMMITS} more")

    if hidden:
        if verbose:
            for name, ts in hidden:
                age = _format_age(ts) if ts else ""
                suffix = f"  {age}" if age else ""
                print(f"{name}  (idle){suffix}")
        else:
            n = len(hidden)
            s = "session" if n == 1 else "sessions"
            print(f"{n} idle {s} not shown. Use -v to list.")
