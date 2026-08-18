# vm-cave: open work

Things known to be missing or wrong, kept out of `vm-cave-design.md` because
they are not phases of the build-out: they are gaps the bootstrap has
surfaced. Each says what is broken and what is undecided, not what to do.

## The guest clock is never corrected

First in this file because it is low in the stack and everything above it
fails confusingly. The guest has no timesyncd, no NTP, no
`qemu-guest-agent` and no NIC to reach a time server with. It takes the
clock from the host at boot and nothing touches it afterwards.

On x86 under KVM this mostly hides: `kvm-clock` tracks host adjustments
across a suspend. Under HVF on aarch64 the guest is on `arch_sys_counter`,
which does not, so a closed lid leaves the guest behind by however long the
host slept. A stale clock fails TLS certificate validation, which fails
every agent request, and nothing in that failure points at the clock. ssh is
unaffected, since host keys carry no validity dates, so the channel that
could correct it keeps working while everything else is dead.

vfkit ships `--timesync` for exactly this and it is not usable here: it
speaks to `qemu-guest-agent` over vsock, which is a whole guest agent shipped
for one syscall. The host already talks to the guest, and whatever it says
can carry the host's UTC time.

Separately, and not the same problem: the timezone is a host information
leak. `main` pins the cave's `/etc/localtime` to UTC so the host's geographic
region stays outside the sandbox, with `coding-cave.timeZone` and a host `$TZ`
as the only levers. The guest owns its own `/etc/localtime`, so the mechanism
differs and the contract must not.

Open: what triggers the correction. Every host-to-guest call is the cheapest,
and bounds staleness by "when you last asked", which is the rule the session
sync already uses. A guest-side timer needs a clock it has no reason to
trust. A resume hook needs the host to notice its own suspend.

## `clone-repo` failed once and has not reproduced

Seen once, on a from-scratch `image create` into a never-booted disk: the
guest reached multi-user and answered ssh, `seed-claude-config` finished, and
then `clone-repo` failed, taking `vm-root-agent` with it, since it requires
it. The VM came up with nothing running in it.

    Starting Clone the coding-cave repo for the agent...
    [FAILED] Failed to start Clone the coding-cave repo for the agent.
    [DEPEND] Dependency failed for Bootstrap vm-root-agent

A later `image create` plus `start`, on the same host and the same code path,
succeeded: `clone-repo` exited 0 and the agent's checkout landed on
`agent-1/vm-cave`. So this is intermittent, not a plain break, and the run
that failed took its journal with it.

What makes it worth keeping rather than forgetting: `clone-repo` is a
`oneshot` with no retry, and what it depends on is not in the guest. It
clones through the vsock bridge to a git daemon on the host, so anything
transient on the far side of that boundary at the wrong moment is
unrecoverable and stops the agent from existing at all. Whether that is what
happened is unestablished; the ordering (`after`/`wants` on
`vsock-git.service`) only waits for the guest's own socat, which is up long
before the host is necessarily answering.

Open: whether to make the unit survive a transient failure (a retry, or a
`Restart` with a bounded limit) or to leave it failing loudly on the argument
that a guest which cannot reach the host has nothing useful to do anyway.

## Preserve uncommitted work across `reformat`

`vmcav image reformat` destroys the guest disk, which is the only copy of anything
the agent has not pushed. Committed work survives because pushing to
`agent-<id>/<base>` puts it in the host repo; a dirty worktree does not.

The agent is told to commit early, but "the tool eats your work if you
forget" is not a workable contract, and reformat is the one command a
broken bootstrap forces you to run.

The stance this sits under: the VM is cattle. Anything worth keeping leaves
by a git commit, which is also the only way it passes the approval gate, so
there is no second escape route to build and no state worth preserving for
its own sake. That makes this entry narrow rather than large: the question is
only what to do about work an agent has written and not yet committed.

Still open, and now the only thing in this file that is. Session state moved
to the host (`vm-cave-design.md`), but a worktree cannot follow it: it is a
whole mutable checkout, not a file that only grows and copies out cleanly.
So a dirty worktree is still the one thing a reformat destroys.

Open: what to preserve and where. A pre-reformat WIP commit on the agent's
own branch is the cheapest, and pollutes the branch. A bundle or patch series
parked in the host state dir keeps the branch clean, and invents a second
place work can live. Also unresolved whether this belongs to reformat at all,
or is a guest-side habit (autocommit unit, periodic push).

## Session management

Mostly built, and the design is in `vm-cave-design.md`. Records are host
state, under `~/.local/share/coding-cave/sessions/<uuid>.jsonl` with a
`key=value` sidecar for agent and project, copied out with the host doing all
the naming, never replaced by anything shorter or with a different prefix.
`cave-vm sessions sync|restore|share|list` on the host, `cave-guestd sessions
list|read|write` in the guest, over ssh for a running VM and over `debugfs`
for a stopped one. Wired into `reformat`, `stop`, and the return from an
attach.

What is left:

