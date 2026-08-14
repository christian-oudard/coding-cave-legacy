# vm-root-agent: the in-VM bootstrap agent

Status: built and running. Companion to `vm-cave-design.md`. Two of the three
bootstrap-era divergences below have since graduated; the section says which.

## Purpose

The vm-root-agent inverts the build order: a Claude agent session runs
*inside* the cave VM with full root over the guest, and builds the in-guest
half of the system (cave-guestd, the nix-daemon deployment, pkgdd wiring,
cave sandboxing) from within, testing against the real environment on every
step. It can redeploy the guest, including its own runtime, autonomously.

This worked. The guest supervisor, the vsock service plane, the git approval
round trip and the gpg signing broker were all built from inside the VM, and
`vm-cave-red-team.md` is an adversarial review the agent ran against the
running boundary from that position.

The agent is "cave 0": the first inhabitant of the VM, privileged inside
it, and subject to exactly the same host boundary as every future cave.

## Trust model (unchanged from vm-cave-design.md)

Root inside the VM grants nothing outside it:

- Credentials stay on the host. The agent reaches the Anthropic API and all
  package registries only through crinj; a fully compromised guest never
  sees a real key.
- Code becomes trusted only through the host-side git approval gate. The
  agent pushes its agent branch to the host repo and the user approves from
  the host, exactly as container caves work today.
- The host surface is the VMM plus the enumerated channels below. Giving
  the agent guest root changes the blast radius inside the guest from
  "one cave" to "the whole guest", which during bootstrap contains nothing
  but the agent itself.

## Bootstrap environment

The production design (sealed squashfs root, direct kernel boot, vsock-only)
is the *endpoint*. The bootstrap era intentionally differs in three ways,
each chosen to maximize the agent's ability to redeploy itself, and each
with a defined graduation step:

1. **Mutable NixOS guest, not a sealed image.** The guest is a standard
   NixOS on a disk image, its configuration a flake *in the repo checkout
   inside the guest* (`guest/` directory, `nixosConfigurations.cave-guest-dev`).
   The agent redeploys with `nixos-rebuild switch --flake`. NixOS
   generations are the rollback mechanism. Graduates to: the sealed image
   built by cave-vm, once cave-guestd and the image build exist.
2. ~~**Bootloader boot (EDK2 firmware + systemd-boot), not direct kernel
   boot.**~~ **Graduated.** The reasoning was that a bootloader keeps kernel
   upgrades and generation rollback fully in-guest, where direct kernel boot
   puts kernel choice on the host and outside the agent's reach. The guest
   now boots directly: `boot.loader.grub.enable = false`, and the host passes
   `--kernel/--initramfs/--cmdline`. The cost predicted here is real and
   still paid. A generation that changes the kernel needs the host to boot
   the new one, which `guest/configuration.nix` records at the point where it
   bites.
3. ~~**A NIC behind passt, not vsock-only.**~~ **Graduated, and it was the
   acceptance test.** The bootstrap VM originally had user-mode networking
   with a guest firewall pinning egress to crinj, which is a rule rather than
   an impossibility. The agent built the vsock plane and removed its own NIC.
   The guest now has no network interface at all: crinj on 7200, git on 7300,
   the gpg broker on 7400, sshd inbound on 22, and `vm-cave-red-team.md`
   confirms no NIC against the running system.

Host-side, until cave-vm exists, a checked-in bootstrap script
(`scripts/vmcav`) does what `cave-vm start` will eventually do: build the
initial image from the flake, start the VMM (rootless, /dev/kvm on Linux),
serve the
repo over a git daemon, stage the host seed (crinj CA, claude login state,
ssh key), and manage the VM's lifecycle (start/stop/status/shell). The
script is throwaway by design; retiring it piece by piece into `cave-vm` is
the work.

## First milestone: the minimal floor

The smallest thing that is the system at all, and the only part built by
hand. Everything in "The redeploy loop" below (health checks, boot
counting, resume markers) is built *by the agent* afterward, not a
prerequisite.

- A minimal NixOS guest: sshd, git, nix (daemon mode), cave-guestd,
  claude-code, a repo clone, and the agent unit. Nothing else.
- claude runs as root with bypass permissions
  (`--dangerously-skip-permissions`); inside the VM there is nothing to
  protect from it.
- crinj on the host is the only egress and injects the Claude OAuth
  credentials; the guest never holds a real token.
