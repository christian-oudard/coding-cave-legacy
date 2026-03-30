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

Agents are identified by globally unique numeric IDs: 1, 2, 3. IDs are assigned from a single incrementing counter across all projects, so there are never two different agents with the same ID. Display names combine the project slug with the ID: `coding-cave@1`, `my-app@2`. Each agent gets its own workspace clone of the shared repo.

Each agent session has a primary project (the project directory where the session was started). When an agent operates on multiple projects, it creates agent branches in each project's bare repo using the same globally unique ID.

## Branching

**Shared branches** (`main`, `develop`, etc.) live on the `cave` remote and are what the user pulls from. Agents cannot push to shared branches. Only the user modifies shared branches, via `cav approve` and `cav reject`.

**Agent branches** are named `agent-<n>/<target>` (e.g. `agent-1/main`, `agent-2/develop`). These are the agent's working branches inside the cave. When an agent's work is ready, it stays on the agent branch until the user approves it.

Force-push is allowed on agent branches (agents amend work in progress). Only the matching agent (or the user) can push to an agent branch.

Agents are responsible for a clean git history. WIP commits on agent branches must be squashed into focused, well-described commits before the user approves them. Only the user may rewrite shared branch history.

## Branch Protection

A pre-receive hook on the shared repo enforces:

1. Agents cannot push to shared branches.
2. Agents cannot push to other agents' branches.
3. Agents cannot delete shared branches.
4. The user can push anything.

Agent branches persist until the agent's work is approved or the agent is explicitly removed via `cav remove-agent`. When an agent exits with all work approved, it is auto-removed (registry entry, branches, workspace). When an agent exits with unapproved work, it remains paused and can be continued with `cav -c`. Unapproved work is detected by patch-id comparison, so rebased or rewritten commits count as unapproved.

Every commit on an agent branch is automatically force-pushed to the cave remote. This keeps the remote branch in sync without manual pushes. Only the agent's own branch is pushed, never shared branches or other agents' branches.

Agent identity is communicated via git push options. No push option means the pusher is the user.

Agents cannot be launched from agent branches. The user must be on a shared branch (e.g. main) when starting a new agent.

## Sandbox

Each agent runs in a sandbox with:

- Nix store overlay (host store read-only, writable upper layer).
- Project workspace mounted at `/projects/<slug>`.
- Persistent home at `/home/cave-coder` (survives across sessions).
- GPG agent socket forwarded for commit signing (private key stays on host).
- Host git identity forwarded via environment variables.
- Shared host network (no isolation).
- Standard POSIX environment (SHELL, TERM, locale).

## Credential Isolation

Caves hold zero real credentials. All HTTPS traffic is routed through a MITM proxy (crinj) running on the host, which injects API keys based on destination host. A placeholder OAuth token satisfies Claude Code's local auth check, but is replaced by crinj at the HTTP level before reaching the API. The proxy CA certificate is available inside the cave. If crinj isn't running, HTTPS requests to credential-protected services fail. No fallback to direct credential forwarding.

## User/System Configuration

### System layer

Base packages are infrastructure only: claude-code, nix, git, coreutils, and standard unix tools. No editors, language runtimes, or dev tools. A system CLAUDE.md is concatenated with the user's CLAUDE.md at session startup.

### User layer (cave.nix)

`~/.config/coding-cave/cave.nix` declares extra packages, environment variables, files, and Claude Code configuration. Packages and env vars that reference binaries (like EDITOR) are declared together, ensuring consistency.

### CLAUDE.md concatenation

Every session gets `~/.claude/CLAUDE.md` = system layer + cave.nix layer + user CLAUDE.md. The system portion describes the cave environment. The user portion comes from `~/.config/coding-cave/CLAUDE.md`.

## Data

A global database in the data directory (`~/.local/share/coding-cave/`) tracks registered agents, projects, and approvals. Each agent has an ID, project, session ID, and lifecycle state. Each project maps a slug to an absolute path. Each approval maps one commit to its agent, recording the shared branch state before and after that commit. An approval batch (multiple commits from one agent) creates one record per commit.

Slug collisions (two different paths producing the same slug) are rejected at init time. Rename the project directory to resolve.

## Commands

### `cav init`

Initializes Coding Cave in a project directory. Sets up the shared bare repo, branch protection hooks, and registers the project in the global database.

### `cav [-c] [-p path ...] [--recursive]`

Launches an agent. Auto-assigns the next available numeric ID. Creates the workspace and sandbox, starts Claude with full permissions inside the cave.

