"""cav status: show what's happening in the cave."""

import shutil
import time
from pathlib import Path

from . import config, git, registry


def format_projects(entries: list[tuple[float, str, str]]) -> str:
    """Combine per-project status strings, sorted by activity, with inactive summary."""
    active = [(ts, o) for ts, _, o in entries if ts]
    inactive = sorted(s for ts, s, _ in entries if not ts)
    if not active:
        return "No running agents in any project."
    active.sort(key=lambda a: a[0], reverse=True)
    parts = [o for _, o in active]
    if inactive:
        parts.append(f"No running agents in: {', '.join(inactive)}")
    return "\n".join(parts)


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


# Agent info tuple: (last_activity, agent_id, name, running, state, unapproved)
# unapproved: list of (branch, ahead_count, commit_lines)
AgentInfo = tuple[float, str, str, bool, str, list[tuple[str, int, list[str]]]]


def sort_agents(agents: list[AgentInfo]) -> list[AgentInfo]:
    """Sort agents: running first, then stopped. Within each group, most recent first."""
    order = {"running": 0, "stopped": 1}
    return sorted(agents, key=lambda a: (order.get(a[4], 9), -a[0]))


def format_agents(agents: list[AgentInfo], max_commits: int | None = 3) -> str:
    """Format sorted agent list into display text."""
    lines = []
    for i, (last_activity, _, name, _, state, unapproved) in enumerate(agents):
        if i > 0:
            lines.append("")
        age = _format_age(last_activity) if last_activity else ""
        header = f"{name}  ({state})" + (f"  {age}" if age else "")
        lines.append(header)
        for branch, ahead, commit_lines in unapproved:
            commit = "commit" if ahead == 1 else "commits"
            lines.append(f"  {ahead} {commit} on {branch}")
            for line in (commit_lines if max_commits is None else commit_lines[:max_commits]):
                lines.append(f"    {line}")
            if max_commits and ahead > max_commits:
                lines.append(f"    ... and {ahead - max_commits} more")
    return "\n".join(lines)


def _remove_agent(base: Path, agent_id: str, branches: list[str]):
    """Remove an agent's remote branches, local branches, and local state."""
    # Clean branches in the primary project.
    for branch in branches:
        git.git("push", config.CAVE_SHARED_REMOTE, f":refs/heads/{branch}",
                cwd=base, echo=False, capture=True)
        git.git("branch", "-D", branch,
                cwd=base, echo=False, capture=True, check=False)
    # Clean branches and worktrees in all associated projects.
    primary_slug = config.project_slug(base)
    for slug in registry.agent_all_slugs(agent_id):
        ws = config.worktree_dir(slug, agent_id)
        if ws.exists():
            shutil.rmtree(ws)
        if slug == primary_slug:
            continue
        path = registry.project_path(slug)
        if not path:
            continue
        try:
            sec_base = Path(path)
            for b in git.remote_branches(config.CAVE_SHARED_REMOTE, cwd=sec_base):
                parsed = config.parse_dev_branch(b)
                if parsed and parsed[1] == agent_id:
                    git.git("push", config.CAVE_SHARED_REMOTE, f":refs/heads/{b}",
                            cwd=sec_base, echo=False, capture=True, check=False)
                    git.git("branch", "-D", b,
                            cwd=sec_base, echo=False, capture=True, check=False)
        except Exception:
            pass
    registry.remove_agent(agent_id)


def format_divergence(local_branch: str, remote_ref: str,
                      cwd: Path) -> str | None:
    """Return a divergence message for local vs remote branch, or None."""
    try:
        local_sha = git.git_output("rev-parse", local_branch, cwd=cwd)
        remote_sha = git.git_output("rev-parse", remote_ref, cwd=cwd)
    except Exception:
        return None
    if local_sha == remote_sha:
        return None
    behind = git.commits_ahead(remote_ref, local_branch, cwd=cwd)
    local_ahead = git.commits_ahead(local_branch, remote_ref, cwd=cwd)
    if behind > 0 and local_ahead > 0:
        nb = "commit" if behind == 1 else "commits"
        na = "commit" if local_ahead == 1 else "commits"
        return (f"{local_branch} and {remote_ref} have diverged,\n"
                f"and have {local_ahead} and {behind} different "
                f"{na} and {nb} each, respectively.")
    if behind > 0:
        n = "commit" if behind == 1 else "commits"
        return (f"{local_branch} is behind {remote_ref} "
                f"by {behind} {n}, and can be fast-forwarded.")
    if local_ahead > 0:
        n = "commit" if local_ahead == 1 else "commits"
        return (f"{local_branch} is ahead of {remote_ref} "
                f"by {local_ahead} {n}.")
    return None


def render_status(base: Path, agent_filter: str | None = None) -> tuple[float, str]:
    """Render project status. Returns (most_recent_timestamp, rendered_text)."""
    slug = config.project_slug(base)
    parts = []

    if not agent_filter:
        local_branch = git.current_branch(cwd=base)
        remote_ref = f"{config.CAVE_SHARED_REMOTE}/{local_branch}"
        div = format_divergence(local_branch, remote_ref, cwd=base)
        if div:
            parts.append(div)

    registered = registry.all_agent_ids(slug)
    if agent_filter:
        registered = [a for a in registered if a == agent_filter]

    branches = git.remote_branches(config.CAVE_SHARED_REMOTE, cwd=base)
    by_agent = {aid: [] for aid in registered}
    for branch in branches:
        parsed = config.parse_dev_branch(branch)
        if not parsed:
            continue
        _, agent_id = parsed
        if agent_id not in by_agent:
            continue
        base_name = config.resolve_base(branch)
        by_agent[agent_id].append((branch, base_name))

    if not by_agent:
        if agent_filter:
            return 0.0, f"No agent {agent_filter} in registry."
        return 0.0, f"No running agents in {slug}."

    verbose = config.verbose or bool(agent_filter)
    max_commits = None if verbose else 3

    agents: list[AgentInfo] = []
    for agent_id, agent_branches in by_agent.items():
        name = registry.display_name(base, agent_id)
        running = registry.is_agent_running(agent_id)

        unapproved = []
        last_activity = 0.0
        for branch, base_name in agent_branches:
            remote_dev = f"{config.CAVE_SHARED_REMOTE}/{branch}"
            remote_base = f"{config.CAVE_SHARED_REMOTE}/{base_name}"
            ts = git.commit_timestamp(remote_dev, cwd=base)
            if ts and ts > last_activity:
                last_activity = ts
            try:
                if git.has_merge_base(remote_dev, remote_base, cwd=base):
                    lines = git.cherry_undelivered(remote_base, remote_dev, cwd=base)
                    if lines:
                        unapproved.append((branch, len(lines), lines))
            except Exception:
                pass

        if not last_activity:
            exit_t = registry.agent_exit_time(agent_id)
            if exit_t:
                last_activity = exit_t

        state = "running" if running else "stopped"

        # Only show agents that are running or have unapproved work.
        if not running and sum(a for _, a, _ in unapproved) == 0:
            continue

        agents.append((last_activity, agent_id, name, running, state, unapproved))

    agents = sort_agents(agents)

    if not agents:
        if agent_filter:
            return 0.0, f"No agent {agent_filter} in registry."
        return 0.0, f"No running agents in {slug}."

    parts.append(f"=== {slug} ===")
    parts.append(format_agents(agents, max_commits))
    return agents[0][0], "\n\n".join(parts)
