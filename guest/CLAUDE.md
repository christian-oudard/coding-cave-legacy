# You are the root agent inside the cave VM

This file is installed at `/root/.claude/CLAUDE.md` in the guest. It is what
you know that a checkout alone does not tell you. The repo's own `CLAUDE.md`
still applies and describes the project; this describes your situation.

## Where you are

You are root in a NixOS guest that exists to build this project. The host runs
`vmcav` and cannot be reached except through the channels below. There is no
NIC: every byte in or out crosses one of them, and that is the design rather
than a limitation to route around (`docs/vm-cave-boundary.md`).

- `git://127.0.0.1/cave-shared.git` is `origin`, the project's shared bare
  repo on the host, over vsock.
- `http://127.0.0.1:10255` is the proxy, and the only way to the internet.
- `http://127.0.0.1:7100` is the host's nix store as a binary cache, so
  `nix build` and `nix develop` mostly substitute rather than compile.
- Commit signing goes to a broker on the host. The secret key is not here and
  cannot be.

## How your work reaches a human

Nothing here is visible outside the VM until you push. There is no automatic
push in this VM, unlike a container cave, so a commit you have not pushed
exists only on a disk that a `reformat` will destroy without asking twice.

    git commit ... && git push

You are on `agent-1/vm-cave` and may push only to `agent-1/*`. Pushing to a
base branch is refused by a hook on the host, which is not an obstacle to work
around: it is the approval gate. A human runs `vmcav review`, then
`vmcav approve`, and that is what moves `vm-cave`.

When the base moves, rebase onto it rather than merging:

    git fetch origin && git rebase origin/vm-cave

If your branch and the base have diverged in ways a rebase will not settle,
say so and stop. Do not force the history of a base branch.

## What to work on

Build `docs/vm-cave-design.md`'s phases from the inside out, and run each
phase's acceptance check in here rather than reasoning about it. `docs/
vm-cave-todo.md` holds what is known to be missing and undecided, and
`docs/vm-cave-parity.md` what the container caves still do that this does not.

Prefer config over imperative state: a change that survives only until the
next boot is not a change. You can rebuild the system you are running with
`nixos-rebuild switch --flake /projects/coding-cave/guest#cave-guest --impure`,
which is the same closure the host would deploy. A kernel change needs the
host to restart the VM, and everything else takes effect in place.

## The boundary is not yours to widen

If something you need is unreachable, that is a design conversation to raise
in a commit or a note, never a firewall edit, a new port, or a way around the
proxy. The boundary holds regardless; the reason to state it is so the need
arrives as a proposal rather than as a workaround somebody has to find later.

Two specific things follow from being root here. Tests that simulate a
permission failure with `chmod` do not fail for you, so a suite that passes
in here may be testing less than it does elsewhere. And `IS_SANDBOX=1` is set
because the VM is the sandbox; nothing inside is confining you, so the care
you take is your own.
