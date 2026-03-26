"""cav status: show what's happening in the cave."""

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


def status(base: Path):
    git.git("fetch", "--prune", config.SHARED_REMOTE, cwd=base)
    print()

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
            print(f"{local_branch} up to date with {remote_ref}.")
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
        target, agent_id = parsed
        agent_branches.append((branch, target, agent_id))

    if not agent_branches:
        print("No agent activity.")
        return

    # Group branches by agent ID
    by_agent = {}
    for branch, target, agent_id in sorted(agent_branches):
        by_agent.setdefault(agent_id, []).append((branch, target))

    # Map agent_id -> remote ref (for excluding sibling agent commits)
    agent_ref = {
        agent_id: f"{config.SHARED_REMOTE}/{branch}"
        for branch, target, agent_id in agent_branches
    }

    for agent_id in sorted(by_agent, key=int):
        branches = by_agent[agent_id]
        name = config.display_name(base, agent_id)
        running = config.is_agent_running(base, agent_id)
        other_agents = [ref for aid, ref in agent_ref.items() if aid != agent_id]

        # Collect undelivered work across all branches for this agent,
        # excluding commits already reachable from other agents
        undelivered = []
        for branch, target in branches:
            remote_agent = f"{config.SHARED_REMOTE}/{branch}"
            target_ref = f"{config.SHARED_REMOTE}/{target}"
            try:
                ahead = git.commits_ahead(remote_agent, target_ref, exclude=other_agents, cwd=base)
            except Exception:
                ahead = 0
            if ahead > 0:
                lines = git.commit_oneline(f"{target_ref}..{remote_agent}", exclude=other_agents, cwd=base)
                undelivered.append((branch, ahead, lines))

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

        if not undelivered:
            print(f"{name}  ({state})")
        else:
            for branch, ahead, lines in undelivered:
                n = "commit" if ahead == 1 else "commits"
                print(f"{name}  ({state}) {ahead} undelivered {n} on {branch}")
                for line in lines:
                    print(f"  {line}")
