# Nix store sharing: redesign

Status: implemented on this branch, pending host integration testing. The
cave-launch and store-manager-daemon paths build and pass unit/integration
tests but cannot be exercised from inside an unprivileged cave; the items
under "Known issues / integration risks" are what to confirm (or fix) on a
real host. This document is the design rationale; the decisions taken during
implementation are recorded below.

## Why change anything

Caves share Nix packages with each other and the host so a package lives once
on disk instead of once per agent. The current implementation does this with a
per-cave overlay filesystem whose lower layer is the host's root-owned
`/nix/store`, exposed through an idmapped mount, plus a database seeded into
each cave at launch and a live two-way sync that keeps the cave's database
current.

That machinery is the source of the problems we hit:

- The idmapped mount has to relabel the root-owned store so the unprivileged
  cave can take ownership of paths it copies up. It assumed the cave user's
  group id was 1000. On a host where it is not (NixOS gives users the shared
  `users` group, gid 100), every store path showed up as an unmapped group
  inside the cave, and the first `chmod` Nix does during a build failed with
  "Value too large for defined data type" (EOVERFLOW).
- The live host-to-cave database sync, and `cav gc`, mutate resources a running
  cave holds open. That is how a cave's Nix database got stranded mid-session
  and every Nix call started failing with "big-lock: No such file or
  directory" until the cave restarted.

