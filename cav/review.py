"""cav review: fetch and checkout an agent branch for review."""

import subprocess
from pathlib import Path

from . import config, git
from .constants import CAVE_SHARED_REMOTE


def review(base: Path, agent_id: str):
    git.git("fetch", "--prune", CAVE_SHARED_REMOTE, cwd=base, **config.v())

    branches = git.remote_branches(CAVE_SHARED_REMOTE, cwd=base)
    agent_branch = None
    for b in branches:
        parsed = config.parse_dev_branch(b)
        if parsed and parsed[1] == agent_id:
            agent_branch = b
            break

    if not agent_branch:
        raise SystemExit(f"No agent branch found for agent {agent_id}.")

    remote_ref = f"{CAVE_SHARED_REMOTE}/{agent_branch}"
    target = config.resolve_base(agent_branch)
    remote_target = f"{CAVE_SHARED_REMOTE}/{target}"

    current = git.current_branch(cwd=base)
    try:
        if current == agent_branch:
            git.git("reset", "--hard", remote_ref, cwd=base, **config.v())
        else:
            git.git("checkout", "-B", agent_branch, remote_ref,
                    cwd=base, **config.v())
    except subprocess.CalledProcessError:
        raise SystemExit(1)

    print(f"Checked out {agent_branch}")

    # Show pending commits relative to target
    try:
        log = git.git_output(
            "log", "--oneline", f"{remote_target}..{remote_ref}", cwd=base)
    except Exception:
        return
    if log:
        count = len(log.splitlines())
        n = "commit" if count == 1 else "commits"
        print(f"{count} {n} ahead of {target}:")
        print(log)
