# cave-vm on macOS: constraints, VMM options, prior art

Status: findings. No decision taken. Records what a macOS port of the design
in `vm-cave-design.md` costs, what it cannot have, and what the surrounding
field has already settled.

The cave model is unchanged by the port. Guests are Linux, caves are bwrap
under per-uid separation, and one VM is shared by every agent. Only the VMM
and the paths around it are platform-specific.

## Hard constraint: no nested virtualization before M3

Apple gates nested virtualization to M3 and later, on macOS 15 and later. M1
and M2 cannot do it at all, and this is enforced in the hypervisor, not in any
VMM. The Asahi project has it working on M2, so the limit appears to be
Apple's choice rather than silicon, which does not help us.

`vm-cave-design.md` records "Nested KVM keeps the subcave workflow" as part of
why Cloud Hypervisor was chosen. On pre-M3 hardware there is no subcave
workflow to keep, whatever VMM runs underneath. Docker Sandboxes runs on M1
and does not offer nesting either, which is the same constraint showing up in
someone else's product.

The development machine currently in use is a `Macmini9,1`, M1, 8 GB.

## Proof of concept: NixOS on Virtualization.framework

Working, and it needs neither Homebrew nor Nix on the host:

    mkdir -p ~/lima && cd ~/lima
    curl -fsSL -O https://github.com/lima-vm/lima/releases/download/\
    v2.2.0/lima-2.2.0-Darwin-arm64.tar.gz
    tar xzf lima-2.2.0-Darwin-arm64.tar.gz
    ./bin/limactl start --yes --memory 4 --cpus 4 --name nixos \
        github:nixos-lima
    ./bin/limactl shell nixos

Result: NixOS 26.11 aarch64, kernel 7.1.1, nix 2.34.7, `nix run nixpkgs#hello`
substitutes and runs. Cold boot to SSH-ready is 12 s, which includes Lima's
own readiness probes and guest agent and so overstates what a minimal VMM
would pay.

Two facts this establishes for the port. `limactl` ships ad-hoc signed with
`com.apple.security.virtualization`, so the `vz` driver runs with no QEMU
anywhere. And Lima's vz driver already reaches guest sshd over vsock, so the
vsock service plane ports across rather than needing a substitute.

Lima is a proof vehicle, not a candidate: it wants a NIC, its own guest
agent, and its own opinions about mounts.

## VMM options

Measured against what `vm-cave-design.md` actually asks a VMM for.

**vfkit** (crc-org, Go, Virtualization.framework) is the closest analogue to
Cloud Hypervisor, and powers `podman machine`:

| design need | vfkit |
| --- | --- |
| direct kernel boot | `--bootloader linux,kernel=,initrd=,cmdline=` |
| guest-initiated vsock as host unix sockets | `--device virtio-vsock,port=N,socketURL=` |
| host-initiated vsock | same, plus `,connect` |
| raw `guest.img` | `--device virtio-blk`; raw only, APFS supplies sparse and CoW |
| read-only seed share | `--device virtio-fs,sharedDir=,mountTag=` |
| API socket for re-attach (phase 10) | `--restful-uri unix://` |
| no NIC | omit the network device |
| rootless | entitlement-gated; no `/dev/kvm` equivalent needed |

It ships a pre-signed `vfkit` binary next to `vfkit-unsigned`, which is how
the codesigning problem gets solved without teaching the nix build to sign.

Others considered: **krunkit** (libkrun on Hypervisor.framework, more
microVM-shaped, less feature surface); **Virtualization.framework directly**
via Code-Hex/vz Go bindings (most control, and we sign our own binary);
**Tart** (Apple Silicon native, oriented at whole-VM workflows rather than a
service plane); **QEMU with hvf** (works, but vsock is painful with no
`/dev/vhost-vsock` on the host).

### One backend or two: settled by phase 10

**libkrun** looks at first like the only option covering both platforms with a
single API: KVM on Linux and HVF on macOS/ARM64, with virtio-vsock, virtio-fs
and virtio-block behind one C library, Apache-2.0. It is what krunkit,
microsandbox, crun and muvm are built on.

It is nonetheless the wrong shape here, for a reason that comes out of this
project's own requirements rather than out of libkrun. `krun_start_enter()`
makes the calling thread become the VM, so linking libkrun puts the guest
inside the supervisor process. Phase 10 requires the opposite: "kill the
supervisor, restart it, the VM and every agent session survive," recorded
because containerd's shim exists precisely for lifetime decoupling. A VM
living in the supervisor cannot outlive it.

So the VMM must be a separate process on both platforms, and once it is, the
single-API advantage evaporates because we talk to it over a socket rather
than through its C API. The cgo tension with the `CGO_ENABLED=0` ground rule
is therefore moot, not traded away.

That leaves two process-backends behind one interface:

- Linux: Cloud Hypervisor, already built.
- macOS: vfkit, or krunkit. These are deliberately CLI-compatible (krunkit
  documents options as existing "purely for vfkit parity"), and both offer
  `--restful-uri`, `--device virtio-vsock` and `--device virtio-blk`.
