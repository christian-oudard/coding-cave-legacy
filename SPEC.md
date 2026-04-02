# Coding Cave

## Purpose

The Coding Cave is a secure space for agents to write software. It has a strong boundary enforced with bubblewrap, so that the agents only have access to their own development environments, and not the host machine. Instead of dictating detailed permissions for each possible action, we give the agent room to experiment, and receive only git commits as their output. All commits are reviewed and approved by the user before merging to the user's host repository.

## Git Topology

Locations:
- **agent cave** - a worktree (shared clone of the bare repo, objects via alternates) inside the bubblewrap sandbox
- **shared remote** - a bare repository (`.coding-cave/cave-shared.git`) which is the coordination point between agent and host
- **host** - the user's repository

The agent cave refers to the shared remote as `host-cave`, but the host refers to it as `cave`. The host will generally also have a separate `origin` remote for publishing to GitHub.

There are two kinds of branches:
- **Base branches** (`main`, `develop`, `issue/123`, etc.) are the project's normal branches. Agents cannot access them directly. Only the user modifies base branches, via `cav approve` and normal manual development.
- **Dev branches** are named `agent-<n>/<base>` (e.g. `agent-1/main`, `agent-2/develop`). These are the working branches for incomplete work to be reviewed and tested.

Here is how code moves through the system:
**Development** The agent commits work to its sandboxed worktree on the dev branch (`agent-N/main`). When the user runs `cav pull`, the agent pushes to the shared remote dev branch, and the user pulls that work from the same "remote" dev branch to their own repo dev branch. If the user needs to make manual edits, `cav push` on a dev branch pushes to the shared remote, and pulls from the shared remote to the agent dev branch.

**Approval** When the user is satisfied with the state of the dev branch, they run `cav approve`, which merges the pending commits into the base branch (`main` and `cave/main`). Also, agent worktrees are rebased on the newly updated `cave/main`. This is always a clean fast-forward, never a merge commit.

**Publication** The host pushes the main branch to the GitHub origin, like normal. The agent never has GitHub credentials, and this local review process does not use the GitHub pull request system.

**Rebase** Any time `cave/main` receives new commits, an attempt is made to rebase agent dev branches on the new `cave/main`. New commits on `main` can be pushed to the `cave/main` remote branch with `cav push` or `cav push --force` if they rewrite history. `cav push` commands trigger rebase for agent dev branches.

**History rewrites**
When agents amend or squash commits, the user running `cav pull --force` pulls history-rewriting updates, which runs agent-side `git push --force` to push agent rewritten history to the shared remote dev branch, and then runs `git reset --hard` to "force-pull" to the host dev branch.

**Illegal Actions** 
Git hooks enforce that git commands run by agents are topologically correct. Agents cannot push to cave/main, or to dev branches that do not belong to them.

## Projects

Projects are registered with the coding-cave system with `cav init`. This creates the shared bare repo and adds the project to the project registry database table. Each project is assigned a unique slug based on its folder name.

## Agent IDs

Agent sessions are launched with `cav agent`, or just `cav` for short. Agent sessions are identified by unique IDs: agent-1, agent-2, agent-3, etc.  Display name tags combine the project slug with the ID: `web-frontend@1`, `data_pipeline@2`, and so on. Each agent gets its own workspace clone of the shared repo. Agent session information, including Claude session IDs, are stored in the agent registry database table.

`cav status` shows current running agents and work in progress, as unmerged commits. The agent session can be running or stopped. Running sessions are visible in `cav status`. Stopped sessions with unmerged work also show in `cav status`.

Each agent session is launched in the context of a project directory. This is their primary project, which gives the agents name tag its project slug.

## Multi-project
`cav agent -p otherproject` allows agents to access multiple projects. They have one dev branch per project, with its associated worktree.

## Sandbox

Each agent runs in bubblewrap sandbox.
- The development environment is a standard POSIX environment and Bash shell.
- Agents can install Nix packages to a Nix store overlay (host store read-only, writable upper layer).
- Project workspaces are mounted at `/projects/<slug>`.
- A persistent shared home directory is at `/home/cave-coder`.
- Agents sign commits with host GPG keys, through a forwarded GPG agent socket. The GPG private keys stay on the host.
- Git commits are signed with the user's Git identity, stored in environment variables.
- The Standard POSIX environment (SHELL, TERM, locale).

## Credential Isolation
Caves do not have access to any of your SSH keys or API tokens. Instead, all web traffic is routed through a credential injection man-in-the-middle proxy, which wraps network traffic and injects credentials.

## User/System Configuration

Base packages are infrastructure only: claude-code, nix, git, coreutils, and standard unix tools. No editors, language runtimes, or dev tools. A system CLAUDE.md introducing the coding-cave environment is prepended to the user's CLAUDE.md at session startup. `~/.config/coding-cave/cave.nix` declares per-user Claude Code configuration, Nix packages, and environment variables. All coding cave state is in the folder `~/.local/share/coding-cave/`. A SQLite database tracks registered agents, projects, and approvals. 


