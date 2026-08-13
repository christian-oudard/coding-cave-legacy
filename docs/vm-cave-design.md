# cave-vm: full design and implementation plan (Go, single VM, rootless)

Status: the target design. Parts of it are built; see "Where the
implementation actually is" below, which is the one section to read before
trusting any other against the tree.

This document supersedes the deleted `vm-cave-plan.md`, whose settled
decisions are restated here, and resolves the recommendation in
`vm-cave-store.md` (option B reshaped: the host projection
becomes an on-demand binary cache instead of a mounted tree).

## Scope

One long-running VM per host, shared by every agent. No multitenancy. No OS
package managers other than nix (no apt, no pacman). The host-side footprint
is rootless: no root daemon, no setuid, no host mounts; the only privileges
used are /dev/kvm access and ordinary user files.

Non-goals for this plan, kept design-compatible: zero-copy host/guest store
sharing (vm-cave-store option C), VM snapshot/park, per-agent disk quotas,
multitenant isolation, `cav` CLI unification (the Rust CLI keeps working for
container caves while this is built).

## Requirements this design must meet

1. Cave startup is fast: under one second from "agent requested" to "claude
   is starting", VM already up. VM cold boot is paid once per host boot.
2. A package present anywhere on the host or in the guest is never
   re-downloaded from the internet.
3. Caves installing the same package share one copy inside the guest, and
   artifacts no surviving cave references are reclaimed. The only accepted
   duplication is host store vs guest store, once per host.
4. Normal dev environment: stock multi-user nix (`nix build`, `nix develop`,
   `nix profile install` work unmodified), plus uv, cargo, pnpm, go, elan or
   lake behaving normally.
5. No escape for a compromised agent: defeating bwrap yields guest root at
   worst. The host is reachable only through enumerated vsock channels, and
   credentials never enter the guest.

## Carried-over decisions (from the superseded bring-up plan)

- VMM: Cloud Hypervisor on Linux; ~130 ms plain boot measured, ~550 ms for
  CHV-in-CHV nested cold start. Kernel config invariants and the
  symbol-assertion build check carry over verbatim, and are restated under
  "Kernel invariants" below because they were expensive to learn. The
  original reason to prefer it, that nested KVM keeps the subcave workflow,
  has since narrowed: Apple gates nested virtualization to M3 and later, so
  on earlier hardware there is no subcave workflow for any VMM to keep.
  macOS uses vfkit, as a second process backend behind one interface;
  `vm-cave-cross-platform.md` has the argument and the measurements.
- Language: Go, module at `cave-vm/` at the repo root. Chosen as a vehicle
  for learning the ecosystem, not for a technical reason; a future reader
  should not go looking for a hidden one.
- One VM for everyone, lifecycle implicit (first agent brings it up), state
  under `$CODING_CAVE_DATA_DIR`, default `~/.local/share/coding-cave/`.
- crinj is the sole network authority. Every byte of guest egress goes
  through it; API keys exist only on the host.
- VM lifecycle is decoupled from any shell session.

## Kernel invariants

Lifted from the Nix exploration that preceded the rewrite, because silent
Kconfig drops cost multiple 12-minute rebuild cycles to diagnose:

- `CONFIG_VIRTUALIZATION` is the Kconfig parent gating the entire KVM
  subtree. With it off, everything below drops silently.
- `CONFIG_KVM` plus `CONFIG_KVM_INTEL` and `CONFIG_KVM_AMD`, for nested.
- `CONFIG_PCI` and `CONFIG_VIRTIO_PCI`, because CHV puts every device on
  virtio-pci.
- `CONFIG_PVPANIC_PCI`, for defence in depth.
- `CONFIG_ACPI_SLEEP`, so `poweroff` triggers ACPI S5 and the VMM exits
  cleanly.