- Windows, if ever: a third backend, unaffected by this choice.

Prefer **vfkit** initially: krunkit supports only the EFI bootloader, so it
wants an EFI-bootable disk image rather than the kernel, initrd and squashfs
artifacts the guest flake already produces. krunkit remains a later option for
libkrun's boot times without much backend churn.

### The vsock plane differs in shape, not capability

Cloud Hypervisor exposes guest-initiated connections as `vsock.sock_<port>`
and host-initiated ones through a single `vsock.sock` with a `CONNECT <port>`
handshake. vfkit instead declares direction per port at launch and gives one
unix socket per port. With the port table fixed (22, 7001, 7100, 7200, 7300)
these are equivalent, and `internal/plane` already exists as the abstraction
that hides the difference. The port is `internal/chv` gaining a sibling and
`internal/plane` gaining a second backend; nothing above them changes.

## New problem the Linux design does not have

Bootstrapping the guest image. Building the `aarch64-linux` NixOS guest
requires a Linux builder, and a Mac has none until it has the VM. No phase in
`vm-cave-design.md` covers this. Options: ship prebuilt guest artifacts (what
nixos-lima does), use `nix-darwin`'s `linux-builder` or `nix-rosetta-builder`,
or build on a Linux machine and copy. Note that an `x86_64-linux` cave cannot
produce them either.

Second, smaller: with `--bootloader linux` on Apple Silicon the kernel must be
uncompressed or the VM hangs, and vfkit errors out if it detects compression.
Confirm what `system.build.kernel` emits for `aarch64-linux` before relying on
it.

## Prior art

### Docker Sandboxes

One microVM per agent, with the agent running directly in the VM as a non-root
user with sudo. Containers appear only when the agent itself runs `docker
build`; the daemon serves the agent's container work and is not the mechanism
separating agents from each other. Their docs are explicit: "The hypervisor
boundary is the isolation control, not in-VM privilege separation."

Not a substrate for this design, for reasons that are structural rather than
matters of taste. N agents means N kernels and N filesystems, therefore N nix
stores, and "multiple sandboxes don't share images or layers" is stated
outright. That is the inverse of requirements 2 and 3, and it reintroduces
precisely the problem narcache and pkgdd exist to solve. The per-sandbox
Docker daemon is dead weight for a nix-only guest: idle RSS plus a second
image-distribution and GC system duplicating the store. The VMM is closed and
`sbx login` binds it to a Docker account. And it has no opinion at all about
multi-agent coordination, so the git daemon, agent branches, and the
pre-receive approval gate remain ours to build regardless.

Worth taking from it, though: one VMM codebase over Hypervisor.framework,
Windows Hypervisor Platform, and KVM is evidently viable, and it runs on M1,
confirming a non-nested design is fine.

#### Scored against this project's requirements

Being fair to it, `sbx` solves more of these than a first read suggests. It is
two specific requirements, both consequences of one-VM-per-agent, that rule it
out.

- *Multiple projects per agent*: solved. `sbx run claude ~/project-a
  ~/shared-libs:ro ~/docs:ro` mounts extra workspaces at their absolute host
  paths, with `:ro` available. Named sandboxes also allow several agents
  against one repository.
- *Cross-platform VM management*: solved, and better than anything we have.
- *Git workflow out to the host repo*: partial. `--clone` gives the sandbox an
  in-VM clone and adds a `sandbox-<name>` remote to the host repository, so
  work moves by fetching from the sandbox. There is no approval gate, and raw
  TCP is blocked at the network layer, so a `git://` daemon cannot be the
  transport.
- *Comms between agents*: ruled out by design. "Sandboxes cannot communicate
  with each other... no shared network between sandboxes."
- *Nix as the agents' package manager, shared*: structurally broken, per
  above.

Smaller convergences worth noting: `sbx cp` is `cav cp`, and their templates
are OCI images (`docker/sandbox-templates:claude-code`, base `ubuntu:questing`)
running the agent as a non-root user with sudo, so a nix installation would sit
inside a container image inside their VM.

Decision: not pursued past documentation. Package dedup requires one store,
one store requires one VM, and one VM shared by many agents requires the
container-within-VM pattern this design already uses. `sbx` cannot be
configured into that shape, so measuring it would only confirm the duplication
its own docs state.

#### Their code is not available

`docker/sbx-releases` is a 24 KB distribution-and-issues repo (LICENSE,
README, SECURITY.md) with binaries attached to releases. The licence reads
"Copyright © 2026 Docker Inc. All rights reserved." The VMM they describe
writing from scratch is closed.

It is also not needed. The same layer exists open and permissively licensed:

| project | licence | language | provides |
| --- | --- | --- | --- |
| libkrun | Apache-2.0 | Rust | KVM plus macOS HVF behind one API |
| apple/containerization | Apache-2.0 | Swift | Apple's VMM over Virtualization.framework |
| apple/container | Apache-2.0 | Swift | VM-per-container CLI on the above |
| vfkit | Apache-2.0 | Go | Virtualization.framework VMM as a process |
| microsandbox | Apache-2.0 | Rust | libkrun microVMs, sub-200 ms boot |
| Cleanroom (Buildkite) | MIT | Go | microVM sandbox, deny-by-default egress, credential proxy |

