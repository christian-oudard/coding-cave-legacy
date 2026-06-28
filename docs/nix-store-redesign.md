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

## The design (recommended)

The store manager (root, in the systemd unit) owns everything about sharing.
There is one shared store, owned by the manager; caves never mount it or write
it. A cave's deduplicated paths are hardlinks to root-owned inodes in the
shared store, so the cave can read and run them but, not being the owner
(mode 555), can't modify, delete, or rename them. That is the same structural
isolation today's root-owned read-only lower gives (see "Resistance to rogue
agents").

A cave runs ordinary Nix against its own store. It reads shared packages (they
are world-readable, so no ownership and no idmap are needed) and it builds its
own packages into its own store, as itself. It never modifies shared content,
so it never copies up a root-owned file, which is why the idmap disappears.
Garbage collection and any repair of shared content are the manager's job, not
the cave's. Because each cave's store is an ordinary Nix store, Nix manages its
database itself: no launch-time seed and no live host-to-cave sync.

### Deduplication

Deduplication is the manager's job, and the mechanism is hardlinks. The manager
hardlinks identical store paths across the per-cave stores and the shared store
onto a single inode — the same thing `nix-store --optimise` does within a
store. Store paths are content-addressed, so identical names are byte-identical
and safe to share an inode. That is what makes a package one copy on disk,
regardless of how each cave obtained it.

### Distribution

