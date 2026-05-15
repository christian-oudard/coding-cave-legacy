# Coding Cave

## Purpose

The Coding Cave is a secure space for agents to write software.

We want to delegate real software-development work to agents — agents that can experiment, run builds, install packages, edit files, fail, retry, and produce useful output without us writing detailed permission allowlists for every possible action. Three properties make that delegation safe enough to do:

1. **Freedom inside.** Each agent works in a full POSIX development environment configured to the user's taste. Inside the cave, the agent is unconstrained: it can install software, run anything, edit anything, try things.
2. **A narrow output channel.** The only thing that leaves the cave is git commits on the agent's own branch. Anything the agent wants the user to keep, it expresses as a commit.
3. **A strong boundary.** The cave keeps the agent's freedom inside the cave. The agent cannot reach the user's host filesystem, processes, credentials, or kernel except where the system explicitly forwards something.

The user stays in control by reviewing the agent's commits and choosing whether they merge into the canonical repository. The agent never publishes directly to the user's repo, never has GitHub credentials, and never touches the user's host.

## Review workflow

Three locations participate:

- **the cave** — the agent's workspace, sealed off from the host.
- **the shared remote** — a bare repository that mediates between cave and host. The cave's view of it is called `host-cave`; the host's view is called `cave`.
- **the host repository** — the user's own working copy, where the canonical branches live. It will typically also have an `origin` remote (e.g. GitHub) for publication, which the cave never sees.

Each agent works on its own copy of a base branch (`main`, `develop`, `issue/123`, etc.). Inside the cave, the agent sees its copy as the base branch name. On the shared remote and the host repo, agent copies are namespaced as `agent-<n>/<base>` (e.g. `agent-1/main`) so they don't collide with the canonical base branch or with other agents' copies.

Code flows through the system as follows:

**Development.** The agent commits work in its cave. Each commit is automatically published to the shared remote as `agent-<n>/<base>`. The user brings those commits into their host-side view of the agent's branch with `cav pull`. If the user makes manual edits, `cav push` sends them through the shared remote, where the agent's cave picks them up.

**Approval.** When the user is satisfied with the agent's work, `cav approve` fast-forwards the canonical base branch to the tip of the agent's branch. Other agents working on the same base are rebased onto the new tip. Approval is always a clean fast-forward — never a merge commit.

**Publication.** The host pushes the canonical branch to GitHub (or wherever) the way the user normally would. The agent has no part in publication and no GitHub credentials.

**Rejection.** `cav reject` undoes the most recent approval: it rolls back the canonical base branch on host and shared remote, and deletes the approval record. Reject is blocked when an agent's dev branch has diverged from the rolled-back base in a way that would lose work; the operator either rebases the agent's branch or removes the agent entirely.

**Rebase.** When the canonical base branch advances, each agent's branch is automatically rebased onto the new base. History rewrites flow through `cav pull` and `cav push`, with `--force` required to clobber the other side's work.

**Illegal actions.** The system enforces that agents can only publish to their own branch. Agents cannot push to canonical base branches or to another agent's branch.

## Projects

Projects are registered with `cav init`. Each project gets a unique slug based on its folder name. A valid project is a git repository with at least one commit, because only committed work is visible from inside the cave.

## Agents

Agent sessions are launched with `cav agent`. Each session gets a unique numeric ID (1, 2, 3, ...) and a display name combining project slug and ID (e.g. `web-frontend@1`, `data_pipeline@2`). Each session has its own dev branch (`agent-<n>/<base>`) and its own workspace.

`cav status` shows running agents, stopped agents with unmerged work, and the state of each agent's branch relative to its base. The sort order is most recent first.

Each agent session is launched in the context of a project directory; that's its primary project and supplies its display name. With `cav agent -p <other>` the agent can access additional projects, with one dev branch per project.

Caves are recursive: agents can launch their own subagents. Each level has its own review workflow — a subagent's work is approved into the dev branch it shares with its parent agent, which is in turn pushed up for approval at the next level.

Agents can optionally communicate with each other through inter-agent messaging, enabled per-session with `cav agent --comms`.

## Inside the cave

What the agent sees inside its cave:

- A standard POSIX environment and bash shell.
- The user-chosen toolchain (compilers, language runtimes, editors, claude-code, anything declared in cave.nix). The agent can install additional packages from nixpkgs at runtime.
- Their projects mounted at `/projects/<slug>`, one directory per project.
- A persistent home directory at `/home/cave-coder` that survives across sessions.
- The ability to sign git commits with the user's git identity, without ever seeing the signing keys directly.
- Optionally, a host TCP port forwarded into the cave per `cav agent --port`.
- Optionally, a host device node (e.g. `/dev/nvidia0` for GPU work) forwarded per `cav agent --dev`.

What the agent never sees: the user's host filesystem outside the cave, the user's SSH keys or API tokens, other caves' workspaces, anything on the host network that hasn't been explicitly forwarded.

## Credential isolation

Caves do not contain SSH keys or API tokens. When the agent makes a network request to a service that needs credentials, the system's outbound proxy injects them on the agent's behalf. The agent can use the service; the agent cannot extract the credential.

## Cave configuration

The cave environment is configured by `~/.config/coding-cave/cave.nix`. This is a simple Nix file (not a flake) with an optional `inputs` attrset for user dependencies and a `config` function that receives `pkgs` and those inputs. The system supplies its own dependencies (nixpkgs, claude-code, home-manager, the coding-cave system itself) without the user having to spell them out, and a small set of core packages (coreutils, bash, nix, git) is always included.

