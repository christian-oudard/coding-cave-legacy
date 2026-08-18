# vm-cave: what is left to reach parity with `main`

What the container caves on `main` do that the VM does not do yet, read off
`main`'s SPEC.md against the tree on this branch (`scripts/vmcav`,
`cave-vm/internal/{agentd,approve,nixcache,registry,vmm}`, `guest/`).

This is a gap list, not a plan. Where the design already covers a gap, the
phase is named and the design is the authority on how it lands;
`vm-cave-todo.md` owns the gaps that are not phases. Parity is also not the
finish line: the nix package cache and pkgdd have no counterpart on `main` at
all, since store sharing there was removed.

Order below is roughly dependency order, not priority.

## Caves do not exist yet

Design phase 6. The agent is root in the guest with no sandbox at all, so
every item further down that says "per agent" or "per project" has nothing to
hang off.

- one guest uid per agent, private home, home materialized one-way from the
  seed tree at cave creation
- bwrap profile: ro /nix/store, the nix-daemon socket, shared caches, the
  agent's worktrees at `/projects/<slug>`
- the seccomp policy, lifted from cav-rs's sandbox config rather than
  re-derived. How that config crosses the cleanroom line is still open in
  `vm-cave-design.md`.

## Projects

`vmcav` serves exactly one bare repo, the coding-cave repo itself, at a path
it computes. There is no notion of a project anywhere host-side except an
unused `Project` field in the registry.

- `cav init`: register a project, create its shared bare repo, install the
  branch-protection hooks
- the project registry itself, and slugs derived from directory names
- `-p/--project`: more than one project per agent, one dev branch and one
  worktree each
- `remove-project`, `rename-project`, and the re-registration of a project
  directory that moved on disk

## Multiple agents in one VM

Sessions are already one process and one unit each, which is the durability
half. The identity half is missing, and `vm-cave-todo.md` holds the open
questions (uid per agent versus all root, where ids are allocated, one vsock
port per agent).

- the git daemon injects `CODING_CAVE_AGENT_ID=1` for the whole guest, so the
  pre-receive guard cannot tell co-resident agents apart
- one ssh identity and one `/root/coding-cave` checkout, so two agents would
  share a working tree
- `vmcav agent` attaches to "the" agent with no way to name one, though
  `cave-guestd attach <id>` underneath it already takes an id

## Git flow

Phase 7, about half landed. Present: the vsock git bridge on 7300, the
pre-receive and post-receive hooks, and `review`, `diff`, `approve` as
fast-forward of a whole agent branch onto its base.

- `pull` and `pull --force`: move an agent's work into the host's view of it
- `push` and `push --force`: send host-side edits back through the shared
  remote into the agent's worktree
- `checkout`: retarget an agent to a different base and rebase its unmerged
  work onto it
- `reject`: roll a base branch back and delete the approval record, with the
  divergence check that blocks it
- `approve --commit <sha>`: partial approval, dev branch preserved with the
  remainder
- automatic rebase of every agent branch when a base branch moves
- the host-side checkout of an agent branch that `pull` maintains

## Status, lifecycle, cleanup

`vmcav status` answers whether the VM is up, which generation it booted, where
the console is, and whether work is waiting. `main`'s `cav status` is a
different command.

- project and agent selectors, `--all`, an agent shown under each project it
  can reach, unreadable projects reported rather than skipped
- running versus stopped, ahead/behind against the cave remote, unmerged
  commits by subject
- the maintenance it does as a side effect: mark dead agents stopped, archive
  stopped agents after 24 hours, drop already-merged shared dev branches
- `remove-agent` and `gc`

The registry already has id reclamation with the same 24 hour window, so this
is the surface around it that is missing, not the bookkeeping underneath.

## The cave environment

Phase 9. There is no `cave.nix` path in the VM at all: what the guest runs is
cav's own system config, and a user's environment has nowhere to arrive.

- `cave.nix` plus its siblings synced host to guest, and the hash-based skip
  that pushes only when it changed
- `env build`, `env update [input]`, `env metadata`
- the per-cave profile linked into the cave home, built by the guest daemon
- the home-manager layer, and the default environment generated for a user
  who has no `cave.nix`
- `cav init` offering to create one

`vm-cave-design.md` settles the direction (the build happens in the guest,
`env pull` brings an agent's changes back out), so this is unbuilt rather than
undecided.

## Sessions, memory, the system prompt

Transcripts largely landed with `internal/sessions`: the host archive under
the data dir, `cave-vm sessions sync|restore|share|list`, checkpoints at stop,
detach and reformat, recovery straight out of a stopped disk, and the shared
read-only view that lets an agent read another's session. What is left:

- no `--continue` and no `--resume <agent-id/session-id>` on the host side.
  Resume is chosen inside the cave by claude's own picker, against whatever
  `restore` put there, so the host cannot name a session to start
- nothing syncs for an agent that exits with nobody attached
- nothing carries `~/.claude/` memory into or out of the guest, and nothing
  shares memory between agents on a project
- no baseline system prompt rebuilt per launch from the flags actually in
  effect and the projects mounted

`vm-cave-todo.md` holds what is still undecided, and the durability question
that remains is uncommitted work, not the logs.

## No counterpart at all

- `cav cp`: the transport is already there and the ergonomics are not. `scp`
  over the ssh config's vsock ProxyCommand copies both directions and
  recursively, verified against the running guest, so what is missing is the
  addressing (`cave:<path>` against the agent's worktree, `~/` for its home,
  `@<n>` when there is more than one agent), the overwrite prompts and the
  completion. The refusal to copy symlinks out does not carry over: it guards
  against a symlink naming a host path through a shared namespace, and a guest
  symlink can name only guest paths (checked: one pointing at `/etc/shadow`
  yields the guest's).
- `cav completion bash|zsh|fish`, and the dynamic callbacks it depends on
- inter-agent comms: `--comms`, the `send_message` tool, channel events, the
  broker and channel server behind them
- nested caves: a shared remote per level, `host-cave` up-link and `cave`
  down-link, approval one level at a time
- `--port`, to expose a host TCP port inside the cave
- `--dev` and `--gpu`, to bind host device nodes in. Both of these are
  shaped by the host boundary being a VM with no NIC, so neither ports as
  written.
- `cav shell` semantics: on `main` it enters a running agent's sandbox at its
  worktree; `vmcav shell` is a root shell in the guest, which is a different
  thing wearing the same name
- the statusline, the `-v` echo of git commands, and the rest of `main`'s
  output conventions

## One binary

`vmcav` is a Linux-only shell script and still owns everything above the VMM:
seed staging, the git daemon, the gpg broker, `agent`, `shell`, `image
create|reformat`, and the prose of `status`. Parity means that surface is
`cav`, or a Go binary `cav` delegates to.

This is also what blocks the Mac host: `vm-cave-cross-platform.md` enumerates
the seven places the script breaks there, so nothing on this branch can be run
end to end on macOS today.