- **Nothing syncs for an agent that exits with nobody attached.** The plan is
  not a timer: sync whenever the host talks to the guest about agents at all
  (`status`, an attach, stopping one), which bounds staleness by "when you
  last asked" and needs no clock. Unbuilt.
- **Ownership is bookkeeping, not enforcement.** `sync -agent N` attributes
  records to whoever the caller names, and `vmcav` always says `1`. Every
  agent is root in one home, so any of them can write any transcript. The
  archive does not depend on this (a rewritten record is set aside rather than
  applied), but "its own sessions" means only what the sidecar says until the
  per-agent split lands, which is the multiple-agents entry below.
- **The `last` sighting only moves when bytes move**, so it says when a
  record last grew rather than when it was last seen. Harmless today, wrong
  if anything ever reads it as liveness.
- **Memory is untouched**, deliberately, and needs its own rule: per-project,
  shared between agents, rewritten rather than grown, so the length and prefix
  checks that protect a transcript say nothing useful about it.

## Starting and stopping agents, and knowing what is running

There is no way to start a second agent, stop one, or ask what is running.
`vmcav agent` attaches to "the" agent and `status` reports the VM plus
whatever the guest's socket directory says.

One defect worth recording rather than rediscovering: the host registry
decides liveness with `alive(pid)` against a *host* pid, which is meaningless
for an agent living in the VM. It is the same trap the VM pidfile already
sprang, asking your own namespace about someone else's process and being told
nothing is there. Nothing depends on it yet because there is only ever one
agent, which is why it has not shown up as a bug.

The shape proposed, and not yet agreed, is to split the two registers by what
each actually owns. Liveness belongs to the guest, where systemd owns
`cave-session@<id>` and the socket directory is the roster, so the host asks
rather than tracking. Durable facts belong to the host registry, which
already carries agent id, project, the root-agent flag and session id, and
should allocate ids instead of the hardcoded `1`.

Then `cav agent [id]` ensures the VM, ensures the unit and attaches, `cav
agent stop <id>` stops the unit and syncs that agent's records, and `status`
prints a line per agent from the guest joined to the registry's facts. Build
order, each step visible on its own: `cave-guestd list` reporting state,
start time and project; `status` showing it; start and stop by id; then id
allocation.

## Agent hibernation

A VM holds many agents, and it restarts: a host-side deploy that changes the
kernel, a stop at the end of a day, a crash. Every agent dies with it. There
is no way to quiesce them first, and no way to bring them back doing what
they were doing.

The present code assumes the opposite of all of this: `vmcav agent` attaches
to "the" agent, and nothing quiesces anything. Sessions surviving their own
restarts is now handled, one process and one unit each, but that is a
different problem from the machine going down under all of them.

What hibernation has to cover is at least: quiescing an agent at a point
where its work is recoverable (which overlaps the uncommitted-work entry
above), recording enough to resume the claude session rather than start a
fresh one, and restoring cwd, branch and whatever the agent had in flight.

Open, and genuinely undecided:

- The level it happens at. Settled: session-level, each agent's claude
  session id recorded and resumed with `--resume`, which is what the
  container cave does through the registry. Machine-level was tempting,
  since Cloud Hypervisor can pause, snapshot and restore a VM's memory and
  every agent would come back mid-thought with no per-agent machinery at
  all. It is out because it does not exist on the other host: vfkit's
  `POST /vm/state` takes `Pause`, `Resume`, `Stop` and `HardStop` and
  nothing else, and although Virtualization.framework has had save and
  restore since macOS 14, and vfkit's own state enum names `Saving` and
  `Restoring`, there is no way to ask for either. Pause and resume are the
  portable pair and belong in the backend interface for maintenance;
  snapshot is a Linux-only convenience that nothing may depend on.
- Who holds the roster of live agents: the host registry, as with container
  caves, or the guest, with the host asking.
- What a redeploy does to hibernated agents. An in-guest `nixos-rebuild
  switch` restarts units under a live agent, and a kernel change means a
  reboot; whether hibernation is what makes those safe, or is orthogonal.
- What survives a reformat. Answered: sessions are host state, so a reformat
  does not reach them at all.

## Memory management

Nothing carries `~/.claude/` memory into or out of the guest. The host seeds
`claude.json` only, so an agent starts with no memory of previous sessions.

Losing it at reformat is answered by memory being host state, alongside the
session logs. What is still open is whether guest memory has anything to do
with the host's own: seeded from it, kept separate per VM, or committed to
the repo like a project's `CLAUDE.md`. The login already works this way, and
is the precedent rather than the exception.

## Multiple agents per VM

A VM is meant to hold many agents. Everything built so far assumes exactly
one, in ways that are cheap to list and not cheap to change:

- `vmcav agent` attaches to "the" agent, with no way to name one, though
  `cave-guestd attach <id>` underneath it takes one and starts the session if
  it is not there, so this is the host CLI rather than the guest
- the git daemon injects `CODING_CAVE_AGENT_ID=1` for the whole guest, so
  the pre-receive guard cannot tell co-resident agents apart (the hook says
  as much: it gates by namespace, not by agent)