**Do not trust Kconfig; assert on the linked kernel.** The build check
asserts the required symbols are present (`vmx_init`, `pvpanic_pci_probe`,
`__pci_register_driver`, and the rest). This exists because a Kconfig option
can be dropped without error and the failure only appears at boot.

One related finding worth not rediscovering: microvm.nix's stock
`virtiofsd-run` is unusable in an unprivileged cave, because it wants
`supervisord --dropPrivileges` and `chown :kvm` on the socket. A launcher
must drive virtiofsd directly.

## Where the implementation actually is

The bootstrap took a different route to the same place, so the tree does not
match the Go module layout below and will not for a while. What exists:

- **`scripts/vmcav`**, a shell script, still owns everything above the VMM:
  seed staging, the git daemon, the gpg broker, `agent`, `shell`,
  `image create|reformat`, and the prose of `status`. It delegates the VM
  itself and every `review|diff|approve` to the Go binary. What remains of
  its portability debt is `mke2fs` for the seed image and the assumption
  that guest store paths exist on the host.
- **`cave-vm/internal/`** is `agentd`, `approve`, `registry` and `vmm`, not
  the packages listed below. `cmd/cave-guestd` runs in the guest and owns
  each agent's pty so a session survives a lost terminal; `cmd/cave-vm` is
  host-side git plus `start|stop|serve|running|socket|vsock-connect`.
  `internal/state` and `internal/guest` do not exist yet.
- **`guest/`** is a mutable NixOS flake on a disk image, not the sealed
  squashfs the image section describes. That is deliberate and temporary;
  `vm-root-agent.md` records each bootstrap-era divergence with its
  graduation step.
- **The service plane works** end to end on both host platforms: crinj on
  7200, the git daemon on 7300, the gpg signing broker on 7400, and sshd
  inbound on 22. Verified under `cave-vm` on each: on Linux the guest boots
  to a running agent, clones the shared repo over 7300, reaches the internet
  through crinj on 7200, and has no NIC (`ip -br link` shows only `lo`); on
  an M1 the same relays carry the same traffic under vfkit. The host-side
  relays are Go, so `socat` is no longer a host dependency on either.
- **`internal/narcache` does not exist.** Phase 4 is the largest unbuilt
  piece and, per `vm-cave-todo.md`, the most differentiated.

Read the phases at the end as the remaining order of work, not as a record
of what has happened.

## System overview

Host processes (all unprivileged, run as the launching user):

- `cave-vm` (Go, this plan): CLI and long-running supervisor. Boots and
  supervises Cloud Hypervisor, owns the vsock service plane, serves the nix
  binary cache, launches crinj, proxies terminal attach.
- crinj (existing, Go): TLS-intercepting egress proxy with credential
  injection. Gains one new listener type (unix socket for the vsock bridge).
  Its planned read-through content cache is separate work; this plan only
  defines the interface to it.
- Existing `cav` host machinery reused as-is where it already speaks a
  network protocol: the git daemon and pre-receive approval gate.

