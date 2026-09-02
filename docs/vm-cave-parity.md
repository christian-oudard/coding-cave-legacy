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

Design phase 6. Two pieces of what a cave is now exist without one: an
ordinary agent runs as its own uid with its own home, and its session has a
private mount namespace binding its worktree over `/projects/<slug>`. That is
the identity and the filesystem view; what a cave adds on top is the sandbox
-- bwrap and seccomp -- and nothing of that is built.

- ~~one guest uid per agent, private home~~ **done**; home is seeded rather
  than materialized one-way from a seed tree, which is the remaining gap
- ~~the agent's worktrees at `/projects/<slug>`~~ **done**, by a systemd
  `BindPaths=` in the session unit rather than by bwrap
- bwrap profile: ro /nix/store, the nix-daemon socket, shared caches
- the seccomp policy, lifted from cav-rs's sandbox config rather than
  re-derived. How that config crosses the cleanroom line is still open in
  `vm-cave-design.md`.

## Projects

`vmcav init [dir]` registers a project, `vmcav projects` lists what the guest
can reach, and the git daemon serves every registered one. A project is a
checkout with a shared bare repo inside it, reached by slug -- the guest still
clones `git://127.0.0.1/<slug>.git` and nothing about that changed.

The registry is the served directory itself: `<state>/repos/<slug>.git` is a
symlink into that project's own shared repo, so registering is creating a link
and listing is reading them back. There is no second list to fall out of step
with the filesystem, which is the failure a registry file invites. The project
`vmcav` was run from is registered on the way past, because it is the one that
can be inferred; any other has to be named.

- ~~`cav init`, the registry, slugs from directory names~~ **done**
- `-p/--project`: more than one project per agent, one dev branch and one
  worktree each. An agent is still given exactly one project when it is
  created, and the guest binds exactly that one over `/projects`.
- ~~`remove-project`~~ **done** as `vmcav remove-project <slug>`: it deletes
  the link and nothing else, so the project's own shared repo and every agent
  branch in it survive -- removing a project must not be a way to lose work.
  Refused while an agent still holds a worktree of it, since unregistering one
  out from under an agent leaves a checkout it can no longer clone or fetch.
  What decides whether such an agent is safe to remove is not re-answered
  there: `vmcav agent remove` already asks it, unlanded work and all.
- `rename-project`, and the re-registration of a project directory that moved
  on disk.
- a project added while the VM is up is served only after `vmcav stop &&
  vmcav start`, since restarting the daemon under a live guest would drop
  whatever push is in flight

## Multiple agents in one VM

Sessions are already one process and one unit each, which is the durability
half. The identity half now exists in the guest: an agent is a uid, a home and
a view of `/projects` of its own. What is still missing is identity as the
*host* sees it -- every agent's git push arrives on one vsock port with one
stamped id, so `pre-receive` cannot tell co-resident agents apart. It gates by
namespace rather than by agent, which is why that is survivable;
`vm-cave-todo.md` holds the demux design and why a port per agent cannot work.

- the git daemon injects one `CODING_CAVE_AGENT_ID` for the whole guest, so
  the pre-receive guard cannot tell co-resident agents apart. It no longer
  pretends to: requiring the branch to match the stamped id rejected every
  agent but one and stranded their work in the VM, since committing is what
  publishes. The host now checks what it can know -- a well-formed agent
  namespace, never a base branch -- and the guest's cave-git refuses to push
  outside the running agent's own namespace, which stops a mistake rather than
  an attacker. Making it hold against intent needs cave-guestd to stamp each
  connection from the peer's uid; agents are separate uids now, so the kernel
  already knows the answer.
- one ssh identity and one `/projects/coding-cave` checkout, so two agents would
  share a working tree
- `vmcav agent` attaches to "the" agent with no way to name one, though
  `cave-guestd attach <id>` underneath it already takes an id

## Git flow

Phase 7, about half landed. Present: the vsock git bridge on 7300, the
pre-receive and post-receive hooks, the guest's auto-pushing git, and
`review`/`approve` as fast-forward of a whole agent branch onto its base.

The round trip is verified end to end rather than assumed. From inside the
guest a bare `git push` lands on `agent-1/vm-cave` (the guest's `/etc/gitconfig`
sets `push.default = current` and `autoSetupRemote`), `vmcav review` shows the
commit on the host, and a force-push to reset the agent branch is accepted
because the agent itself pushed that history. The guard holds in both
directions it is meant to: a push to the base branch and a push to another
agent's namespace are both refused by `pre-receive`, with the agent id coming
from the daemon rather than the client.

Three things about the shape of that flow now match `main` rather than
differing from it, all three found by running it for real rather than by
reading it:

