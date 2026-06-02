# Coding Cave

## Purpose

The Coding Cave is a secure space for agents to write software. It has a strong boundary enforced with bubblewrap, so that the agents only have access to their own development environments, and not the host machine. Instead of dictating detailed permissions for each possible action, we give the agent room to experiment, and receive only git commits as their output. All commits are reviewed and approved by the user before merging to the user's host repository.

## Git Topology

Locations:
- **agent cave** - a worktree (shared clone of the bare repo, objects via alternates) inside the bubblewrap sandbox
- **shared remote** - a bare repository (`.coding-cave/cave-shared.git`) which is the coordination point between agent and host
- **host** - the user's repository

The agent cave refers to the shared remote as `host-cave`, but the host refers to it as `cave`. The host will generally also have a separate `origin` remote for publishing to GitHub.

Each agent works on its own copy of a base branch (`main`, `develop`, `issue/123`, etc.) in its sandbox workspace. The agent sees its copy by the base branch name: inside the cave, the local branch is simply `main`. On the shared remote and the host repo, agent copies are namespaced under `agent-<n>/<base>` (e.g. `agent-1/main`) so they don't collide with the canonical base branch or with other agents' copies.

Here is how code moves through the system:

**Development** The agent commits work to its sandboxed worktree. Each commit is automatically published to the shared remote as `agent-<n>/<base>`. The user runs `cav pull` to bring those commits into their host-side view of the agent's branch. If the user makes manual edits, they run `cav push` to send them through the shared remote, where they are picked up by the agent's worktree.

**Approval** When the user is satisfied with the agent's work, they run `cav approve`, which fast-forwards the canonical base branch (`main` and `cave/main`) to the tip of the agent's branch. Other agent worktrees are rebased on the newly updated `cave/main`. This is always a clean fast-forward, never a merge commit.

**Publication** The host pushes the main branch to the GitHub origin, like normal. The agent never has GitHub credentials, and this local review process does not use the GitHub pull request system.

**Rebase** Any time `cave/main` receives new commits, an attempt is made to rebase each agent's branch on the new base. New commits on `main` can be pushed to the `cave/main` remote branch with `cav push` or `cav push --force` if they rewrite history. `cav push` commands trigger rebase for agent branches.

**History rewrites**
When agents amend or squash commits, the user can run `cav pull` to pull history-rewriting updates. If these updates would clobber user commits, `cav pull --force` is required to authorize this.

**Illegal Actions**
Git hooks enforce that git commands run by agents are topologically correct. Agents cannot publish to canonical base branches (e.g. `main`), or to another agent's branch.

## Projects

Projects are registered with the coding-cave system with `cav init`. This creates the shared bare repo and adds the project to the project registry database table. Each project is assigned a unique slug based on its folder name.

## Agent IDs

Agent sessions are launched with `cav agent`, or just `cav` for short. Agent sessions are identified by unique numeric IDs (1, 2, 3, etc.). Display name tags combine the project slug with the ID: `web-frontend@1`, `data_pipeline@2`, and so on. Dev branches are named `agent-<n>/<base>` (e.g. `agent-1/main`). Each agent gets its own workspace clone of the shared repo. Agent session information, including Claude session IDs, are stored in the agent registry database table.

`cav status` shows current running agents and work in progress, as unmerged commits. The agent session can be running or stopped. Running sessions are visible in `cav status`. Stopped sessions with unmerged work also show in `cav status`.

Each agent session is launched in the context of a project directory. This is their primary project, which gives the agents name tag its project slug.

## Multi-project
`cav agent -p otherproject` allows agents to access multiple projects. They have one dev branch per project, with its associated worktree.

## Nested Caves
Agents can launch their own agents, creating a subcave one level deeper. Each level has its own shared remote, so the same approve/review/push/pull workflow applies at every depth. A subcave sees its parent's shared remote read-only as `host-cave` (the up-link) and coordinates its own subagents through its local shared remote as `cave` (the down-link). Commits flow upward one level at a time through approval: a subagent's work is approved into the dev branch it shares with its parent agent, which in turn pushes that for approval at the next level up.

## Sandbox

Each agent runs in bubblewrap sandbox.
- The development environment is a standard POSIX environment and Bash shell.
- Agents can install Nix packages to a Nix store overlay (host store read-only, writable upper layer).
- Project workspaces are mounted at `/projects/<slug>`.
- A persistent shared home directory is at `/home/cave-coder`.
- Agents sign commits with host GPG keys, through a forwarded GPG agent extra socket. The extra socket runs in restricted mode, allowing signing but blocking secret key export and other dangerous operations.
- Git commits are signed with the user's Git identity, stored in environment variables.
- The Standard POSIX environment (SHELL, TERM, locale).