- one ssh identity and one `/root/coding-cave` working tree, so two agents
  would share a checkout
- one home, so one `~/.claude/projects`: every agent can read and rewrite
  every other agent's transcript, which is what keeps session ownership at
  the level of bookkeeping
- `/root` is written literally about twenty times across the guest's seeding
  units (gnupg, authorized_keys, gitconfig, `claude.json`, the credentials,
  the checkout) and `HOME` is set to it in the session unit. So moving an
  agent off that home is not one setting: a unit missed keeps seeding `/root`
  while the agent reads somewhere else, and it fails by the agent quietly
  having no credentials rather than by anything erroring

Open: whether agents are separate unix users or all root with separate
worktrees; where ids are allocated (the host registry, as in container
caves, versus the guest); whether each gets its own vsock port and thus its
own daemon identity, which is what would make the pre-receive guard exact;
and what `vmcav agent` becomes when there are several.

The lifecycle half of this is a separate entry above, and can land first: it
makes who is running visible, which is the honest step before enforcing who
owns what.

Related: [Agent hibernation](#agent-hibernation) above, and the root-agent
flag already in `cave-vm/internal/registry` that makes root a per-VM agent
property rather than a reserved id.

## Other agent harnesses

Everything is claude-specific, in more places than the name suggests: the
unit runs `claude --dangerously-skip-permissions`, `IS_SANDBOX=1` exists
only to satisfy claude's root guard, the seed carries `claude.json`,
`seed-claude-config` forces claude's trust and onboarding keys, the guest's
managed settings pre-accept claude's bypass-permissions dialog, `vmcav
agent` attaches to a daemon session that is assumed to be running claude,
and crinj's OAuth chain is configured for Anthropic's token endpoint.

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

**Access to the Mac has since gone away, for the time being.** Nothing below
can be re-checked until it comes back, so treat every macOS claim in this
repo as of its recorded date and do not add more. Work proceeds from the VM
downward on the Linux host, which is unaffected: the VMM split is already
behind one interface, so the vfkit backend keeps building and keeps being
unverified.

The hardware was no longer the blocker. The Apple silicon host that
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

## Rosetta, optional and later

`vfkit --device rosetta` shares Apple's translation runtime into the guest
over virtio-fs; the guest registers it through `binfmt_misc` and then executes
`x86_64` Linux binaries on an `aarch64` guest.
`vm-cave-cross-platform.md` treats it as what would make the two hosts
behaviourally equivalent rather than merely similar.

Deferred, and running the guest natively is the position: an aarch64 guest on
an aarch64 host is the straightforward configuration, and nix content is built
per system and equivalent either way. What Rosetta buys is the bytes nix never
sees, npm optional dependencies shipped as x86 binaries, pip wheels,
Playwright's browser download, `go install` output, container images, plus
`nix.settings.extra-platforms` for the cases where aarch64 cache coverage is
thin enough to mean compiling on four cores.

So the trigger to build it is evidence rather than principle: an agent blocked
on a toolchain that ships x86-only. Two things to establish first, since
neither shows up where it would be noticed: Rosetta's instruction coverage (no
AVX historically, AVX2 in recent macOS) and whether it is installed on the host
at all.

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
  up on, and the nix package cache and `pkgdd` have no equivalent anywhere.
- **Git as the coordination mechanism with a host-side gate.** Agent
  branches, `pre-receive`, `cav approve`. Docker's `--clone` is fetch-based
  with no gate; Dagger's `container-use` is the nearest and is containers
  only.
- **Inter-agent comms.** Docker forbids it outright between sandboxes.

The reframe this section was written around, that on Linux bwrap already gives
caves so the VM buys only macOS support and the one-store model, no longer
holds. copy.fail and dirty-frag settled it: bwrap is not the host boundary
any more, because the host boundary may not be a kernel one. The VM is the
boundary on every platform, and there is no second backend to keep
sandbox-agnostic.

What survives is that out-hardening Docker is not the point and not
achievable on their budget. The store and the coordination gate are still the
things nobody else has.

bwrap does not disappear: inside the guest it is still what separates caves
from each other. That is now a blast-radius choice, where an escape reaches
the other agents in one VM, and no longer the thing standing between an agent
and the host.

So: keep the VMM layer boring and small (two process-backends, skip Windows),
stop treating cold boot as a headline metric since one VM per host boot means
never competing on it, and prioritize phase 4 (the nix package cache) and
phase 8 (pkgdd) over 1-3. Land the git bridge (phase 7) early, since the
approval gate is the thesis.

Phase 4 is done: with crinj absent, a closure the guest did not have arrives
from the host's store over vsock and verifies, and a host-absent path fails
rather than reaching anything. That leaves phase 8 as the remaining half of
the argument, since language package caches are where an agent's bytes
actually go.

Closed: the argument for staying sandbox-agnostic, keeping bwrap on Linux and
using `sbx` or Apple's `container` on macOS. It was far less code and it
always failed on the shared store needing one VM; it now also fails on the
boundary itself, which is the point above.
