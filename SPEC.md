# Coding Cave

## Purpose

Coding Cave gives multiple Claude agents full autonomy inside a security boundary. Each agent gets unrestricted access within a bwrap sandbox. Nothing escapes until the user explicitly pulls. The user reviews and controls what crosses the boundary.

## Design Principles

**Full autonomy inside, full control outside.** Agents can install packages, run arbitrary commands, modify any file. But the host filesystem is untouched until the user pulls.

**Git is the sole coordination mechanism.** No external messaging system. Agents discover state changes through normal git operations. Branch protection hooks enforce safety.

**The host is the source of truth.** The host repo is canonical. The cave works on an overlay copy. Only commits cross the boundary. Uncommitted changes, working tree state, and staging area never cross.

**Nix for reproducibility, overlays for mutability.** The environment is defined in a Nix flake. The nix store is mounted as a writable overlay so agents can install additional packages. Project files are also on overlays: host files are the read-only lower layer, cave modifications go to the upper layer.

## Git Topology

A bare Git repo (`.coding-cave/cave-shared.git`) acts as the shared remote, named `cave` on both the user's repo and agent clones. The user also has a separate `origin` remote for publishing to GitHub.

## Agents

Agents are identified by globally unique numeric IDs: 1, 2, 3. IDs are assigned from a single incrementing counter across all projects, so there are never two different agents with the same ID. Display names combine the project slug with the ID: `coding-cave@1`, `my-app@2`. Each agent gets a lightweight shared clone that shares the object store with the bare repo via alternates.

Each agent session has a primary project (the project directory where the session was started). When an agent operates on multiple projects, it creates agent branches in each project's bare repo using the same globally unique ID.

## Branching

**Shared branches** (`main`, `develop`, etc.) live on the `cave` remote and are what the user pulls from. Agents deliver work by fast-forward pushing to shared branches. The user may force-push shared branches (for rollback).

**Agent branches** are named `agent-<n>/<target>` (e.g. `agent-1/main`, `agent-2/develop`). These are the agent's private working branches inside the cave. The user does not interact with agent branches directly. When an agent's work is ready, the agent merges into the target shared branch and pushes. The ff-only constraint forces agents to coordinate: if another agent pushed first, the agent must fetch and rebase before it can deliver.

Force-push is allowed on agent branches (agents amend work in progress). Only the matching agent (or the user) can push to an agent branch.

Agents are responsible for delivering a clean git history. WIP commits on agent branches must be squashed into focused, well-described commits before delivery to shared branches. Only the user may rewrite shared branch history.

## Branch Protection

A pre-receive hook on the shared repo enforces:

1. Agents cannot force-push shared branches.
2. Agents cannot push to other agents' branches.
3. Agents cannot delete shared branches.
4. The user can push anything.

Agent branches persist until the agent is explicitly removed via `cav remove-agent`. When an agent exits with all work delivered, it is auto-removed (registry entry, branches, workspace). When an agent exits with undelivered work, it remains paused and can be continued with `cav -c`. Undelivered work is detected using `git cherry` (patch-id comparison), so rebased or rewritten commits count as undelivered.

Agent identity is communicated via git push options. No push option means the pusher is the user.

Agents cannot be launched from agent branches. The user must be on a shared branch (e.g. main) when starting a new agent.

## Sandbox

Each agent runs in a bwrap namespace (via jail.nix) with:

- Nix store overlay (host store read-only + writable upper via fuse-overlayfs).
- Project workspace mounted at `/projects/<slug>`.
- Persistent home at `/home/cave-coder` (survives across sessions).
- GPG agent socket forwarded for commit signing (private key stays on host).
- Host git identity forwarded via environment variables.
- Shared host network (no isolation).
- Standard POSIX environment (SHELL, TERM, locale).

## Credential Isolation

Caves hold zero real credentials. All HTTPS traffic is routed through a MITM proxy (crinj) running on the host, which injects API keys based on destination host. A placeholder OAuth token satisfies Claude Code's local auth check, but is replaced by crinj at the HTTP level before reaching the API. The proxy CA certificate is bind-mounted into the cave. If crinj isn't running, HTTPS requests to credential-protected services fail. No fallback to direct credential forwarding.