Without a `cave.nix`, a default environment is generated with claude-code and standard tools. `cav init` offers to create one for new users.

`cav env update` updates the environment's flake inputs (e.g. `cav env update claude-code` for the latest Claude Code), and `cav env build` rebuilds the environment. `cav env metadata` shows the locked versions of all inputs.

A system-supplied CLAUDE.md is prepended to the user's CLAUDE.md at session startup so agents inherit baseline coding-cave conventions.

## Commands

CLI output generally follows Git's conventions: oneline-style commit lists, ahead/behind counts where relevant, and command-echo with the `-v`/`--verbose` flag (prefixed with `+`, similar to `set -x`).

### `cav init`

Registers a project with the coding-cave system. The project must be a git repository with at least one commit.

### `cav agent [-c,--continue | -r,--resume <agent_id/session_id>] [-p,--project <project_slug>] [--port <port>] [-d,--dev <path>] [--comms]`

Launches a Claude Code session in a new coding-cave session. Auto-assigns a numeric agent ID.

`--continue` resumes the most recently exited session. `--resume <id>` resumes a specific session, by session ID or agent ID.

`--project` (repeatable) gives the agent access to additional projects.

`--port` (repeatable) forwards a host TCP port into the cave. The agent reaches it at `10.0.2.2:<port>`.

`--dev` (repeatable) forwards a host device node (e.g. `/dev/nvidia0`) into the cave. Devices must be under `/dev/` and refer to a char or block device. All forwarded devices must share an owning group of which the host user is a member.

`--comms` enables inter-agent communication: the agent gets a `send_message` tool and receives other agents' messages as channel events.

### `cav approve [agent-id] [--commit <SHA>]`

Approves an agent's work, fast-forwarding the canonical base branch to the tip of the agent's branch. Agent ID is inferred from the currently checked-out agent branch if not given. Approval errors out if the merge isn't a fast-forward (the agent must rebase first).

`--commit SHA` does a partial approval, fast-forwarding only up to that commit. The remaining commits stay on the agent's branch for a later approval.

### `cav reject [agent-id]`

Undoes the most recent approval on the current base branch: rolls back the host and shared remote, and deletes the approval record. The agent's dev branch is untouched.

Reject is blocked when the agent's dev branch has diverged from where the base will be after rollback, in a way that would lose work. The block directs the operator to either rebase the agent (the common case) or `cav remove-agent` to discard the branch entirely.

### `cav pull [--force]`

Brings the agent's recent commits into the user's host-side view of the agent's branch. Idempotent. With `--force`, accepts history-rewriting updates that would clobber user commits.

### `cav push [--force]`

Pushes the current branch up to the shared remote. Valid on both canonical base branches and agent branches. If pushed to an agent branch, the cave automatically picks up the update. Idempotent. With `--force`, rewrites history.

### `cav checkout <branch> [agent-id]`

Retargets an agent to a different base branch. The agent's workspace switches to the new base, and any unmerged work is rebased onto it. Agent ID is inferred from the currently checked-out branch if present.

### `cav status [-a,--all] [agent-id]`

Shows the current list of agents and their unmerged commits — for the current project, or with `--all` for every project. Optionally filtered to one agent.

The status command is informational, and is also when lightweight maintenance happens: agents whose process has died are removed if they left no pending work, and shared dev branches whose tip already matches their base are dropped.

### `cav remove-agent [--yes] [<n>]`

Removes a stopped agent: deletes its session, workspace, and dev branch. Refuses to remove a running agent. Warns about unapproved work and prompts for confirmation unless `--yes` is given. If the agent ID is omitted and there's exactly one stopped agent on the project, that one is selected.

### `cav remove-project <slug>`

Unregisters a project. Refuses if any agents are still running on it. Does not delete the project directory or its work — only the registry entry.

### `cav env build`

Force-rebuilds the cave environment from `~/.config/coding-cave/cave.nix`. Useful for iterating on cave.nix without launching an agent.

### `cav env update [INPUT]`

Updates the environment's flake inputs. With an argument, updates only that input (e.g. `cav env update claude-code`). Without, updates all of them. Run `cav env build` afterward to rebuild.

### `cav env metadata`

Shows the locked versions of all flake inputs in the cave environment.

### `cav cp [-R] [-f] [(cave|<slug>)[@<n>]:]<src> [(cave|<slug>)[@<n>]:]<dest>`

Copies files between the host and a cave, similar in spirit to `scp`. Exactly one of source or destination has a `cave:` prefix; the prefix decides direction.

Cave paths are relative to the agent's worktree, or with `~/` are in the cave's shared home directory, or with `/` are absolute paths from the cave's filesystem root. Project and agent are specified by:

- `<slug>:<path>` — some agent in project `<slug>`
- `<slug>@<n>:<path>` — agent N in project `<slug>`
- `cave:<path>` — auto-detect the project slug from the current directory
- `cave@<n>:<path>` or `@<n>:<path>` — auto-detect slug, with explicit agent

If a project has multiple agents and no agent is named, the command prompts. For safety, symlinks are not copied out of the cave. Existing files are not overwritten without confirmation, unless `-f` is passed.

Tab completion for cave-side paths is available with the shell completion installed (`cav completion`).

### `cav completion bash|zsh|fish`

Prints a shell completion script to stdout. Source it from your shell rc to enable tab completion for `cav` commands and cave-side paths.

```
eval "$(cav completion bash)"   # in ~/.bashrc
eval "$(cav completion zsh)"    # in ~/.zshrc, after compinit
cav completion fish | source    # in fish
```

### `cav shell`

Open an interactive shell session inside a running agent's cave.