The underlying issue: the unprivileged cave does privileged-shaped work. It
owns and copies up store paths, and it holds a bind-mounted database that the
host also writes. That is what forces the idmap (and with it a dependency on
the host user's identity) and the fragile live sync.

## The principle

The cave should see an ordinary Nix store and do nothing special with it. All
of the sharing and deduplication complexity lives outside the cave, in a
privileged store manager. The cave never needs to own store files, never does
privileged mounts, and never needs to know anything about the host user's
identity.

## The design (recommended: no experimental features)

The store manager (root, in the systemd unit) owns everything about sharing.
There is one shared store, owned by the manager and mounted read only into
caves, and every shared package in it is marked immutable. A cave can read a
shared package but can never modify, delete, or rename it, even though the cave
can write its own store directory. That is the same structural isolation
today's root-owned read-only lower gives, kept on purpose (see "Resistance to
rogue agents").

A cave runs ordinary Nix against its own store. It reads shared packages (they
are world-readable, so no ownership and no idmap are needed) and it builds its
own packages into its own store, as itself. It never modifies shared content,
so it never copies up a root-owned file, which is why the idmap disappears.
Garbage collection and any repair of shared content are the manager's job, not
the cave's. Because each cave's store is an ordinary Nix store, Nix manages its
database itself: no launch-time seed and no live host-to-cave sync.

Deduplication and propagation are entirely the manager's job, and the mechanism
is interchangeable. Both are fine, and we can pick at build time:

- Hardlinks. The manager hardlinks identical store paths across the shared
  store and the per-cave stores (all on one filesystem) and marks them
  immutable. This is the mechanism `nix-store --optimise` already uses. Store
  paths are content-addressed, so identical names are byte-identical and safe
  to share an inode. One copy on disk.
- Overlay. The manager mounts each cave's store as an overlay with the shared
  store as a read-only lower and a per-cave upper. Shared packages are read in
  place, and the cave's own builds land in its upper. The cave must never
  trigger a copy-up of a shared path, which holds because GC and repair are the
  manager's job.

How a cave gets a shared package without re-downloading: it substitutes from
the shared store (the manager keeps it configured as a substituter), and Nix
verifies the signature or content-address on the way in, which is the integrity
gate with no custom code. The manager then collapses the copy to a shared,
immutable inode.

What this removes:

- The idmapped mount and the `/var/lib/coding-cave/nix-store` mirror.
- The dependency on the cave user's uid and gid.
- The launch-time database seed and the live host-to-cave database sync.
- In-cave copy-up of root-owned files, and with it the EOVERFLOW class.
- Any dependency on experimental Nix features.

What stays:

- A privileged store manager, now with a single job: watch caves, promote
  packages worth sharing into the shared store, and keep the hardlinks current.
- Per-cave isolation. Each cave has its own store and its own database, so it
  can only see and use packages registered in its own store. Sharing a package
  to another cave is a deliberate, verified act by the manager, never ambient
  access to another cave's store.
- The integrity guarantee. A package only crosses to another cave or the host
  through substitution, which verifies it against its recorded hash or
  signature.

## Trade-offs of the recommended design

- More copying than an overlay. Substitution copies a package's bytes into the
  cave's store, and the hardlink pass then collapses duplicates to one inode.
  An overlay reads a shared lower in place with no copy. End-state disk is the
  same; the cost is transient IO, once per package per cave. Bandwidth is still
  saved, because substitution comes from the local shared store rather than the
  network.
- The store manager owns the cross-store hardlinking. `nix-store --optimise`
  deduplicates within a single store; deduplicating across the per-cave stores
  is a small amount of manager logic (walk identical-named paths, hardlink
  them). It is mechanically simple but it is ours to maintain.

Notably, this design avoids the live-visibility problem that the overlay
approach has (see below): there is no shared lower changing under an open
store, so a running cave can pull a newly-shared package through normal
substitution at any time.

## Alternative: Nix local-overlay-store (uses an experimental feature)

Nix has an experimental store type, `local-overlay-store`, that layers an upper
store over a lower store on an OverlayFS mount and manages the combined
database itself. A cave's store would be a local-overlay-store with the lower
being a shared cave-owned store and the upper being the cave's private layer.
This also removes the idmap (the lower is cave-owned) and the hand-rolled
database sync (Nix manages the union), and it reads shared packages in place
with no copy.

Reasons not to lead with it:

- It is an experimental feature, flagged by Nix as subject to change, replacing
  plain stable overlayfs.
- It expects the lower to be stable. Promoting a package into the lower while a
  cave already has the store open may not be picked up by that running cave, and
  changing an overlay lower under a live mount is the same class of problem as
  today's live sync. The recommended design does not have this problem.

Its one advantage over the recommended design is zero-copy in-place reads of
shared packages. Worth revisiting only if that IO cost turns out to matter.

## Requirements either design meets

- Deduplicate packages. One copy on disk, shared by all caves. Sharing happens
  promptly (the manager promotes as packages appear), so duplicate disk does
  not wait for a cave to exit.
- No arbitrary writes between agents and the host. A cave writes only into its
  own store. Sharing happens only through the manager and only for content
  verified against its hash or signature. A cave never writes another cave's
  store or the host's.

## Resistance to rogue agents

Two threats to keep separate.

First, sandbox escape to host code execution. That surface is the bwrap
namespace, seccomp, and mount configuration, not the store, and this redesign
neither widens nor narrows it. The one store-related caveat is the privileged
store manager: it runs outside the cave and processes content an agent
produced, so a bug in it, or in the Nix it shells out to, is an escalation
path. That is equally true of the current design.

Second, cross-cave or host poisoning: a rogue agent getting malicious content
into a place another cave or the host will trust and run. This is the threat
the store design has to defend, and where the redesign needs care.

Keeping shared content under the manager closes the gap. The current shared
lower is root-owned and read only, so an unprivileged cave physically cannot
alter a shared package; that structural isolation is worth preserving. The
redesign preserves it by keeping shared content owned by the manager (root) and
marked immutable. The cave is not the owner and cannot clear the immutable bit
(that needs CAP_LINUX_IMMUTABLE, which the cave's user namespace does not hold
over a host-mounted filesystem), so it cannot modify, delete, or rename a
shared package even though it can write its own store directory. The only
cave-owned content is a cave's own builds that have not been promoted yet; once
the manager promotes one it becomes a shared, immutable, manager-owned inode
like the rest. Garbage-collecting shared paths is the manager's job (clear the
bit, delete), which fits its role.

What must hold for the redesign to be resistant:

- The promotion gate stays strict. Only content-addressed paths (where the name
  binds the content) or paths signed by a trusted key cross into the shared
  store. Never input-addressed paths, and never trust-local promotion in a
  fleet with untrusted agents. An agent cannot forge a content address or a
  trusted signature, so it cannot place chosen content under a path others
  trust.
