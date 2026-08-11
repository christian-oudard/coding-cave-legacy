# vm-cave: open work

Things known to be missing or wrong, kept out of `vm-cave-design.md` because
they are not phases of the build-out: they are gaps the bootstrap has
surfaced. Each says what is broken and what is undecided, not what to do.

## Preserve uncommitted work across `reformat`

`vmcav reformat` destroys the guest disk, which is the only copy of anything
the agent has not pushed. Committed work survives because pushing to
`agent-<id>/<base>` puts it in the host repo; a dirty worktree does not.

The agent is told to commit early, but "the tool eats your work if you
forget" is not a workable contract, and reformat is the one command a
broken bootstrap forces you to run.

Open: what to preserve and where. A pre-reformat WIP commit on the agent's
own branch is the cheapest, and pollutes the branch. A bundle or patch
series parked in the host state dir keeps the branch clean, and invents a
second place work can live. Also unresolved whether this belongs to
reformat at all, or is a guest-side habit (autocommit unit, periodic push).

## Session management

The agent's claude session lives in a tmux session on the guest disk, so it
dies with a reformat and cannot be resumed from the host. `vm-root-agent.md`
already needs a resume marker for the reboot-to-test-a-generation loop,
which is the same problem seen from a different side.

Open: whether sessions are host-visible at all (the container cave tracks
them in the registry: session id per agent, `cav status`, resume by id), and
whether more than one agent per VM is a thing, which decides whether "the
agent" stays a singular tmux session named `agent`.

## Agent hibernation

A VM holds many agents, and it restarts: a host-side deploy that changes the
kernel, a stop at the end of a day, a crash. Every agent dies with it. There
is no way to quiesce them first, and no way to bring them back doing what
they were doing.

The present code assumes the opposite of all of this. One tmux session named
`agent`, one systemd unit, and `vmcav agent` attaches to "the" agent.

What hibernation has to cover is at least: quiescing an agent at a point
where its work is recoverable (which overlaps the uncommitted-work entry
above), recording enough to resume the claude session rather than start a
fresh one, and restoring cwd, branch and whatever the agent had in flight.

Open, and genuinely undecided:

- The level it happens at. Session-level (each agent's claude session id
  recorded and resumed with `--resume`, which is what the container cave
  does through the registry) or machine-level (Cloud Hypervisor can pause,
  snapshot and restore a VM's memory, so every agent comes back mid-thought
  with no per-agent machinery at all). The second is tempting and couples
  restore to CHV's snapshot format and to the disk being untouched.
- Who holds the roster of live agents: the host registry, as with container
  caves, or the guest, with the host asking.
- What a redeploy does to hibernated agents. An in-guest `nixos-rebuild
  switch` restarts units under a live agent, and a kernel change means a
  reboot; whether hibernation is what makes those safe, or is orthogonal.
- What survives a reformat. Today, nothing: the sessions live on the disk
  that reformat destroys.

## Memory management

Nothing carries `~/.claude/` memory into or out of the guest. The host seeds
`claude.json` only, so an agent starts with no memory of previous sessions
and loses whatever it accumulates at the next reformat.

Open: whether guest memory is seeded from the host's, kept separate per VM,
or committed to the repo like a project's `CLAUDE.md`. Related: the login
lives on the disk too, so a reformat costs a `/login` until crinj mints a
placeholder for the VM up front.

## Multiple agents per VM

A VM is meant to hold many agents. Everything built so far assumes exactly
one, in ways that are cheap to list and not cheap to change:

- one tmux session, literally named `agent`, and one `vm-root-agent.service`
- `vmcav agent` attaches to "the" agent, with no way to name one
- the git daemon injects `CODING_CAVE_AGENT_ID=1` for the whole guest, so
  the pre-receive guard cannot tell co-resident agents apart (the hook says
  as much: it gates by namespace, not by agent)
- one ssh identity and one `/root/coding-cave` working tree, so two agents
  would share a checkout

Open: whether agents are separate unix users or all root with separate
worktrees; where ids are allocated (the host registry, as in container
caves, versus the guest); whether each gets its own vsock port and thus its
own daemon identity, which is what would make the pre-receive guard exact;
and what `vmcav agent` becomes when there are several.

Related: [Agent hibernation](#agent-hibernation) above, and the root-agent
flag already in `cave-vm/internal/registry` that makes root a per-VM agent
property rather than a reserved id.

## Other agent harnesses

Everything is claude-specific, in more places than the name suggests: the
unit runs `claude --dangerously-skip-permissions`, `IS_SANDBOX=1` exists
only to satisfy claude's root guard, the seed carries `claude.json`,
`seed-claude-config` forces claude's trust and onboarding keys, `vmcav
agent` attaches to a tmux session that is assumed to be running claude, and
crinj's OAuth chain is configured for Anthropic's token endpoint.

Codex, antigravity and whatever follows each arrive with their own version
of all of it: an auth story (a different OAuth provider, or a plain API key,
which is `[[host]]` header injection rather than the OAuth chain), their own
config and state files to seed, their own first-run prompts to pre-accept,
and their own idea of a resumable session.

The one piece that generalizes for free is crinj's placeholder mechanism:
it is provider-agnostic, so abbreviated bootstrapping (a VM that comes up
authenticated without a login) is the same trick per provider. What does
not generalize is where each harness keeps its credentials and in what
format.

Open: whether an image ships every harness or one per image; whether an
agent becomes a typed thing (harness plus id) in the registry, which is
also what `vmcav agent` would need to pick between; and whether the
harness-specific seeding is a list of files the harness declares, so adding
one is config rather than code.

## Image management, much later

`vmcav image create` builds one image into one disk at one path, for one VM.
Eventually there are several VMs, several images, and a reason to keep more
than one image around: rolling back a bad guest, running two VMs on
different images, sharing one image between VMs that were created from it.

Open: whether images are named and content-addressed (the built toplevel is
already a hash, so an image could just be its store path plus a disk
created from it) or whether a VM simply remembers what it was built from;
whether disks are copy-on-write from a base image rather than a full copy,
which is what would make several VMs per image cheap; and where the
registry of both lives.

## Deployment, once the single-root-agent milestone lands

Settled, so that it is not re-derived:

- The guest config in `guest/` is the *default* the package ships, not a
  live config. Editing it changes what the next built package installs and
  nothing that is running.
- Outside updates go over ssh into a running VM, the way NixOS already does
  this: build on the host, `nix copy` the closure in, activate there
  (`nixos-rebuild --target-host`, transport being the vsock ProxyCommand).
  The host keeps the kernel and initrd it built as GC roots, so it can boot
  what it deployed.
- A stopped VM cannot be updated. Accepted restriction: start it and deploy,
  or `reformat` to the shipped default.
- Inside updates are just the agent running `nixos-rebuild switch` as root.
- The repo therefore stops being load-bearing: it is the project the agent
  works on, not where the guest config comes from. `CAVE_GUEST_REF`, the
  hardcoded `agent-1`, and the spec-commit state all go away with it.

Left over once that lands: nothing on the host consumes what an agent
pushes, so there is no `cav approve` equivalent and agent commits just
accumulate on their branch.

Open: whether the spec ref means "the agent's branch" (agent deploys without
approval, human is the outsider) or "what the VM runs" (both move it, the
human's deploy is a first-class command). That choice decides the other two.