- The host git daemon serves the repo; the agent pushes its spec ref
  (pre-receive permits fast-forwards under refs/heads/agent-*/** and
  nothing else). vmcav builds the guest from that ref, so a push is a
  deploy on next boot, with no host approval in the loop. Merging into the
  human's branches remains a host-side decision.

Acceptance: attach over ssh, find claude running, ask it to make a trivial
guest config change, watch it `nixos-rebuild switch`, commit, and push for
approval, all without any credential existing inside the VM.

## The agent runtime

- `vm-root-agent.service`, a systemd unit in the guest config: a
  cave-guestd session running claude-code as root in `/root/coding-cave` (a clone of
  the vm-cave branch, remote pointing at the host git daemon). The unit is
  part of the NixOS configuration, so the agent redeploys its own runtime
  like everything else, with generations as the undo.
- Auth via the crinj OAuth passthrough; config seeding (trust dialog,
  onboarding) as the Rust implementation does today, translated into the
  guest config's activation.
- The user attaches from the host (`ssh -> cave-guestd attach`) to steer, and
  approves commits from the host. The agent otherwise works unattended.

## The redeploy loop

The core capability requested: the agent changes the system it is running
on, safely, without a human in the loop.

1. **Edit** guest config and Go code in `/root/coding-cave`.
2. **Switch**: `nixos-rebuild switch --flake .#cave-guest-dev`. Userspace
   changes (guestd, pkgdd config, units, the agent's own service) activate
   in place; the session and the claude process survive because the daemon
   holding the pty is not restarted by the switch
   (`X-RestartIfChanged=false` on the agent unit; redeploying the agent
   binary itself takes effect on next session).
3. **Health check**: a `guest-health` script (also in the config) verifies
   the invariants that make the agent reachable and recoverable: sshd up,
   git remote reachable, crinj egress works, disk not full, agent unit
   loaded. Run after every switch; on failure the agent rolls back with
   `nixos-rebuild switch --rollback` before anything else.
4. **Reboot when needed** (kernel, early-boot changes): the agent writes a
   resume marker (`/var/lib/vm-root-agent/resume`: session id, the prompt
   to continue with, the generation being tested), then reboots. On boot,
   the unit finds the marker and resumes the session (`claude --resume`)
   with a note stating which generation booted and the health-check result.
   systemd-boot boot counting handles the bricked case: a generation that
   fails to reach `boot-complete.target` N times falls back to the previous
   one automatically, and the agent wakes up in the old generation with the
   marker telling it what it was attempting.
5. **Commit and push** once a step lands: the agent fast-forwards its spec
   ref (agent-1/vm-cave; root is a per-VM flag, not a reserved id, so the
   root agent still gets a normal id), which is what vmcav builds from, so
   pushing is deploying, with no host permission required. The VM is cattle:
   deleting the disk and running `vmcav start` must always produce a
   working agent from the last pushed spec commit. Merging the spec into
   vm-cave/main is still a host decision, but the VM never waits on it.

Rule 5 is the real safety net and a standing discipline: anything the agent
does to its environment that matters must be expressed in the committed
flake, not in imperative drift. The health check asserts rebuildability
indirectly; from-scratch rebuilds (delete the disk, `vmcav start`) in
CI-fashion are the direct test and should be run at every graduation step.

## Mission and guardrails

The agent's standing mission, encoded in the guest CLAUDE.md:

- Build vm-cave-design.md's phases from the inside out: the vsock plane,
  cave-guestd, the nix supply chain (guest daemon side), pkgdd deployment,
  cave sandboxing, then help the host side (`cave-vm`) replace the
  bootstrap script.
- Every phase's acceptance check from vm-cave-design.md is run inside the
  VM and its transcript committed with the phase.
- Work phase by phase; keep the guest rebuildable from the approved branch
  at all times; prefer config over imperative state.
- Never attempt to widen host access. The channels are what they are;
  needing a new one is a design conversation with the user, not a firewall
  edit. (Enforced by the boundary anyway; stated so the agent routes the
  need into a commit rather than a workaround.)

## Graduation

The vm-root-agent is scaffolding. It retires in stages, each stage deleting
one bootstrap-era liberty:

1. vsock plane built: NIC removed, passt retired.
2. cave-guestd manages caves: the root agent stops hosting sessions other
   than itself; new work happens in ordinary unprivileged caves inside the
   VM, root agent used only for guest-level changes.
3. Sealed image + cave-vm image lifecycle built: mutable NixOS and
   in-guest nixos-rebuild retired; guest changes become image rebuilds on
   the host, and the root agent's remaining role (guest OS maintenance)
   ends. What may survive it: a non-interactive `guest-admin` capability
   for health and diagnostics, decided then.

## Open questions

- Session continuity across from-scratch rebuilds: resume markers survive
  reboots on the state volume, but a rebuilt VM has no session history.
  Acceptable (the branch is the memory), or worth persisting
  `/root/.claude` on the state volume from day one?
- Whether the bootstrap flake lives in `guest/` of this repo (assumed
  above) or a separate repo; in-repo keeps the approval gate covering the
  guest config, which argues strongly for in-repo.
- How much of the Rust implementation's config seeding and sandbox profile
  the agent may read for reference: per the branch rules, semantics only,
  via branch switching on the host and pasted excerpts, since the guest
  clone tracks vm-cave.