Caves do NOT substitute from the shared store. An unprivileged cave can't lock
a root-owned store to substitute from it (nix takes a lock under the
substituter store's `nix/var`), and making the shared store writable to caves
would be a security hole. So a cave fetches packages the normal way — the
public binary cache, or building from source — and the manager's hardlink pass
then collapses its copy onto the shared inode. A cave may re-fetch a package a
sibling already built, but it never becomes a second copy on disk. (If avoiding
those re-fetches ever matters, the place to add it is a root-side fetch the
manager performs into the cave's store — not cave access to the shared store.)

### Concurrency

There is exactly one place the manager *writes* a live cave's store: the
hardlink that collapses a cave path onto the shared inode. It holds a shared
lock on that cave's `nix/var/nix/gc.lock` while doing so. nix's garbage
collector takes that lock exclusively, so the shared lock lets the cave's
normal operations run but blocks its GC from deleting a path mid-hardlink (and
vice versa). Everywhere else the actors don't collide: each cave's own nix uses
that store's normal per-store locking, and the root-owned shared store has a
single writer (the manager).

The manager also *reads* a cave's store — to verify a candidate path and to
copy it into the shared store — but always read-only (`local?root=...&read-only=true`,
the `read-only-local-store` feature). This matters: nix opening a writable
local store as root rewrites the store directory to its canonical `root:nixbld`
1775 ownership, which would seize the store from the unprivileged user that owns
it and lock that user out of its own store (a later `nix copy` into it then
fails with EACCES on a store lock file). read-only reads without taking
ownership, so the cave store stays owned by its launching user.

After a path is deduped, the cave's copy is a hardlink to the root-owned shared
inode, so the cave can read and run it but not modify or delete it. The cave
therefore must not garbage-collect its own store; GC of both the cave stores
and the shared store is the manager's job.

### Integrity

A package only enters the shared store through promotion, gated on
content-address or trusted signature, so one cave can't plant content another
would trust. Caves never write the shared store — no mount, no substituter —
and its paths are root-owned, so a cave can't alter shared content even through
its dedup hardlink.

What this removes versus the old idmapped-overlay design: the idmapped mount and
its mirror, the dependency on the cave user's uid/gid, the launch-time DB seed
and the live host->cave sync, and in-cave copy-up of root-owned files (the
EOVERFLOW class). The only experimental Nix feature it keeps is
`read-only-local-store`, used solely host-side so the root manager can read a
cave's store without taking ownership of it — not in the cave's runtime store,
so it carries none of the stability risk that rules out the local-overlay-store
alternative below.

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

Keeping shared content under the manager closes the gap. The shared store is
root-owned and caves never mount it, so an unprivileged cave can't reach it to
alter a shared package. Dedup hardlinks a cave's path onto the root-owned
shared inode: the cave can read and run it but, not being the owner (the path
is mode 555), can't modify, delete, or rename it. The only cave-owned content
is a cave's own builds not yet promoted; once promoted, a copy becomes a
root-owned shared inode like the rest. Garbage-collecting shared paths — and
the cave stores — is the manager's job, since a cave can't delete its own
root-owned deduped paths.

What must hold for the redesign to be resistant:

- The promotion gate stays strict. Only content-addressed paths (where the name
  binds the content) or paths signed by a trusted key cross into the shared
  store. Never input-addressed paths, and never trust-local promotion in a
  fleet with untrusted agents. An agent cannot forge a content address or a
  trusted signature, so it cannot place chosen content under a path others
  trust.
- Caves never mount or access the shared store, and its paths are root-owned
  (mode 555), so a cave cannot rewrite a shared inode even through its dedup
  hardlink. Only the manager writes the shared store.
- The manager treats agent content as hostile: refuse symlinks, verify each
  candidate is a real store path, and never follow an agent-controlled path out
  of the store.

Net: the implemented design keeps today's structural isolation without the
idmap. Shared content stays root-owned and caves never touch the shared store,
so a cave can't alter shared packages; the strict promotion gate is what stops
a cave from getting forged content trusted. No immutable bit is needed (it only
broke GC, since `nix-store --gc` can't unlink immutable paths), and no daemon —
root ownership plus the gate carry the guarantee, with the one live-store touch
(the dedup hardlink) serialized against the cave's GC via its gc.lock.

## Decisions taken during implementation

- Shared store at `/var/lib/coding-cave/store`, root-owned. Per-cave stores at
  `/var/lib/coding-cave/nix-stores/agent-<uuid>`, on the same filesystem as the
  shared store so the hardlink dedup works. Both are a single source of truth
  (`store_paths.rs`) the launcher and the manager resolve through — an earlier
  split, where the launcher derived its base from `$HOME` while the manager
  hardcoded `/home/cave-coder`, made the manager watch an empty directory and
  silently disabled dedup. The per-cave base is created by the manager
  root-owned but **sticky and world-writable (mode 1777, like `/tmp`)**: nix
  stores are system-level, not per-user, so they live in `/var/lib`, not a home;
  any user that launches a cave creates its own `agent-<uuid>` store there, and
  the sticky bit stops one user removing another's. A fixed path (not a `$HOME`)
  lets the root daemon find every cave's store without knowing — or being
  configured with — which user runs caves. (There is no `cave-coder` host user;
  that name is an in-cave convention only.)
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
- The manager reads each cave store **read-only** (`&read-only=true`,
  `read-only-local-store` feature) for verify and copy-from. Found during host
  integration: nix opening a writable local store as root canonicalizes the
  store dir to `root:nixbld` 1775, seizing the cave store from its unprivileged
  owner; the owner's next `nix copy` into it then died with EACCES on a store
  lock file. A root process reading a user-owned store must read it read-only.
- Shared content is NOT marked immutable. Root ownership plus the read-only
  store bind already stop a cave from altering shared paths, and `chattr +i`
  would have blocked the GC pass from ever deleting them.

## What was built

- Cave side: each cave gets its own nix chroot store, bound at /nix/store and
  /nix/var. The env closure is `nix copy`d in at launch and GC-rooted. The cave
  does not substitute from the shared store (it fetches normally). The overlay
  mount, idmap, DB seed, and host->cave down-sync are gone, and the supervisor
  no longer mounts an overlay.
- Store manager (cav-store): promotes content-addressed / trusted-signed paths
  from each cave into the shared store, hardlink-dedups identical paths across
  the cave stores and the shared store (holding the cave's gc.lock during the
  hardlink), and GCs the shared store while rooting live caves' paths. `cav
  gc`, agent removal, and disk accounting target the per-cave store; a
  mount-table guard stops gc from unlinking a live cave's bound store.
- Host module: the unit runs the store manager; no idmapped mount.

## Known issues / integration risks

To confirm on a real host (none are exercisable inside a cave):

- Cave launch end to end: the per-cave store binds, the env-closure copy + GC
  root, and that nix inside the cave uses its own store/DB.
- The promote -> dedup -> GC loop against a live cave store, including the
  gc.lock coordination with a cave that is concurrently building or GC-ing.
- Deduped cave paths become root-owned hardlinks to the shared inode, so the
  cave can't GC its own store; confirm the manager's GC covers live cave stores
  and that nothing in the cave triggers a self-GC that would fail on them.
- Nested caves keep using the inherited /nix/store (no per-cave store one level
  down) — confirm that's acceptable.

## Deferred follow-ups (improvements, not bugs)

- Share `SHARED_STORE_ROOT`, the `local?root=` URI builder, and the gcroots
  path between the cav and cav-store binaries via a `#[path]`-included module
  (cav-rs has no lib crate, so they are duplicated with a "must match" comment
  today).
- Batch the per-path `nix copy` in promotion so a shared closure is copied once
  instead of once per path.
- Optional: a root-side fetch so a cave gets a sibling-built package without
  re-fetching it from the public cache or rebuilding (today it re-fetches, then
  the hardlink pass dedups). Only worth it if those re-fetches prove costly.
