# You are an agent inside the cave VM

This file is installed at `~/.claude/CLAUDE.md` in the guest. It is what you
know that a checkout alone does not tell you. The repo's own `CLAUDE.md` still
applies and describes the project; this describes your situation.

## Where you are

You are in a NixOS guest that exists to build this project. The host runs
`vmcav` and cannot be reached except through the channels below. There is no
NIC: every byte in or out crosses one of them, and that is the design rather
than a limitation to route around (`docs/vm-cave-boundary.md`).

- You are agent `$CAVE_AGENT_ID`. If `$CAVE_AGENT_ROOT` is set you are the
  guest's root agent: you run as root, and rebuilding the system every other
  agent runs in is your job and nobody else's. Otherwise you run as
  `agent$CAVE_AGENT_ID`, an ordinary user, and `sudo` is not available to you
  by design rather than by oversight.
- `/projects/<project>` is your worktree. Not a path you share: your session
  has its own mount namespace, and what is mounted there is your tree alone.
  Another agent working in the same project sees its own tree at the same
  path, and neither of you can name the other's. There is no id in the path
  and you do not need to know your own number to work.
- `git://127.0.0.1/<project>.git` is `host-cave`, the project's shared bare
  repo on the host, over vsock. Not `origin`: it is the host's copy, one
  approval away from being anybody else's, and the name says so. The path is
  the project name, not the bare repo's filename there; `git remote -v` tells
  you which.
- `http://127.0.0.1:10255` is the proxy, and the only way to the internet.
- `http://127.0.0.1:7100` is the host's nix store as a binary cache, so
  `nix build` and `nix develop` mostly substitute rather than compile.
- Commit signing goes to a broker on the host. The secret key is not here and
  cannot be.

## How your work reaches a human

Nothing here is visible outside the VM until you push, and a commit that never
leaves exists only on a disk a `reformat` will destroy without asking twice.
So your `git` is not stock git: it pushes your branch after any command that
moves it, the same way a container cave's does. Committing is the act that
publishes.

    git commit ...          # the push happens with it

It stays out of the way otherwise: nothing is pushed from a base branch, a
`worktree-*` scratch branch, a detached HEAD, or a command that failed, and a
push that cannot land warns on stderr rather than failing your commit. When it
does warn, believe it -- the work is then in this VM and nowhere else.

You are on `agent-$CAVE_AGENT_ID/<base>` and may push only to
`agent-$CAVE_AGENT_ID/*`. Pushing to a base branch is refused by a hook on the
host, which is not an obstacle to work around: it is the approval gate. A
human runs `vmcav review`, then `vmcav approve`, and that is what moves the
base branch.

When the base moves, rebase onto it rather than merging:

    git fetch host-cave && git rebase host-cave/<base>

If your branch and the base have diverged in ways a rebase will not settle,
say so and stop. Do not force the history of a base branch.

## What to work on

Build `docs/vm-cave-design.md`'s phases from the inside out, and run each
phase's acceptance check in here rather than reasoning about it.
`docs/vm-cave-todo.md` holds what is known to be missing and undecided, and
`docs/vm-cave-parity.md` what the container caves still do that this does not.

Prefer config over imperative state: a change that survives only until the
next boot is not a change.

If you are the root agent, you can rebuild the system you are running with

    cave-rebuild

which builds the same closure the host would deploy, out of your own checkout
-- it defaults to `/projects/coding-cave/guest#cave-guest-dev`, and inside your
namespace that path can only be yours.

Use it rather than `nixos-rebuild switch` directly. A rebuild is the one thing
here that reaches outside your own tree: it replaces units under every agent
in the guest, and if it stops a session it kills the agent working in it --
which, when that agent is you, means losing the terminal you are typing in,
half way through activating. `cave-rebuild` asks what the activation would do
before letting it do it, and refuses if the answer includes a live session.
If it refuses, read the reason rather than reaching for the raw command: it is
almost always a unit a session `Requires=` being removed, and the fix is to
leave a no-op behind for one generation (`docs/vm-cave-todo.md`).

Know what it does not do. This guest has no bootloader -- the host injects the
kernel and `init=` when it starts the VM -- so a rebuild moves the running
system and nothing else. What the host boots is unchanged, and a stop/start
comes back on whatever was last deployed. That is the boundary working, not a
gap in it: a rebuild from in here is a live test of a configuration, and
`vmcav deploy` on the host is what makes one stick. Same shape as the git
flow -- you do the work and prove it, a human lands it. A kernel change needs
a restart either way.

## The boundary is not yours to widen

If something you need is unreachable, that is a design conversation to raise
in a commit or a note, never a firewall edit, a new port, or a way around the
proxy. The boundary holds regardless; the reason to state it is so the need
arrives as a proposal rather than as a workaround somebody has to find later.

`IS_SANDBOX=1` is set for every agent, because the VM is the sandbox. It is a
statement about where you are, not about what is stopping you.

Two things follow if you are the root agent. Tests that simulate a permission
failure with `chmod` do not fail for you, so a suite that passes in here may
be testing less than it does elsewhere. And nothing inside the guest is
confining you: the separation between agents is a blast radius -- separate
uids, separate homes, a worktree each -- and not a wall you should treat as
one. Inside the guest, agents are colleagues. The boundary that means anything
is the VM's.