## Credential Isolation
Caves do not have access to any of your SSH keys or API tokens. Instead, all web traffic is routed through a credential injection man-in-the-middle proxy, which wraps network traffic and injects credentials.

## User/System Configuration

The cave environment is configured by `~/.config/coding-cave/cave.nix`. This is a simple Nix config (not a flake) with an optional `inputs` attrset for user dependencies and a `config` function that receives `pkgs` and those inputs. The system wraps it in a proper flake, injecting system dependencies (nixpkgs, claude-code, home-manager, coding-cave) via build-time substituted store paths. The user never specifies system dependency URLs. Core packages (coreutils, bash, nix, git) are always included automatically. Without a cave.nix, a default environment is generated with claude-code and standard tools. The build result is cached in `~/.local/share/coding-cave/user-env/` and skipped on agent startup if nothing changed. `cav env update` updates flake inputs (e.g. `cav env update claude-code` for latest Claude Code). `cav init` offers to create a default cave.nix for new users. The cave gives the agent a baseline system prompt at session startup, covering cave conventions and the list of projects mounted into this launch. Users who want CLAUDE.md content in the cave set `programs.claude-code.context` in their `cave.nix`, just like any other home-manager option. All coding cave state is in `~/.local/share/coding-cave/`. A SQLite database tracks registered agents, projects, and approvals.


## Commands

### `cav init`

Adds a project to the coding-cave registry. Sets up the shared bare repo, and branch protection hooks. A valid project is a git repository with at least one commit, because only committed work is shared with the agent.

### `cav agent [-c,--continue | -r,--resume <agent_id/session_id>] [-p,--project <project_slug>] [--port <port>] [-d,--dev <path>] [--comms]`

Launches a Claude Code CLI agent in a coding-cave session. Auto-assigns a numeric ID. There are no agent ID duplicates among running agents or stopped agents with unmerged work.

Continue the most recently exited session with `--continue`. Continue arbitrary sessions, by session-id or agent-id, with `--resume`. Per-launch flags (`--project`, `--port`, `--dev`, `--comms`) apply only to the current launch and do not carry over from the original session; the agent's system prompt is rebuilt each launch to reflect the flags actually in effect.

Allow access to multiple projects with the `--project` flag (repeatable).

With `--port`, expose a host TCP port inside the cave (repeatable). The agent can reach it at `10.0.2.2:<port>`.

With `--dev`, bind-mount a host device node (e.g. `/dev/nvidia0`) into the cave (repeatable). The path must be under `/dev/` and refer to a char or block device. All paths must share an owning group, and the host user must be a member of that group.

With `--comms`, enable inter-agent communication: the agent gets a `send_message` tool for messaging other agents and receives their messages as channel events. Off by default.

All caves are recursive. Agents can launch sub-caves for testing coding-cave features.

### `cav approve [agent-id] [--commit <SHA>]`

Approves an agent's work and fast-forwards the canonical base branch to the tip of the agent's branch. Specify which agent ID number to approve, or the agent ID is inferred from the currently checked-out agent branch. Approval errors out if the merge is not a fast-forward (the agent must rebase first). To process an approval, the command advances the base branch on both the cave remote and the host repo.

With `--commit SHA`, approve only up to that commit (partial approval). The dev branch is preserved with remaining commits for a future approval.

### `cav reject [agent-id]`

Rejects the tip commit of the current base branch. Must be run from a base branch. This undoes the approval that got the commits into the base branch in the first place: rolling back the base branch on host and shared remote, and deleting the approval record. The agent's dev branch is not touched by reject. Reject is blocked when the target agent's dev branch has diverged from where main will be after the rollback — that is, the dev branch is neither a fast-forward target from the new base nor at-or-behind it. The block directs the operator to have the agent rebase their branch onto the new base (the common case), or run `cav remove-agent` to discard the dev branch entirely. If the original agent has been removed from the registry, a different agent must be specified.

### `cav pull [--force]`

Moves work from the agent's branch to the host's view of it. One push from agent to shared, and one pull from shared to host. This is only valid on agent branches, not on canonical base branches. The pull command is idempotent, multiple calls are the same as one call.

### `cav push [--force]`

Pushes the current branch to the shared remote. With `--force`, force-pushes (for rollback or history rewriting). Valid on both base branches and agent branches. If we push to an agent branch, the agent's workspace is automatically updated. The push command is idempotent, multiple calls are the same as one call.

