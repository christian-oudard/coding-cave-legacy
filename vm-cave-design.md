# cave-vm: full design and implementation plan (Go, single VM, rootless)

Status: plan. No code exists yet. This document supersedes the phase list in
`vm-cave-plan.md` (its settled decisions carry forward and are restated here)
and resolves the recommendation in `vm-cave-store.md` (option B reshaped: the
host projection becomes an on-demand binary cache instead of a mounted tree).

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
3. Caves installing the same package share one copy inside the guest. The
   only accepted duplication is host store vs guest store, once per host.
4. Normal dev environment: stock multi-user nix (`nix build`, `nix develop`,
   `nix profile install` work unmodified), plus uv, cargo, pnpm, go, elan or
   lake behaving normally.
5. No escape for a compromised agent: defeating bwrap yields guest root at
   worst. The host is reachable only through enumerated vsock channels, and
   credentials never enter the guest.

## Carried-over decisions (from vm-cave-plan.md)

- VMM: Cloud Hypervisor. Nested KVM keeps the subcave workflow; ~130 ms
  plain boot measured. Kernel config invariants and the symbol-assertion
  build check carry over verbatim.
- Language: Go, module at `cave-vm/` at the repo root.
- One VM for everyone, lifecycle implicit (first agent brings it up), state
  under `$CODING_CAVE_DATA_DIR`, default `~/.local/share/coding-cave/`.
- crinj is the sole network authority. Every byte of guest egress goes
  through it; API keys exist only on the host.
- VM lifecycle is decoupled from any shell session.

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
`vsock.sock` with a `CONNECT <port>` handshake. Fixed port map, one Go
constant table (`internal/plane/ports.go`):

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
    auto-optimise-store = true
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

`auto-optimise-store` gives intra-guest file-level dedup between similar
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

Design stance (settled in review): agents within the one VM are cooperating,
so shared writable caches inside the guest are acceptable; the VM boundary
is what contains a hostile agent, and lockfile hash verification (uv, pnpm,
cargo, go) is defense in depth against cache tampering. Network dedup
across time and caves comes from crinj's read-through cache on the host
(separate crinj work; until it lands, the guest-shared caches already
prevent most re-downloads).

Mechanism: one shared cache root on the guest volume, `/var/cave/cache/`,
group `caves`, setgid dirs with a default ACL granting the group rwX, so
files created by any cave are usable by all. Each ecosystem is one entry in
a single Go table in `internal/guest/caches.go`:

    type CacheSpec struct {
        Name    string   // "uv", "pnpm", ...
        Dir     string   // subdir under /var/cave/cache
        Env     []string // per-cave env vars pointing at it
        Symlink []string // $HOME-relative symlinks into it
        Files   []Seed   // dotfile fragments to merge (e.g. .npmrc)
    }

guestd applies the table when materializing a cave home. Initial entries:

- uv: `UV_CACHE_DIR=/var/cave/cache/uv`, `UV_LINK_MODE=hardlink`. The cache
  is content-addressed and concurrency-safe; hardlink mode means venvs in
  worktrees share wheel inodes with the cache (same filesystem, the guest
  volume), so N caves using the same wheel hold one copy.
- pnpm: `.npmrc` fragment `store-dir=/var/cave/cache/pnpm-store`. The store
  is content-addressed, designed for concurrent writers, and pnpm hardlinks
  from it into node_modules: one copy per package per disk, pnpm's whole
  premise.
- go: `GOMODCACHE=/var/cave/cache/go-mod`, `GOCACHE=/var/cave/cache/go-build`,
  `GOFLAGS=-modcacherw`. The module cache is verified against go.sum and
  built for sharing; the build cache is content-addressed and safe for
  concurrent use.
- cargo: symlinks `~/.cargo/registry` and `~/.cargo/git` into
  `/var/cave/cache/cargo/`. Cargo flocks the registry, so concurrent caves
  serialize correctly. `~/.cargo` itself stays private (it can hold
  credentials and config).
- lean: `ELAN_HOME=/var/cave/cache/elan` (toolchains are multi-GB, the
  single biggest win), plus `~/.cache/mathlib` symlinked into the shared
  root for `lake exe cache get` artifacts.

Adding an ecosystem later is one table row, no new machinery. What is
deliberately NOT built: a converge-style dedup daemon for language caches.
Sharing the cache up front makes after-the-fact dedup unnecessary.

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
tmux, bwrap, cave-guestd, and nothing else; agent-visible tooling comes
from the store via cave.nix, not the image. Upgrading the image is: build
new artifacts, stop VM, start VM (guest state survives on guest.img).

Rootless notes: CHV runs as the user with /dev/kvm; vsock is the
unix-socket hybrid (no /dev/vhost-vsock); the seed share uses an
unprivileged virtiofsd. Nothing in the host plan requires root, sudo, or a
systemd system unit. `cave-vm start` daemonizes the supervisor; a systemd
user unit is optional convenience.

## Sessions and attach

Agent sessions run under tmux inside the guest, owned by cave-guestd, so
they survive host-side detach and terminal loss (and, with the VM being
long-lived, laptop client disconnects in the eventual remote-host setup).
`cave-vm shell [agent]` attaches: ssh over vsock (ProxyCommand `cave-vm
vsock-connect 22`) into `tmux attach -t agent-<n>`. Host ssh keypair
generated at state-dir init and baked into the guest's authorized_keys at
image build. No custom terminal mux protocol.

Control RPC (port 7001, JSON over HTTP, no framework): `POST /agents`
(create cave: project slug, base branch, prompt), `GET /agents`,
`POST /agents/<n>/stop`, `GET /health`. cave-vm is the only client; the
protocol is internal and versioned by the image/binary shipping together.

## Go module layout

    cave-vm/
      go.mod
      cmd/cave-vm/        host CLI: start stop status shell agent vsock-connect
      cmd/cave-guestd/    guest supervisor (built into the image)
      internal/chv/       Cloud Hypervisor process control: config, launch,
                          API socket client, clean shutdown (ACPI), crash
                          detection
      internal/plane/     vsock port map, host unix-socket dialer/listener,
                          guest forwarder
      internal/narcache/  binary cache HTTP server, narinfo signing,
                          nix CLI exec wrappers
      internal/guest/     guestd internals: cave lifecycle, uid allocation,
                          bwrap invocation, home materialization, CacheSpec
                          table, tmux session management
      internal/state/     state dir layout, key generation, guest.img
                          creation, image artifact resolution
      internal/rpc/       control protocol types shared by CLI and guestd

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
  tmux. Accept: detach, reattach, session intact.
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
- **8. Language caches.** CacheSpec table applied; uv, pnpm, go, cargo,
  elan wired. Accept: cave A `uv add numpy`, cave B `uv add numpy`; B does
  zero network fetches and the cache directory grows once; same check for
  pnpm.
- **9. User env.** cave.nix built by the guest daemon at cave creation,
  profile linked into the cave home. Accept: cave start stays under 1 s
  when the env is already built; first-ever build is dominated by vsock
  substitution, not evaluation.
- **10. Lifecycle polish.** start-if-needed from `cave-vm agent`, image
  upgrade flow, crash restart, log rotation under `<state>/logs/`.

## Deletions and revisions this plan implies

- vm-cave-plan.md: Phase 3's store-sharing sketch and acceptance criterion
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
- Whether GOCACHE sharing measurably helps or just grows; drop the entry if
  it misbehaves.
- Guest volume filesystem: ext4 assumed; revisit only if reflink support
  (xfs) turns out to matter for uv/pnpm patterns hardlinks don't cover.
