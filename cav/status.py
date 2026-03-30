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
    """Remove an agent's remote branches, local branches, and local state."""
    for branch in branches:
        git.git("push", config.SHARED_REMOTE, f":refs/heads/{branch}",
                cwd=base, echo=False, capture=True)
        # Also delete the local checkout if it exists
        git.git("branch", "-D", branch,
                cwd=base, echo=False, capture=True, check=False)
    config.remove_agent(agent_id)
    # Clean up workspaces for all projects (agent ID is globally unique)
    worktrees_dir = config.DATA_DIR / "worktrees"
    if worktrees_dir.exists():
        for slug_dir in worktrees_dir.iterdir():
            ws = slug_dir / agent_id
            if ws.exists():
                shutil.rmtree(ws)


def status(base: Path, agent_filter: str | None = None):
    git.git("fetch", "--prune", config.SHARED_REMOTE, cwd=base, **config.v())

    if not agent_filter:
        local_branch = git.current_branch(cwd=base)
        remote_ref = f"{config.SHARED_REMOTE}/{local_branch}"

        # Shared branch status
        try:
            local_sha = git.git_output("rev-parse", local_branch, cwd=base)
            remote_sha = git.git_output("rev-parse", remote_ref, cwd=base)
        except Exception:
            remote_sha = local_sha = None

        if local_sha and remote_sha and local_sha != remote_sha:
            ahead = git.commits_ahead(remote_ref, local_branch, cwd=base)
            if ahead > 0:
                n = "commit" if ahead == 1 else "commits"
                print(f"{local_branch} is behind {remote_ref} by {ahead} {n}.")
                for line in git.commit_oneline(f"{local_branch}..{remote_ref}", cwd=base):
                    print(f"  {line}")
                print()

    # Agents come from the registry, branches provide detail
    slug = config.project_slug(base)
    registered = config.all_agent_ids(slug)
    if agent_filter:
        registered = [a for a in registered if a == agent_filter]

    branches = git.remote_branches(config.SHARED_REMOTE, cwd=base)
    by_agent = {aid: [] for aid in registered}
    for branch in branches:
        parsed = config.parse_agent_branch(branch)
        if not parsed:
            continue
        _, agent_id = parsed
        if agent_id not in by_agent:
            continue
        target = config.resolve_target(branch)
        by_agent[agent_id].append((branch, target))

    if not by_agent:
        if agent_filter:
            print(f"No agent {agent_filter} in registry.")
        else:
            print("No running agents.")
        return

    verbose = config.verbose or bool(agent_filter)

    MAX_COMMITS = None if verbose else 3

    # Collect agent info and sort by last activity (oldest first)
    agents = []
    for agent_id, branches in by_agent.items():
        name = config.display_name(base, agent_id)
        running = config.is_agent_running(agent_id)

        unapproved = []
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
                        unapproved.append((branch, len(lines), lines))
            except Exception:
                pass

        # Fall back to registry timestamps if no branches
        if not last_activity:
            exit_t = config.agent_exit_time(agent_id)
            if exit_t:
                last_activity = exit_t

        total_ahead = sum(a for _, a, _ in unapproved)

        if running:
            state = "running"
        elif total_ahead > 0:
            state = "paused"
        else:
            state = "idle"

        agents.append((last_activity, agent_id, name, running, state, unapproved))

    # Sort by last activity, oldest first
    agents.sort(key=lambda a: a[0])

    hidden = []
    for last_activity, agent_id, name, running, state, unapproved in agents:
        if not unapproved and not running:
            hidden.append((name, last_activity))
            continue

        age = _format_age(last_activity) if last_activity else ""
        header = f"{name}  ({state})" + (f"  {age}" if age else "")

        if not unapproved:
            print(header)
        else:
            print(header)
            for branch, ahead, lines in unapproved:
                n = "commit" if ahead == 1 else "commits"
                print(f"  {ahead} unapproved {n} on {branch}")
                for line in (lines if MAX_COMMITS is None else lines[:MAX_COMMITS]):
                    print(f"    {line}")
                if MAX_COMMITS and ahead > MAX_COMMITS:
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