With `-c` / `--continue`, continues the most recent Claude session for this project via `claude --resume <session-id>`. If a paused agent exists in the registry, it is reused. Otherwise, a fresh agent ID is allocated. In both cases, the session ID is resolved before launching Claude to avoid TOCTOU races with other agents. When no session ID is in the registry (agent was auto-cleaned), the most recent session is found by scanning `~/.claude/projects/<encoded-cwd>/*.jsonl` by file mtime, matching Claude's own recency metric. Since all agents for a project share the same sandbox cwd (`/projects/<slug>`), sessions from any agent are visible.

With `-p`, additional projects are mounted into the same cave. Each project must already have `cav init` done. The agent uses its globally unique ID across all projects, with separate workspaces and agent branches per project. All workspaces are mounted at `/projects/<slug>`. Fails with a helpful message if any project is not initialized.

With `--recursive`, the cave can launch subcaves. Disabled by default.

### `cav approve`

Approves the current agent branch's work into its target shared branch. Must be run from an agent branch. The agent ID and target branch are inferred from the branch name (e.g. `agent-3/main` targets `main`).

Displays the commit message(s) and prompts Y/n. Fails if the agent branch is not a fast-forward from the target shared branch. The agent must rebase first.

On success: merges the commits into the target branch, records one approval per commit in the database, and pushes the updated shared branch to cave. The agent branch is reset to the new shared branch head. If the agent is running, it continues working from the updated base. If the agent is paused, it is removed (registry entry, branches, workspace) since no unapproved work remains.

### `cav reject [agent-id]`

Rejects the tip commit of the current shared branch. Must be run from a shared branch. Refuses if the commit has been pushed to `origin` (use `git revert` instead). Prompts Y/n for confirmation. Run repeatedly to reject multiple commits.

Resets the shared branch by one commit and force-pushes to cave. Creates an agent branch at the rejected commit so the work can be reworked. Deletes the corresponding approval record.

If the original agent is still in the registry, the branch is restored to that agent automatically. If the original agent was cleaned up, reject fails and instructs the user to start or continue an agent session first, then re-run with the agent ID: `cav reject <agent-id>`. The target agent must have no unapproved work of its own on that branch.

### `cav pull`

Fetches from the cave remote and updates the current branch.

On **shared branches**, fast-forward merges. Equivalent to `git fetch cave && git merge --ff-only cave/<branch>`. Fails if the branches have diverged. Force-pull is not allowed on shared branches, because the host repo is the source of truth. Use `cav push --force` to correct the cave instead.

On **agent branches**, always hard-resets to the remote agent branch. Agent branches are ephemeral on the host. The cave agent is authoritative, so pull unconditionally accepts the remote version. Shows the agent's commits relative to the target branch.

### `cav push`

Pushes the current branch to the cave remote. With `--force`, force-pushes (for rollback).

### Idempotency

`cav pull` and `cav push` are idempotent. Running them multiple times is equivalent to running once. A pull with nothing new says "Already up to date." A push with nothing new is a no-op.

### `cav status`

Fetches from the cave remote and shows agent activity. Agents are derived from the registry, not from branch names. For each registered agent, shows whether it is running, paused (has unapproved commits), or idle. Unapproved commits are listed newest-first, matching `git log` order. Read-only aside from the fetch. With `--all`, iterates all registered projects and shows status for each.

### `cav remove-agent <n>`

Removes a paused agent by ID. Deletes its session from the database, its remote agent branches, local agent branch checkouts, and its workspace. Warns about any unapproved commits being discarded. Refuses to remove a running agent.

### `cav remove-agent --all [--yes]`

Removes all paused agents and resets the database. Also deletes local agent branch checkouts. Refuses if any agents are currently running. Prompts for confirmation unless `--yes` is given.

### `cav cp [-R] [-f] <src>... <dest>`

Copies files between the host and a cave agent worktree or persistent home. One of the paths must use a cave prefix:

- `slug:path` - project-relative path in the agent's worktree
- `slug@N:path` - specific agent N's worktree
- `cave:path` - auto-detect slug from CWD (project must be registered)
- `slug:~/path` - persistent cave home (no worktree required)
- `slug:/projects/slug/path` - absolute cave path inside the project

Direction is determined by which path has the cave prefix. If the destination has a cave prefix, copies host files into the worktree. If a source has a cave prefix, copies from the worktree to the host.

If multiple agents have worktrees for a slug, the `@N` form is required. Refuses to copy symlinks out of the cave. Prompts before overwriting existing host files unless `-f` is given.

### `cav prune-branches`

Deletes orphaned local agent branch checkouts. An agent branch is orphaned if it has no upstream or its cave upstream no longer exists. Refuses to touch agent-named branches tracking a non-cave remote. Prompts for confirmation before deleting.

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
