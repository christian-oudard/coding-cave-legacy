# Cave Security Redesign: Remote Isolation

## Problem

The bare repo (`cave-shared.git`) is bind-mounted read-write into the cave so agents can push. This gives agents direct filesystem access, allowing them to:

1. Clear push options to impersonate the user (`git -c push.pushOption=""`)
2. Overwrite refs directly (bypass hook entirely)
3. Overwrite the pre-receive hook itself

The hook is a guardrail against accidents, not a security boundary.

## Goal

The agent should be able to fetch and push via the `cave` remote, but have zero filesystem access to the bare repo or its hooks. The pre-receive hook and agent identity must be enforced server-side, outside the sandbox.

## Approach: git daemon on localhost

The cave shares host network. A git daemon running on the host can serve the bare repo to agents inside the cave.

### How it works

1. `cav agent` starts a `git daemon` on a per-agent localhost port before launching the sandbox.
2. The agent workspace remote URL is `git://127.0.0.1:PORT/`.
3. The bare repo is NOT mounted into the cave.
4. The pre-receive hook runs on the host, outside the sandbox. The agent cannot read or modify it.
5. The daemon stops when the agent exits.

### Agent identity

Current design: agent identity comes from `push.pushOption` in the agent's git config. This is client-controlled, so the agent can clear it.

New design: the daemon injects the agent identity. Each agent gets a unique port. The hook determines identity from the port (or an env var set by the daemon wrapper), not from push options. The agent has no way to influence this.

Options for passing identity to the hook:
- **Port-based**: each agent gets port 9400+N. The hook reads `$REMOTE_ADDR` or a custom env var to determine the agent.
- **Wrapper script**: instead of raw `git daemon`, a wrapper sets `CAV_AGENT_ID=N` in the environment before invoking `git-receive-pack`. The hook reads this env var.
- **Per-agent daemon config**: each daemon instance has a config that identifies the agent. The hook reads it.

The wrapper approach is simplest: the daemon runs `git-receive-pack` through a wrapper that sets the identity env var.

### Push options

Push options are no longer needed for identity. They could still be used for other metadata, but identity is now server-controlled.

The `push.pushOption` config in agent workspaces becomes unnecessary. Remove it from workspace setup.

### Fetch

`git daemon` serves `git-upload-pack` by default (fetch is read-only). No special handling needed.

### Multi-agent port allocation

Each agent gets a deterministic port: base port + agent ID. For example, agent 1 gets 9401, agent 2 gets 9402. No conflicts.

### Lifecycle

```
cav agent 1
  -> start git daemon on port 9401, serving cave-shared.git
  -> launch bwrap sandbox (bare repo NOT mounted)
  -> agent works, pushes to git://127.0.0.1:9401/
  -> agent exits
  -> stop git daemon
```

### What changes

- `sandbox.py`: start/stop git daemon, remove bare repo mount
- `flake.nix`: remove `CODING_CAVE_SHARED_REPO` bind-mount
- `worktree.py`: set remote URL to `git://127.0.0.1:PORT/` instead of filesystem path
- `hooks/pre-receive`: read identity from env var instead of push options
- `init.py`: no changes (bare repo setup stays the same)
- `cav pull`/`cav push`: no changes (these run on the host, outside the cave)

### What stays the same

- The bare repo lives at `.coding-cave/cave-shared.git`
- The pre-receive hook enforces branch protection
- The user pushes directly (no daemon needed, file path works)
- `cav init`, `cav pull`, `cav push`, `cav status` are unchanged

## Resolved questions

- `git daemon --enable=receive-pack` runs pre-receive hooks. Confirmed.
- `git daemon --inetd` with socat works. Implemented.
- Bare repo mounted read-only (not removed). Needed for git alternates object access. Read-only blocks all write-based bypasses.

## Known limitation: cross-agent port hopping

All agents share the host network. Agent 1 can push to `git://127.0.0.1:9402/` (agent 2's daemon port) and the hook will see `CAV_AGENT_ID=2`. This breaks cross-agent branch isolation.

Impact: agents can write to each other's branches. They still cannot force-push, delete, or create base branches (hook enforces ff-only for all agents regardless of identity).

Fix: Unix sockets instead of TCP. Each agent's sandbox mounts only its own socket. Other agents' sockets are inaccessible.