### `cav checkout <branch> [agent-id]`

Retargets an agent to a different base branch. The agent's workspace is switched to the new base, and its unmerged work is rebased onto it. Agent ID is inferred from the currently checked-out agent branch if present, otherwise it must be specified.

### `cav status [-a,--all] [agent-id]`

Shows the current list of agents and unmerged commits for the current project, or for all projects with `cav status --all`. Optionally filter to a single agent by ID. The agent ID resolves from the registry regardless of the current working directory. It fetches from the shared remote to get latest information, and queries the registry database. As a side effect, the status command performs lightweight maintenance: it reaps agents whose process has died and removes their registry entries and branches if they left no pending work, and it drops shared dev branches whose tip already matches their base (already merged). The status command shows agent status of "running" or "stopped". The sort order is most recent first. Similar to `git status`, it shows whether the host base branch is behind or ahead of the cave remote.

### `cav remove-agent [--yes] [<n>]`

Removes a paused agent by agent ID. Deletes its session from the database, deletes its worktree, deletes its remote agent branch, and deletes the corresponding host-side checkout. Warns about unapproved commits being discarded, giving commit hashes, and prompts for confirmation when there is unapproved work or uncommitted changes; with `--yes`, skips that prompt. If the agent ID is omitted, the only paused agent in the project is selected. Refuses to remove a running agent.

### `cav remove-project <slug>`

Unregisters a project from the coding-cave registry. Refuses if the project has running agents. Does not delete the project directory, worktrees, or the shared bare repo — it only removes the registry entry. Use this to clean up after a project directory has been moved or deleted.

### `cav env build`

Force-rebuilds the cave environment from `~/.config/coding-cave/cave.nix` (or the default). Useful for iterating on cave.nix without starting an agent.

### `cav env update [INPUT]`

Updates the flake lock file without rebuilding. With an argument, updates only that input (e.g. `cav env update claude-code`). Without, updates all inputs. Run `cav env build` separately to rebuild after updating.

### `cav env metadata`

Shows the locked versions of all flake inputs in the cave environment.

### `cav cp [-R] [-f] [(cave|<slug>)[@<n>]:]<src> [(cave|<slug>)[@<n>]:]<dest>`

Copies files between the host and a cave agent worktree, or to cave persistent home. Similar to `scp` for ssh remote hosts. Exactly one of the source and destination paths must specify a file in the cave. Paths are relative to the agent worktree, or with `~/` they are paths in the coding-cave shared home directory, or with `/` they are specified from the coding-cave filesystem root. There are several ways to specify which project and agent id to copy files from or to:
- `<slug>:<path>` - a path in some agent's worktree
- `<slug>@<n>:<path>` - the working tree for agent N.
- `cave:<path>` - auto-detect the project slug from current project directory
- `cave@<n>:<path>` or `@<n>:<path>` - auto-detect the project slug, agent N

Direction is determined by which path has the `cave:` prefix. If the destination has a cave prefix, copies host files into the worktree. If the source has a cave prefix, copies from the worktree to the host. Multiple sources can be specified, similar to the `cp` command.

If a project has multiple agents, the `@<n>` suffix specifies an agent. If no agent id is given, there is a prompt which asks which agent session to copy into. For security reasons, it refuses to copy symlinks out of the cave. `cav cp` prompts before overwriting existing files, both in the cave and on the host, unless `-f` is given.

Tab completion is supported for cave-side paths once the shell completion script is installed (see `cav completion`). Typing `cav cp cave:src/<TAB>` lists files inside the cave worktree, similar to how `scp host:<TAB>` lists files on a remote host.

### `cav completion bash|zsh|fish`

Prints a shell completion script to stdout. Source it from your shell rc to enable tab completion for `cav` commands and arguments, including cave-side path completion for `cav cp`.

```
eval "$(cav completion bash)"   # in ~/.bashrc
eval "$(cav completion zsh)"    # in ~/.zshrc, after compinit
cav completion fish | source    # in fish
```

### `cav shell`

Start a shell session in a running agent's worktree, inside the bubblewrap sandbox.

## Output Style

CLI output generally follows Git's conventions:

- Echo the git commands being run, prefixed with `+`, when the `-v`/`--verbose` option is set, similar to `set -x` in shell scripts.
- Use Git's `--oneline` format to list commits.
- Say how many commits ahead or behind we are when relevant.
- Show commit subject lines for new work, not just how many commits counts.