- The guest's `git` is a wrapper that pushes what it just committed
  (`guest/cave-git.sh`, `main`'s `cav-rs/src/bin/git_wrapper.rs`). Before it,
  a commit the agent forgot to push was one `reformat` from never having
  existed, and "commit early" was a contract kept by remembering. Verified in
  the guest after `nixos-rebuild switch`, not on the host alone: this
  paragraph's own commit was made with no `git push` after it, and arrived on
  `agent-1/vm-cave` in the shared repo.
- `review` checks the agent's branch out in the human's checkout, so reviewing
  is reading the code with the tools you already have. `vmcav diff` existed to
  substitute for that and is gone; `main` never had one.
- `approve` lands the work in the human's checkout as well as in the shared
  repo, and leaves you standing on the base. It used to move a ref inside the
  bare repo alone, which meant a successful approve followed by a `git log`
  that did not have the commit -- the first real run of the flow ended exactly
  there.

So the minimum for a productive single agent is present. What is missing
below is ergonomics, and each item has a plain-git equivalent a human can run.

- `pull` and `pull --force`: move an agent's work into the host's view of it
- `push`: half of it exists. `vmcav push [branch] [--force]` sends the
  checkout's branch to the shared repo the agent clones from, and `start` does
  it for the current base branch, which is what keeps an agent working against
  current code. What is missing is `main`'s other half: a push to an *agent*
  branch there updates that agent's workspace, where here it would only move a
  ref the agent has yet to fetch
- `checkout`: retarget an agent to a different base and rebase its unmerged
  work onto it
- `reject`: roll a base branch back and delete the approval record, with the
  divergence check that blocks it
- ~~`approve --commit <sha>`: partial approval, dev branch preserved with the
  remainder~~ **done**. The commit has to be one of the pending ones: anything
  already on the base would move nothing, and anything off the branch would
  land work that was never reviewed. The branch survives a partial landing,
  because it is then exactly the work that was held back.
- automatic rebase of every agent branch when a base branch moves
- the host-side checkout of an agent branch that `pull` maintains as its own
  thing. `review` checks one out in the checkout you are standing in, which
  covers reading an agent's work; what is absent is a tree the host keeps
  current without being asked

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
- ~~`remove-agent`~~ **done** as `vmcav agent remove <id>`: it refuses a running
  agent, and refuses one holding commits its base does not have, naming them
  rather than counting them. `vmcav gc` sweeps the ones nobody removed by hand:
  stopped, not the root agent, and untouched for over 24h, each still put
  through the unlanded-work check so a sweep can only do what naming them one
  at a time would have.

The registry already has id reclamation with the same 24 hour window, so this
is the surface around it that is missing, not the bookkeeping underneath.

## The cave environment

Phase 9, guest half landed. `guest/env` holds the home-manager layer --
`mkCaveEnv` plus the `cave-system` and `claude-code` modules, ported from
`main` -- and `cave-guestd env` builds it and copies it into an agent's home
from `seed-agent-home@`. A guest with no `cave.nix` still gets the cave
defaults; one with a `/var/lib/coding-cave/env/cave.nix` gets that merged over
them. `cave-env build|edit|show` is the in-guest command.

Three things differ from `main` deliberately, each because the guest is a
machine rather than a mount namespace:

- **The toolbox moved to `environment.systemPackages`.** `main` carries
  coreutils, jq, ripgrep and the rest in its `cave-system` module because cav
  cannot configure a system it does not own. The guest owns its system, so
  every agent has the floor whether or not an environment was ever built.
- **One environment, many homes.** `main` builds one per cave; there is one
  spec per guest here, built once and materialized into each home. The cost is
  that nothing in a `cave.nix` may interpolate `home.homeDirectory`.
- **Removals are honoured.** A manifest per home records what was written, so
  deleting a skill from `cave.nix` takes it out of the homes that had it.
  `main` leaves it behind, which it gets away with because a cave home is
  discarded and a guest home survives every `reformat`.

What is left is the host half, and it is all of the crossing:

- `cave.nix` plus its siblings synced host to guest, and the hash-based skip
  that pushes only when it changed. The guest-side stamp exists and is what
  the sync would drive; nothing carries the files in yet. The design's answer
  is a control-plane endpoint, which is the plane that already exists.
- `cav env build`, `env update [input]`, `env metadata`, and `env pull` to
  bring an agent's edits back out to the host's `cave.nix`
- the guest keeping its copy in a git repository of its own, so "has the guest
  changed" is a question git answers
- `cav init` offering to create one

Also missing, and not host-side: a `path:` flake input in a `cave.nix` cannot
resolve, because there is no host filesystem in the guest. `main` supports
pinning a local dotfiles directory that way and re-pins it on every build.
Undecided whether those contents ride in the synced set or whether `path:`
becomes unsupported with git URLs as the answer.

The marketplace registration and the `cave-messages` plugin that `main`'s
`cave-system` module enables are not here, because the comms plane is not.

## Sessions, memory, the system prompt

Transcripts largely landed with `internal/sessions`: the host archive under
the data dir, `cave-vm sessions sync|restore|share|list`, checkpoints at stop,
detach and reformat, recovery straight out of a stopped disk, and the shared
read-only view that lets an agent read another's session. What is left:

- ~~no `--continue`~~ **partly done in the guest**: an agent restarted with
  `vmcav agent` continues its own last conversation, because its worktree path
  is stable and claude files transcripts by directory. What is still absent is
  the host naming one: no `--resume <agent-id/session-id>`, so a particular
  session is still chosen inside the cave by claude's own picker.
- a restore lands in the root agent's home whichever agent it is meant for.
  Reading is per agent now -- a record belongs to the home it was found in --
  but a restore is addressed to the guest rather than to an agent in it, so
  the request has no id to aim at.
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
