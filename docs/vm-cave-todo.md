# vm-cave: open work

Things known to be missing or wrong, kept out of `vm-cave-design.md` because
they are not phases of the build-out: they are gaps the bootstrap has
surfaced. Each says what is broken and what is undecided, not what to do.

## `clone-repo` fails on a freshly created image

Seen on a from-scratch `image create` into a never-booted disk, and
unexplained. The guest boots, reaches multi-user and answers ssh,
`seed-claude-config` finishes, and then `clone-repo` fails. `vm-root-agent`
requires it, so the agent never starts and the VM comes up with nothing
running in it.

    Starting Clone the coding-cave repo for the agent...
    [FAILED] Failed to start Clone the coding-cave repo for the agent.
    [DEPEND] Dependency failed for Bootstrap vm-root-agent

The host side looks right at the point of failure: `vmcav` created the shared
repo, started the git daemon on 9418, and brought up the 7300 bridge. crinj
was unreachable in that run, which `clone-repo` does not need.

The cause is still unread, not unreachable: the console only carries the
`[FAILED]` line, because the unit's own output goes to the journal. A `vmcav
start` leaves the VM up, so `vmcav shell journalctl -u clone-repo` should say
it outright. That is the next step and it has not been taken.

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

The agent's claude session lives in the pty cave-guestd holds and in
`/root/.claude` on the guest disk, so it dies with a reformat and cannot be
resumed from the host. `vm-root-agent.md` already needs a resume marker for
the reboot-to-test-a-generation loop, which is the same problem seen from a
different side.

Open: whether sessions are host-visible at all (the container cave tracks
them in the registry: session id per agent, `cav status`, resume by id), and
whether more than one agent per VM is a thing, which decides whether "the
agent" stays the single session the daemon names `1`.

## Agent hibernation

A VM holds many agents, and it restarts: a host-side deploy that changes the
kernel, a stop at the end of a day, a crash. Every agent dies with it. There
is no way to quiesce them first, and no way to bring them back doing what
they were doing.

The present code assumes the opposite of all of this. One session named `1`,
one systemd unit, and `vmcav agent` attaches to "the" agent.

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
or committed to the repo like a project's `CLAUDE.md`. The login has the same
shape of problem and is already solved the crude way: reformat copies it out
of the guest first and seeds it back, which memory could do too.

## Multiple agents per VM

A VM is meant to hold many agents. Everything built so far assumes exactly
one, in ways that are cheap to list and not cheap to change:

- one `vm-root-agent.service`, starting one session the daemon names `1`.
  cave-guestd already addresses agents by name and can hold several, so this
  is the unit and its launcher, not the daemon
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
`seed-claude-config` forces claude's trust and onboarding keys, the guest's
managed settings pre-accept claude's bypass-permissions dialog, and crinj's
OAuth chain is configured for Anthropic's token endpoint.

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
  this: build on the host, `nix copy` the closure in, activate there,
  transport being the vsock ProxyCommand. Built: `vmcav deploy`. The closure
  goes in before the host's out-links move, so a failed activation still
  leaves a VM that boots what was deployed. A kernel change needs a restart
  and says so.
- What a VM boots is read off its own disk at `start`: the system profile the
  guest activated names the toplevel, and that names the kernel and initrd.
  So an in-guest `nixos-rebuild switch` survives a restart with no host-side
  sync, and a disk boots on a host whose store never had the closure (a gc, a
  fresh machine), because the kernel and initrd can be pulled back out of the
  disk. The host's out-links stay as gc roots, not as the source of truth.
- A stopped VM cannot be updated. Accepted restriction: start it and deploy,
  or `reformat` to the shipped default.
- Inside updates are just the agent running `nixos-rebuild switch` as root.
- The repo therefore stops being load-bearing: it is the project the agent
  works on, not where the guest config comes from. `CAVE_GUEST_REF`, the
  hardcoded `agent-1`, and the spec-commit state all go away with it.

Done: the host consumes what an agent pushes. `vmcav review`, `vmcav diff`
and `vmcav approve` are the last hop, and `vmcav status` says when work is
waiting. Fast-forward only, so a base that has moved on sends the agent back
to rebase rather than growing a merge nobody tested.

Open: whether the spec ref means "the agent's branch" (agent deploys without
approval, human is the outsider) or "what the VM runs" (both move it, the
human's deploy is a first-class command). That choice decides the other two.

## Nothing runs the Mac host end to end

The hardware is no longer the blocker. The Apple silicon host that
`vm-cave-cross-platform.md` took its evidence from is provisioned and
reachable over ssh, with vfkit v0.6.4 installed and working and 170 GB free.
What is on it is the residue of that validation run: the guest artifacts it
booted (`Image`, `initrd`, `nixos.img`, `seed.img`), a hand-written
`boot.sh`, and a `cave-vm` binary. The Lima VM's disk is still there but
`limactl` is not, and the coding-cave checkout and crinj were deleted once
the findings were written up.

So every macOS result on record was produced by hand, and nothing on that
machine can be re-run from this repo. `vmcav` is the reason: it is a Linux
shell script that breaks in the seven places
`vm-cave-cross-platform.md` enumerates (no `/proc`, no `setsid`, BSD
`readlink`, no `socat`/`mke2fs`/`gpg`, `x86_64-linux` hardcoded), so "run the
tool on the Mac" is not a thing that can be attempted today, only
approximated by driving vfkit directly again.

Open: whether to keep re-validating by hand against the current guest, which
is cheap and proves only what is already documented, or to pull the VM
lifecycle into `cave-vm` now, which is what the cross-platform doc concludes
and what would make the Mac a first-class host rather than a demo. The second
is real scope and lands ahead of where `vm-cave-design.md` currently puts it.
Also unresolved: the aarch64 guest needs a Linux builder, so re-provisioning
Lima is a prerequisite either way.

## Where the effort should go, given the field

A survey of the agent-sandbox landscape (recorded in `agent-sandbox-field.md`)
changes the priority order, without changing the plan.

Docker Sandboxes and the microVM crowd have commoditised the isolation
boundary, which is most of phases 0-3 and 10. Three things nobody else does,
and which no competitor's product can be configured into:

- **A nix store shared across agents.** Nothing in the survey uses nix at all;
  every one is OCI images or a stock distro. Note this is also unsolved on
  `main` today, where store dedup was removed and each cave has a private
  store. One guest with a stock multi-user daemon is the answer `main` gave
  up on, and `narcache` plus `pkgdd` have no equivalent anywhere.
- **Git as the coordination mechanism with a host-side gate.** Agent
  branches, `pre-receive`, `cav approve`. Docker's `--clone` is fetch-based
  with no gate; Dagger's `container-use` is the nearest and is containers
  only.
- **Inter-agent comms.** Docker forbids it outright between sandboxes.

The reframe worth holding onto: the VM has been treated as an isolation
upgrade, but on Linux bwrap already gives caves. What the VM actually buys is
**macOS support and the one-store model**, neither of which any product on
the market can supply. Out-hardening Docker is not the point and not
achievable on their budget.

So: keep the VMM layer boring and small (two process-backends, skip Windows),
stop treating cold boot as a headline metric since one VM per host boot means
never competing on it, and prioritize phase 4 (narcache) and phase 8 (pkgdd)
over 1-3. Phase 4's acceptance check, building a host-present package in the
guest with zero internet bytes, is the demo no competitor can reproduce. Land
the git bridge (phase 7) early, since the approval gate is the thesis.

The strongest argument against all of this, worth answering rather than
ignoring: make cave sandbox-agnostic instead, keep bwrap on Linux, use `sbx`
or Apple's `container` on macOS, and spend everything on coordination, comms
and nix. Far less code. It fails on exactly one point, that the shared store
needs one VM, so it would mean accepting per-agent stores on macOS and shared
on Linux.