## Commands

### `cav init`

Adds a project to the coding-cave registry. Sets up the shared bare repo, and branch protection hooks. A valid project is a git repository with at least one commit, because only committed work is shared with the agent.

### `cav agent [-c,--continue | -r,--resume <agent_id/session_id> [-p,--project <project_slug> ] [--recursive]`

Launches a Claude Code CLI agent in a coding-cave session. Auto-assigns a numeric ID. There are no agent ID duplicates among running agents or stopped agents with unmerged work.

Continue the most recently exited session with `--continue`. Continue arbitrary sessions, by session-id or agent-id, with `--resume`.

Allow access to multiple projects with the `--project` flag (repeatable).

With `--recursive`, the cave agent can launch their own sub-caves. This is mainly for automated testing of coding-cave features.

### `cav approve [agent-id]`

Approves a dev branch's work, and merges it into the base branch. Specify which agent ID number to approve, or the agent ID is inferred from the current dev branch if one is checked out. Approval errors out if the dev branch is not possible to fast-forward merge to the base branch (the agent must rebase first). To process an approval, the command pushes the dev branch tip to the base branch, on both the cave remote and host repo.

### `cav reject [agent-id]`

Rejects the tip commit of the current base branch. Must be run from a base branch. This means to undo the approval that got it into the base branch in the first place. this means running `git reset --hard` on the host repo base branch to remove the commit, and ensuring that it exists as the tip of the dev branch, for both host and agent. If the agent has exited and their dev branch is gone, a running agent with no unmerged commits is required to give the rejected commits a place to go.

### `cav pull [--force]`

Moves work from the agent dev branch to the host dev branch. One push from agent to shared, and one pull from shared to host. This is only valid on dev branches, not on base branches. The pull command is idempotent, multiple calls are the same as one call.

### `cav push [--force]`

Pushes the current branch to the shared remote. With `--force`, force-pushes (for rollback or history rewriting). Valid on both base branches and dev branches. If we push to a dev branch, the agent dev branch is automatically updated. The push command is idempotent, multiple calls are the same as one call.

### `cav checkout <branch> [agent-id]`

Changes the current base branch to `<branch>`. The host repo runs `git checkout <branch>`. The agent creates a new branch `agent-<n>/<branch>`. Unmerged work from the previous agent dev branch is rebased on the new shared base branch. Agent ID is inferred from the current branch if on a dev branch, otherwise agent ID must specified.

### `cav status [-a,--all]`

Shows the current list of agents and unmerged commits for the current project, or for all projects with `cav status --all`. It fetches from the shared remote to get latest information, and queries the registry database. The status command is read-only, and does not change registry database state or Git state except for fetching. The status command shows agent status of "running" or "stopped". The sort order is most recent first. Similar to `git status`, it shows whether the host base branch is behind or ahead of the cave remote.

### `cav remove-agent [--all] <n>`

Removes a paused agent by agent ID. Deletes its session from the database, and deletes its work tree, and and deletes its remote dev branch, and deletes the host dev branch checkout. Warns about unapproved commits being discarded, giving commit hashes. Refuses to remove a running agent. `--all` removes all agents. The command asks for confirmation before removing agents. `--yes` skips the confirmation.

### `cav cp [-R] [-f] [(cave|<slug>)[@<n>]:]<src> [(cave|<slug>)[@<n>]:]<dest>`

Copies files between the host and a cave agent worktree, or to cave persistent home. Similar to `scp` for ssh remote hosts. Exactly one of the source and destination paths must specify a file in the cave. Paths are relative to the agent worktree, or with `~/` they are paths in the coding-cave shared home directory, or with `/` they are specified from the coding-cave filesystem root. There are several ways to specify which project and agent id to copy files from or to:
- `<slug>:<path>` - a path in some agent's worktree
- `<slug>@<n>:<path>` - the working tree for agent N.
- `cave:<path>` - auto-detect the project slug from current project directory

Direction is determined by which path has the `cave:` prefix. If the destination has a cave prefix, copies host files into the worktree. If the source has a cave prefix, copies from the worktree to the host. Multiple sources can be specified, similar to the `cp` command.

If a project has multiple agents, the `@<n>` suffix specifies an agent. If no agent id is given, there is a prompt which asks which agent session to copy into. For security reasons, it refuses to copy symlinks out of the cave. `cav cp` prompts before overwriting existing files, both in the cave and on the host, unless `-f` is given.

### `cav shell`

Start a shell session in a running agent's worktree, inside the bubblewrap sandbox.

## Output Style

CLI output generally follows Git's conventions:

- Echo the git commands being run, prefixed with `+`, when the `-v`/`--verbose` option is set, similar to `set -x` in shell scripts.
- Use Git's `--oneline` format to list commits.
- Say how many commits ahead or behind we are when relevant.
- Show commit subject lines for new work, not just how many commits counts.