Cleanroom is the one worth reading closely: MIT, Go, and the closest published
combination of microVM plus egress policy plus credential proxy, so it is
where code could be lifted rather than only semantics. See below.

This also qualifies Docker's stated rationale. Their blog says no existing VMM
was cross-platform and that Firecracker has "no native support for macOS or
Windows, full stop." libkrun already covered Linux KVM and macOS HVF behind
one API. The claim holds for Windows, which libkrun does not support and which
Docker wanted; it does not hold for a Linux-plus-macOS scope.

### Convergence with crinj

Their network model arrived independently at what crinj already does. All
HTTP/HTTPS egress through a host-side proxy with credential injection there;
private, loopback, and link-local destinations blocked; sandboxes cannot reach
host localhost. Policy is per sandbox, host-side, default-deny with an
explicit allow list, which is the same answer per-agent crinj policy was
heading toward: policy lives with the per-agent proxy instance, not in one
shared config.

Two places they go further: raw TCP, UDP and ICMP are blocked outright, where
crinj tunnels raw TCP through CONNECT; and DNS resolution is itself
policy-checked at the resolver.

### The wider field

Docker Sandboxes' true peers are hosted APIs, a different market: E2B,
Daytona, Modal, Vercel Sandbox, Fly Sprites, Blaxel, Runloop, CodeSandbox,
Northflank. Locally the field is thin.

- Nearest on isolation and egress: **Cleanroom** (Buildkite) is a microVM
  sandbox with deny-by-default egress and a credential proxy, which is crinj
  and the cave in one project. **microsandbox** is libkrun microVMs with
  sub-200 ms boot. **Apple `container`** (v1.0, June 2026, Swift, open source)
  is VM-per-container on Virtualization.framework, macOS-native but the wrong
  granularity.
- Nearest on thesis: **container-use** (Dagger) gives each agent a
  containerized worktree and reviews parallel agents as git branches.
  **Conductor**, **Sculptor**, and **treebeard** are variations on isolated
  worktrees per agent.
- Anthropic's **`srt`** uses Seatbelt on macOS and bubblewrap on Linux with a
  network-filtering proxy: the same primitives, minus the VM.

Two observations from the survey. None of them uses nix; every one is OCI
images or a stock distro, which is why nothing off the shelf substitutes for
narcache and pkgdd. And none combines multi-agent git coordination with a
hardware boundary. The worktree projects are all containers and the microVM
projects have no multi-agent story, so Cleanroom and container-use are each
half of this design and neither is the whole.

### Cleanroom and SporeVM

Investigated together because Cleanroom is explicitly "a thin layer over
SporeVM" and inherits its constraints. Cleanroom is Buildkite's, MIT, Go, and
actively developed; SporeVM is the runtime beneath it, MIT, Zig, and young.

**SporeVM is not adoptable, on two independent grounds.**

Platform. It is ARM64-first: macOS/HVF and Linux/ARM64 both have the mature
full lifecycle, while Linux/AMD64 is an experimental profile with one vCPU,
512 MiB fixed, and no capture, save, restore, resume, fork or fan-out. The
entire feature that makes it interesting is absent on an x86-64 Linux host.
Spores also do not move between KVM and HVF.

Architecture. Its warm-spore model bakes a parent with dependencies installed
and caches hot, then forks children that "share the parent's chunk store" via a
hard-linked `ram.backing` and take "independent disk heads, so siblings inherit
the same bytes and then diverge." That is base sharing, not ongoing dedup: two
children each installing the same package after the fork pay separately, in
their own disk head and their own download, which is requirement 3 failing. The
disk contract is block-based over "one writable rootfs device", so there is no
natural shared `/nix/store` across siblings either. Separate VMs per agent also
put inter-agent comms back behind a host broker.

What fork optimises, VM cold start and base image duplication, this design
already amortises to zero by running one VM per host boot. It is therefore fast
at something we do not pay for, and silent on what we do.

**Cleanroom is worth mining rather than adopting.** It owns three things this
project would otherwise design from scratch, and it owns them in MIT-licensed
Go:

- A host-side **credential mediation gateway** brokering secrets into the guest
  so they never enter the sandbox or its captured artifact. The artifact half
  has no analogue here (crinj has no snapshots to leak into), but the gateway
  design is directly comparable to crinj and the code is liftable.
- A host-side **content cache** served to the guest through that gateway, for
  Git and Go. This is narcache's pattern in another ecosystem, and is useful
  independent validation of phase 4 before building it.
- A **policy compiler that fails closed**, turning `cleanroom.yaml` into
  enforceable runtime configuration and refusing anything the runtime cannot
  enforce, over a `network: {default: deny, allow: [...]}` schema. That is a
  concrete shape for the still-open per-agent crinj policy question.

Also of note, with no counterpart here: **provenance**, attaching repo, commit,
policy hash and image digest to a baked artifact and verifying them later. It
is adjacent to what the approval gate exists to establish.