- Shared paths are immutable, as above, so a cave cannot rewrite a shared inode
  after the hardlink.
- The shared store is read only to caves. Only the manager writes it.
- The manager treats agent content as hostile: refuse symlinks, verify each
  candidate is a real store path, and never follow an agent-controlled path out
  of the store.

Net: the redesign can be made as resistant as today, but not for free. Today's
root-owned read-only lower enforces shared-package integrity in the kernel with
no extra code. The cave-owned-store simplification moves that guarantee into
manager logic (immutability plus a strict gate), which is more to get right.
The daemon variant, where the cave delegates every store write and never
touches shared content directly, is the most resistant of the three, at the
cost of more moving parts. This is a real security-versus-simplicity trade to
make consciously.

## Decisions taken during implementation

- Shared store at `/var/lib/coding-cave/store`, root-owned, on the same
  filesystem as the per-cave stores (`<data>/store/agent-<uuid>`) so hardlink
  dedup works. The store manager (cav-store, root) writes it.
- Integrity gate: promotion crosses only content-addressed or trusted-signed
  paths (`nix store verify --sigs-needed 1`, which accepts CA paths with no
  signature). The cave's own nix runs with `require-sigs = false` so its
  unsigned from-source builds aren't blocked; shared content is protected by
  the promotion gate plus root-owned store paths.
- "Worth sharing": every newly-valid path in a cave's store is a promotion
  candidate; the gate decides what actually crosses. The GC pass re-roots all
  live caves' paths and collects the rest hourly.
- The store manager still runs as root (writes the root-owned shared store,
  reads each cave's DB); it no longer needs the idmapped mount.
- Shared content is NOT marked immutable. Root ownership plus the read-only
  store bind already stop a cave from altering shared paths, and `chattr +i`
  would have blocked the GC pass from ever deleting them.

## What was built

- Cave side: each cave gets its own nix chroot store, bound at /nix/store and
  /nix/var; the env closure is `nix copy`d in at launch and GC-rooted; the
  shared store is configured as a substituter. The overlay mount, idmap, DB
  seed, and host->cave down-sync are gone, and the supervisor no longer mounts
  an overlay.
- Store manager (cav-store): promotes verified paths into the shared store,
  hardlink-dedups them, and GCs the shared store while rooting live caves'
  paths. `cav gc`, agent removal, and disk accounting target the per-cave
  store; a mount-table guard stops gc from unlinking a live cave's bound store.
- Host module: the unit runs the store manager; no idmapped mount.

## Known issues / integration risks

To confirm or fix on a real host (none are exercisable inside a cave):

- Substituter locking. nix locks the substituter store's nix/var while
  substituting, so the shared store is bound with nix/store read-only but
  nix/var read-write; a fully read-only bind fails with "big-lock: Permission
  denied". The writable nix/var lets a cave write the shared store's metadata
  (db/temproots) — a DoS surface, not content poisoning (paths stay
  root-owned). A binary-cache server (nix-serve / file:// cache) would give
  read-only substitution with no writable shared state; revisit if untrusted
  agents matter.
- Cave launch end to end: the per-cave store binds, the env-closure copy + GC
  root, and that nix inside the cave uses its own store/DB.
- The promote -> hardlink -> GC loop against a live cave store.
- Nested caves keep using the inherited /nix/store (no per-cave store one level
  down) — confirm that's acceptable.

## Deferred follow-ups (improvements, not bugs)

- Share `SHARED_STORE_ROOT`, the `local?root=` URI builder, and the gcroots
  path between the cav and cav-store binaries via a `#[path]`-included module
  (cav-rs has no lib crate, so they are duplicated with a "must match" comment
  today).
- Batch the per-path `nix copy` in promotion so a shared closure is copied once
  instead of once per path.
- Minor fail-loudly: `verify_authentic(...).unwrap_or(false)` swallows a spawn
  error (only bites with `PROMOTE_TRUST_LOCAL=1`); the substituter is skipped
  silently when the shared store dir is absent rather than warning.