## User/System Configuration

### System layer (flake.nix)

Base packages are infrastructure only: claude-code, nix, git, coreutils, and standard unix tools. No editors, language runtimes, or dev tools. `system-claude.md` is baked into the derivation and concatenated with the user's CLAUDE.md at session startup. Claude Code settings are declared via nix-claude.

### User layer (cave.nix)

`~/.config/coding-cave/cave.nix` declares extra packages, environment variables, files, and Claude Code configuration. Packages and env vars that reference binaries (like EDITOR) are declared together, ensuring consistency.

### CLAUDE.md concatenation

Every session gets `~/.claude/CLAUDE.md` = system layer + cave.nix layer + user CLAUDE.md. The system portion describes the cave environment. The user portion comes from `~/.config/coding-cave/CLAUDE.md`.

## Commands

## Data

`coding-cave.db` in the data directory (`~/.local/share/coding-cave/`) is a SQLite database (WAL mode) containing:

- **agents**: agent_id (PK), project slug, session_id, pid, started_at, exited_at.
- **projects**: slug (PK) to absolute path mapping, registered at `cav init` time.

Slug collisions (two different paths producing the same slug) are rejected at init time. Rename the project directory to resolve.

### `cav init`

Initializes Coding Cave in a project directory. Creates `.coding-cave/cave-shared.git` with the pre-receive hook. Registers the project in the global database.

### `cav [-c] [-p path ...] [--recursive]`

Launches an agent. Auto-assigns the next available numeric ID. Creates the workspace and sandbox, starts Claude with full permissions inside the cave.

With `-c` / `--continue`, resumes the most recently paused agent whose primary project matches the current directory. A paused agent is one that exited but still has a session in the registry. If multiple paused agents exist, the one with the most recent exit timestamp is chosen. Fails if no paused agents exist.

With `-p`, additional projects are mounted into the same cave. Each project must already have `cav init` done. The agent uses its globally unique ID across all projects, with separate workspaces and agent branches per project. All workspaces are mounted at `/projects/<slug>`. Fails with a helpful message if any project is not initialized.

With `--recursive`, the cave can launch subcaves. This exposes `/dev/fuse` inside the sandbox so the child cave can create its own nix store overlay. Disabled by default.

### `cav pull`

Fetches from the cave remote and updates the current branch.

On **shared branches**, fast-forward merges. Equivalent to `git fetch cave && git merge --ff-only cave/<branch>`. Fails if the branches have diverged. Force-pull is not allowed on shared branches, because the host repo is the source of truth. Use `cav push --force` to correct the cave instead.

On **agent branches**, always hard-resets to the remote agent branch. Agent branches are ephemeral on the host. The cave agent is authoritative, so pull unconditionally accepts the remote version. Shows the agent's commits relative to the target branch.

### `cav push`

Pushes the current branch to the cave remote. With `--force`, force-pushes (for rollback).

### Idempotency

`cav pull` and `cav push` are idempotent. Running them multiple times is equivalent to running once. A pull with nothing new says "Already up to date." A push with nothing new is a no-op.

### `cav status`

Shows whether shared branches on the cave remote are ahead of local branches. No fetch, reads local state only.

### `cav remove-agent <n>`

Removes a paused agent by ID. Deletes its session from the database, its agent branches from the current project's bare repo, and its workspace. Warns about any undelivered commits being discarded. Refuses to remove a running agent.

### `cav remove-agent --all [--yes]`

Removes all paused agents and resets the database. Refuses if any agents are currently running. Prompts for confirmation unless `--yes` is given.

### `cav shell`

Joins a running agent's namespace via nsenter for inspection and debugging.

## Output Style

CLI output follows Git's own conventions:

- Echo the git commands being run, prefixed with `+` (like `set -x`).
- Use Git's ref-line format: `<flag> <summary> <from> -> <to>`.
  Flags: `*` new, `+` forced, `-` deleted, space for fast-forward.
  Summary: `old..new` for fast-forward, `old...new` for forced.
- Show commit subject lines for new work, not just counts.
- Column-align for readability.
- Let git's own stderr (fetch progress, etc.) pass through.