Guest (NixOS image, built by the host's nix from this flake):

- systemd, sshd (vsock-bound), nix-daemon (multi-user), and `cave-guestd`
  (Go, from the same module) as the agent supervisor.
- Caves: one guest uid per agent, bwrap sandbox, shared read-only
  /nix/store, nix-daemon socket, shared language-cache mounts, private home.

There is no network device in the guest. All host/guest traffic is
virtio-vsock via Cloud Hypervisor's unix-socket hybrid model, so unproxied
egress is impossible by construction, not by firewall rule.

## The vsock service plane

CHV exposes guest-initiated vsock connections as host unix sockets at
`<state>/vsock/vsock.sock_<port>`, and host-initiated ones through
`vsock.sock` with a `CONNECT <port>` handshake; vfkit gives each port its own
socket. Fixed port map, one Go constant table (`internal/vmm`):

- 22   guest sshd (host-initiated; terminal attach)
- 7001 control RPC: cave-guestd gRPC-free JSON over HTTP (host-initiated)
- 7100 nix binary cache (guest-initiated, served by cave-vm on the host)
- 7200 crinj HTTP(S) proxy (guest-initiated, bridged to crinj's listener)
- 7300 git (guest-initiated, bridged to the existing per-project git daemon)

Guest side, a tiny forwarder in cave-guestd exposes each guest-initiated
service as 127.0.0.1:<port> inside the guest (systemd socket units would
also work; doing it in guestd keeps the image free of per-service units).
In-cave configuration then never mentions vsock: `HTTPS_PROXY` points at
localhost, nix substituters point at localhost, git remotes point at
localhost.

## Nix packages

### Guest store and daemon

One store, one stock nix-daemon in the guest. `/etc/nix/nix.conf` in the
image:

    experimental-features = nix-command flakes
    trusted-users = root
    allowed-users = @caves
    sandbox = true
    auto-optimize-store = true
    require-sigs = true
    substituters = http://127.0.0.1:7100 https://cache.nixos.org
    trusted-public-keys = cave-host-1:<generated> cache.nixos.org-1:<stock>

Properties that fall out with zero code: caves cannot inject unsigned
paths (daemon trust model), identical installs by N caves are one valid
path in one store, per-uid GC roots and profiles, `nix develop` and
`nix build` behave exactly as on any NixOS machine. `populate_cave_store`,
per-cave chroot stores, the converge daemon, the promotion gate, and the
shared store have no counterpart here; in the VM world they are deleted,
not ported (vm-cave-store fact 5 is overridden by this plan).

`auto-optimize-store` gives intra-guest file-level dedup between similar
store paths as a bonus.

### Host binary cache server (`internal/narcache`)

cave-vm serves the host's /nix/store over the standard binary-cache HTTP
protocol on vsock port 7100. This replaces vm-cave-store's projected tree,
manifest files, and want-lists: a narinfo request IS the want-list entry,
answered synchronously, and no sqlite ever crosses the boundary.

Endpoints:

- `GET /nix-cache-info` returns `StoreDir: /nix/store`, `WantMassQuery: 1`,
  `Priority: 10`.
- `GET /<hash>.narinfo`: resolve via `nix path-info --json
  /nix/store/<hash>-*` (exec, same kernel, nix handles DB locking). Respond
  with StorePath, References, NarHash, NarSize, `Compression: none`,
  `URL: nar/<hash>.nar`, and a fleet signature. 404 on unknown hash.
- `GET /nar/<hash>.nar`: stream `nix nar dump-path` (or `nix-store --dump`)
  of the path. No compression: this is a local link, CPU is the scarce
  resource, and NarSize is known up front for Content-Length.

Signing: an ed25519 keypair generated once at state-dir init
(`<state>/cache-key`, `cave-host-1:...`). Sign the standard fingerprint
`1;<path>;<narhash>;<narsize>;<refs>`. Implementation via
`github.com/nix-community/go-nix` (nixbase32, narinfo, signature); the nar
bytes themselves come from exec'ing nix, which is already a runtime
dependency. If go-nix proves awkward, narinfo is a 10-line text format and
the signature is one ed25519 call; hand-rolling is acceptable.

Concurrency and GC: no coordination with host GC at all. If the host GCs a
path between narinfo and nar fetch, the nar request fails and the guest
daemon falls through to cache.nixos.org via crinj. Self-healing by
protocol, the property vm-cave-store had to engineer with root-lists.

Privacy note, recorded as accepted: binary-cache lookups are point queries
by store-path hash. An agent can probe whether the host has a specific
derivation it can already name, and cannot enumerate the host store.

### What "fast" means here

Cave start does no store work: guestd creates a uid and a home, binds
/nix/store (ro) and the daemon socket into the bwrap, and execs the agent.
The user environment from cave.nix is built by the guest daemon on first
use, fast because every host-present input substitutes over vsock at disk
speed. The 8.6 s `populate_cave_store` phase and the 5 GB working-set seed
have no equivalent.

## Language package managers

Owned by package-dedupe (`pkgdd`), specified in `package-dedupe-spec.md`:
shared per-ecosystem stores (uv, pnpm, cargo, go), GC roots derived from
the caves' native lockfiles, and a serialized sweep that reclaims artifacts
no surviving cave references. cave-vm consumes it and reimplements none of
it; the earlier CacheSpec design (guestd owning cache env wiring with no
reclamation story) is superseded. pkgdd is an independent project (Rust,
per its spec); the cleanroom rule bans depending on cav-rs, not on new
tools, and the guest consumes pkgdd as a packaged binary.

Deployment in the guest image:

- pkgdd binary, `/etc/pkgdedupe/config.toml` with stores under
  `/var/cave/cache/{uv,pnpm-store,cargo,gomod}` (one filesystem with the
  worktrees, satisfying the same-fs invariant), roots and leases dirs under
  `/var/cave/pkgdedupe/`, the `pkgstore` user, and the `pkgdd gc` timer
  with the disk-pressure trigger from pkgdd's NixOS module.
- Write model A (insert-only by ownership) maps directly onto this design's
  per-cave uids: stores mounted rw, existing artifacts owned by
  `pkgstore` mode 444, caves can insert but not mutate. This upgrades the
  earlier stance (tamper-tolerance among cooperating agents) to inode
  immutability. The spec's open verification item (uv cache insertion
  across uids) gates it; the fallback is group-writable stores with
  tampering accepted as VM-bounded, and `pkgdd doctor` in phase 8 is where
  the experiment runs.

Integration in cave-guestd (a layer-2 caller per the spec):

- Cave create: `pkgdd env create agent-<n>` with the cave's worktree as the
  workspace (one root per extra project). guestd merges the returned env
  file into the cave environment and renders the mount plan into the bwrap
  invocation via `pkgdd env plan --format bwrap`.
- Cave destroy: `pkgdd env destroy agent-<n> --keep-workspace` (worktree
  lifecycle stays with guestd and the git flow).
- Leases: agents run native tools unwrapped, so v0 relies on the spec's
  grace-window fallback rather than lease integration; reclamation is
  delayed, never unsafe.
- nix is not routed through pkgdd. The guest nix-daemon with per-uid roots
  already is the nix instance of the same model, and pkgdd's nix plugin
  delegates to it anyway.
- lean is outside pkgdd v0.1: `ELAN_HOME=/var/cave/cache/elan` and a shared
  `~/.cache/mathlib` symlink stay as guestd-wired shared directories,
  unswept (toolchains pinned by elan's own metadata), a candidate future
  pkgdd plugin.

## Git, worktrees, approval

Worktrees live on the guest volume (virtio-fs never touches the hot build
path). The existing host-side machinery is reused unchanged at the protocol
level: the per-project git daemon serves the shared bare repo, the
pre-receive hook enforces the agent-branch rules, `cav approve` runs on the
host against the bare repo exactly as today. The only change is transport:
guest remotes are `git://127.0.0.1:7300/<slug>`, bridged over vsock to the
host daemon. Agent content still becomes trusted code only through the
host-side approval gate; nothing an agent writes crosses otherwise.

Home materialization is one-way: cave homes are created in the guest from a
seed tree (dotfiles, claude config) shipped read-only via a small virtio-fs
share of `<state>/seed/`, applied by guestd at cave creation. There is no
RW mount of any host directory (revises the plan's earlier "mirror host
home RW", which contradicted requirement 5).

## Guest image

A flake output `nixosConfigurations.cave-guest` producing three artifacts:
kernel, initrd, and a read-only squashfs root. Guest mutable state lives on
one raw disk image (`<state>/guest.img`, ext4, sparse, created on first
start) mounted at `/var/cave` and holding the guest nix store
(`/nix/store` bind from `/var/cave/nix`), cave homes, worktrees, and the
shared caches. Image contents: systemd, nix with the daemon enabled, sshd,
bwrap, cave-guestd, pkgdd (with its config, `pkgstore` user, and gc
timer), and nothing else; agent-visible tooling comes from the store via
cave.nix, not the image. Upgrading the image is: build
new artifacts, stop VM, start VM (guest state survives on guest.img).
Accepted cost: an image upgrade evicts every running agent. There is no
online upgrade path, matching Kata and unlike containerd.

Rootless notes: CHV runs as the user with /dev/kvm; vsock is the
unix-socket hybrid (no /dev/vhost-vsock); the seed share uses an
unprivileged virtiofsd. Nothing in the host plan requires root, sudo, or a
systemd system unit. `cave-vm start` daemonizes the supervisor; a systemd
user unit is optional convenience.

## Sessions and attach

cave-guestd owns each agent's pty directly, so a session survives host-side
detach and terminal loss (and, with the VM being long-lived, laptop client
disconnects in the eventual remote-host setup). It keeps a bounded buffer of
each agent's recent output and replays it on attach, which is what makes a
reattach come back to the screen the agent is on rather than a blank one;
more than one client can watch the same agent at once.

No multiplexer. tmux and dtach were both tried and dropped: tmux brings
windows, panes, a prefix key and a scrollback worse than the terminal's own,
and dtach buffers nothing and never resizes a session to its attaching
client, so every reattach came back blank and at the wrong width.

`cave-vm agent [n]` attaches: ssh over vsock (ProxyCommand `cave-vm
vsock-connect 22`) into `cave-guestd attach`, which relays the terminal raw
and forwards its size on attach and on every resize. Detach is ctrl-\ and
leaves the agent running; attaching to an agent that has exited starts a
fresh one under the same name. Host ssh keypair generated at state-dir init
and baked into the guest's authorized_keys at image build.

Control RPC (port 7001, JSON over HTTP, no framework): `POST /agents`
(create cave: project slug, base branch, prompt), `GET /agents`,
`POST /agents/<n>/stop`, `GET /health`. cave-vm is the only client; the
protocol is internal and versioned by the image/binary shipping together.

## Prior art: containerd and Kata Containers

Checked against the closest established systems. containerd is the
general-purpose case; Kata Containers (`containerd-shim-kata-v2`) is the
direct analogue, being VM-per-pod with an in-guest agent over vsock and
virtio-fs for the rootfs.

Where this design already matches the prior art, and can stop
re-deriving:

- `cave-guestd` is `kata-agent`: a guest supervisor reached by a small RPC
  over vsock (port 7001 here, ttrpc there).
- narcache is containerd's content store plus registry pull: blobs
  addressed by digest, fetched on demand, dedup falling out of the
  addressing rather than out of a sync protocol. A narinfo is a manifest
  fetch, a nar is a layer.
- pkgdd's roots-and-leases GC is containerd's GC model (`gc.root` labels,
  leases with expiry, mark-and-sweep under a lock).
- Worktrees on the guest volume rather than virtio-fs repeats Kata's own
  history: 9p to virtio-fs to DAX, every step driven by build-path I/O.

Where the prior art knows something this design does not yet apply:

- **Lifetime decoupling is the shim's whole reason to exist.** containerd
  never parents workloads. The shim owns the container and is re-attached
  over its socket, so restarting or upgrading the daemon kills nothing.
  Here, CHV is a child of the `cave-vm` supervisor. The supervisor should
  be stateless and re-attach to a running VM through the CHV API socket
  plus a pidfile, so it can crash or be upgraded under a live VM (phase
  10).
- **GC scheduling, not just the GC algorithm.** containerd schedules its
  sweep on pause-time feedback rather than a fixed timer. Noted in
  package-dedupe-spec.md as a reference for pkgdd's sweep.

Accepted costs, recorded rather than solved:

- Image upgrade evicts every agent (stop VM, start VM). containerd
  upgrades online; Kata has the same constraint this design does.
- Agent-agent isolation is one VM with per-uid caves, the same stance as
  containerd namespaces (metadata separation over a shared content store,
  not a security boundary). Kata's unit is a VM per pod. If adversarial
  agent-agent isolation ever becomes a requirement, the migration is
  VM-per-agent, and the fixed vsock port table plus the single-guestd
  assumption are what bend first.

Where this design beats the prior art: rootlessness. containerd rootless
needs rootlesskit, user namespaces and slirp4netns, and still loses
capabilities, because it wants root-shaped operations on the host. This
design inverts that: unprivileged on the host (/dev/kvm only), ordinary
root inside the guest. And having no NIC at all is stronger than any
firewall-pinning arrangement Kata or firecracker-containerd ship.

## Go module layout

    cave-vm/
      go.mod
      cmd/cave-vm/        host CLI: start stop status shell agent vsock-connect
      cmd/cave-guestd/    guest supervisor (built into the image)
      internal/vmm/       VMM process control and the vsock plane: launch,
                          clean shutdown (ACPI), crash detection, port map,
                          host unix-socket dialer/listener. One Backend per
                          platform (Cloud Hypervisor, vfkit).
      internal/narcache/  binary cache HTTP server, narinfo signing,
                          nix CLI exec wrappers
      internal/guest/     guestd internals: cave lifecycle, uid allocation,
                          bwrap invocation, home materialization, CacheSpec
                          table, pty session management
      internal/state/     state dir layout, key generation, guest.img
                          creation, image artifact resolution
      internal/rpc/       control protocol types shared by CLI and guestd

`internal/vmm` was two packages in an earlier draft of this layout, `chv` and
`plane`, written when Cloud Hypervisor was the only backend. CHV selects a
host-initiated port with a `CONNECT <port>` handshake at connect time, so a
plane really could dial a port the VMM knew nothing about. vfkit takes one
`--device virtio-vsock` per port, with number, socket path and direction all
fixed in the launch argv, which leaves the two halves unable to be built
without each other. The constraint that survives, and which binds on Linux
too: no feature may allocate a host-initiated vsock port after boot.

Shared types live in internal/rpc only; host-only and guest-only code do
not import each other. Both binaries build from one module, cross-compiled
statically (CGO_ENABLED=0) so the guest image build is a plain nix
buildGoModule.

## Phases

Each phase lands runnable end-to-end with its acceptance check; no deferred
wiring. Phases 0 and 1 restate the old plan's floor.

- **0. Skeleton.** Module, cmd layout, `cave-vm version`. Builds via nix.
- **1. Boot floor.** `cave-vm start` boots CHV with an off-the-shelf
  nixpkgs kernel and a stub initramfs; `cave-vm stop` triggers ACPI
  poweroff and CHV exits cleanly. Accept: start-stop cycle leaves no
  processes and no stale sockets.
- **2. Real guest.** cave-guest image (kernel, initrd, squashfs), guest.img
  creation, systemd boots, cave-guestd starts and answers `GET /health`
  over vsock. `cave-vm status` reports it. Accept: cold boot to healthy
  under 10 s; state survives stop/start.
- **3. Attach plane.** sshd on vsock, `cave-vm shell` lands in a guest
  session held by the daemon. Accept: detach, reattach, session intact.
- **4. Nix supply.** narcache server, fleet key, guest daemon substituter
  order. Accept: with crinj absent and no network, `nix build nixpkgs#hello`
  (host-present) succeeds in the guest and transfers zero internet bytes;
  a host-absent package fails (proving no hidden egress).
- **5. Egress.** crinj bridged on 7200, guest proxy env global. Accept: the
  host-absent package now substitutes from cache.nixos.org through crinj;
  guest has no NIC (`ip link` shows only lo).
- **6. Caves.** guestd cave lifecycle: uid, home from seed, bwrap profile
  (ro /nix/store, daemon socket, shared caches, private home, worktree),
  claude launch with the existing config seeding ported. Accept: two caves
  run concurrently; second cave start-to-claude under 1 s.
- **7. Git.** Bridge to the host git daemon, worktree clone at cave
  creation, agent branch push, `cav approve` on the host completes the
  round trip. Accept: commit in cave, approve on host, merged on host.
- **8. Language caches.** pkgdd in the image; guestd env create/destroy
  integration; env file and bwrap mount plan wired; `pkgdd doctor` clean
  (runs the write-model-A experiment from the spec). Accept: cave A
  `uv add numpy`, cave B `uv add numpy`; B does zero network fetches and
  one copy exists on disk (`st_nlink` >= 3); same check for pnpm; destroy
  both caves and `pkgdd gc` frees the wheel, while a third cave still
  locking it pins it.
- **9. User env.** cave.nix built by the guest daemon at cave creation,
  profile linked into the cave home. Accept: cave start stays under 1 s
  when the env is already built; first-ever build is dominated by vsock
  substitution, not evaluation.
- **10. Lifecycle polish.** start-if-needed from `cave-vm agent`, image
  upgrade flow, crash restart, log rotation under `<state>/logs/`. The
  supervisor holds no state a running VM depends on: CHV is spawned
  detached, and `cave-vm` re-attaches through the CHV API socket plus a
  pidfile (containerd's shim model). Accept: kill the supervisor, restart
  it, the VM and every agent session survive and `cave-vm status` is
  correct again.

## Deletions and revisions this plan implies

- `vm-cave-plan.md` is deleted. It asked to be, once the Go bring-up was
  real and its load-bearing decisions were folded in, which is what the
  carried-over decisions and the kernel invariants above are. Its own
  standing instruction survives it: do not re-add `vm-transition-plan.md`,
  which was a moving target during exploration.

- The bring-up plan's Phase 3 store-sharing sketch and acceptance criterion
  are replaced by phase 4 above (as vm-cave-store.md already argued); the
  RW home mirror is replaced by one-way seed materialization; the
  "store sharing: virtio-fs of host /nix/store RO plus overlay" carry-over
  conclusion is retired.
- vm-cave-store.md: option B is adopted in on-demand form (binary cache,
  not mounted projection); options A and C rejected/deferred as written;
  option D's shared-store projection is unnecessary because point queries
  already avoid enumeration, and the shared store itself does not exist in
  the VM world.
- The container-cave architecture on main (per-cave chroot stores, seeding,
  cav-store converge, promotion gate) is not ported into the guest. It
  keeps running on main for non-VM caves during the transition; its
  startup-latency fixes are separate main-branch work.

## Open questions

- How much of cav-rs to reuse in the guest: cave-guestd reimplements cave
  launch (bwrap profile, config seeding) in Go per the cleanroom decision,
  but the bwrap flag set and seccomp filter should be lifted from the Rust
  implementation's sandbox config rather than re-derived. Mechanism TBD
  (share the JSON sandbox config the nix build already emits).
- CLI unification: when does `cav agent` learn to delegate to cave-vm, and
  which of status/approve/cp grow VM awareness first.
- crinj read-through content cache scope and eviction policy (separate
  crinj plan; the vsock bridge here does not depend on it).
- Where package-dedupe lives: its own repo (its layer 1 is deliberately
  reusable beyond caves) or a crate in this one. Its spec, phases, and
  open questions (write model A verification, lease depth, sccache
  lifecycle) are tracked in package-dedupe-spec.md, not here.
- Guest volume filesystem: ext4 assumed; revisit only if reflink support
  (xfs) turns out to matter for uv/pnpm patterns hardlinks don't cover.
