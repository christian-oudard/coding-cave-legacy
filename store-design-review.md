# Nix store sharing: commit review and root-cause analysis

Status: review. No decision is taken here; options and trade-offs are at the
end.

## Requirements

As now stated, all four at once:

1. Fast startup. No multi-second work at cave launch.
2. No duplication on disk. One copy of a package across host and all caves.
3. No copying. Sharing must not re-stream bytes that are already on disk.
4. Normal-ish dev environment. An agent installs packages with ordinary nix
   commands and they work.

The current design meets 2 and 4, meets 1 only on resume, and fails 3 by
design (it copies first, then deduplicates the copies away).

## Timeline

Six weeks, three architectures, five production incidents.

### Era 1: idmapped overlay of the host store (May 24 to Jun 22)

The cave's /nix/store was a kernel overlayfs whose lower layer was the live
host /nix/store through an idmapped bind; agent installs copied up into a
private upper. Zero-copy reads, no duplication, normal nix. It met
requirements 1 to 3 natively and failed on coherence and identity:

- 48a1e62 introduced the idmapped mount. It relabeled root-owned paths to the
  cave user so nix's canonicalisation and GC chmods would not EPERM.
- dd5abb9: the idmap option fields were applied inverted (the kernel's syntax
  is documented backwards), producing overflow uids and EOVERFLOW failures
  mid-substitute. The mount looked healthy in findmnt either way.
- 7ce8d6d added a root daemon that promoted agent installs into the host
  store itself, so siblings inherit them through the shared lower.
- 468cfe7: the host store changes under a running cave (host builds, GC,
  sibling promotions), so the cave's one-shot DB seed drifted; nix saw
  on-disk paths it had no record of, tried to delete them, triggered
  copy-ups, and failed. A live host-to-cave DB down-sync was added. The same
  class of problem later stranded a running cave's DB mid-session (the
  "big-lock" failures).
- 7fa1b96: red-teaming found the promotion gate was self-referential (the
  agent controls the DB the gate checked against), so a malicious agent
  could plant forged content under a path a sibling would trust. The gate
  was rebuilt around content addresses and trusted signatures.

Lesson recorded in nix-store-redesign.md: an unprivileged cave doing
privileged-shaped work (owning copied-up store paths, holding a DB the host
also writes) is fragile in proportion to the machinery propping it up.

### Era 2: per-cave stores with a shared-store substituter (Jun 22 to 28)

- 49bbba6 rewrote sharing around ordinary per-cave chroot stores. A root
  manager (cav-store) promotes gated packages into a root-owned shared
  store; caves were to substitute from it. This removed the idmap, the DB
  seed, and the live sync in one stroke.
- 3d7b6f3: first-launch deadlock on a fresh host (bind sources that only a
  first promotion would have created).
- 4057245: the substituter was dropped days later. Nix write-locks a
  substituter store's nix/var, which an unprivileged cave cannot do against
  a root-owned store, and making it writable is a security hole. With the
  substituter gone, caves re-fetch from the public cache and the launcher
  nix-copies the base closures into every new store. This is the moment
  requirement 3 (no copying) was silently given up.
- 98d681a extended the launch copy to the cav package closure itself,
  which is what the launcher, hooks, and comms need inside the cave.

### Era 3: converge-and-repair (Jun 28 to Jul 3)

The dedup daemon matured through incident response:

- f594889 / d50e9db: nix opening a writable local store as root
  canonicalises its ownership to root:nixbld, seizing the cave store from
  its owner; later launches died with EACCES. The read-only-store flag does
  not prevent this as real root; only dropping privileges does.
- 8307709: a host disk filled to 100%. The shared GC, reading a cave DB with
  a hot WAL, collected in-use paths on partial knowledge; re-import under a
  new inode stranded every cave hardlinked to the old one on private
  copies. Promotion was also one-shot and the host store was not part of
  the dedup tree. Fix: idempotent three-level converge, fail-closed GC.
- 45cdd7e: converge promoted transient build inputs the cave itself would
  GC, creating a delete/re-import cycle churning gigabytes per hour.
- ec1b8ae: NixOS's read-only bind of /nix/store onto itself made link()
  refuse with EXDEV, so every host-present package existed twice (about 10G
  on the deployed host); fixed by detaching the bind in a private mount
  namespace.
- eb59bb0, e49f123, f3da89c: .drv import failures, DB-valid-but-missing
  repair, log hygiene.

Meanwhile 004fef4 notes the VM-per-cave plan has the same problem in harder
form: hardlinks cannot cross a VM boundary at all.

## Why we failed

1. **We built around nix instead of with it.** Nix ships exactly two
   mechanisms for "many mutually untrusted consumers, one store": the
   multi-user daemon, and the local-overlay-store. We disqualified both up
   front (daemon: sandbox hole; overlay store: experimental) and then spent
   six weeks hand-building a third mechanism out of idmaps, DB seeds, live
   syncs, substituters, hardlink converge passes, and privilege drops. Each
   piece collided with a nix invariant we did not control: canonicalisation
   on open, substituter locking, GC locking, DB coherence, reference
   scanning.

2. **Each era optimised away the previous failure, not toward the
   requirements.** Era 1 failed on coherence, so era 2 bought coherence by
   giving up zero-copy. The substituter failed on locking, so we bought
   isolation by giving up sharing-on-fetch. Disk filled, so converge bought
   back the disk but not the copies. No step re-derived the design from the
   full requirement set; requirement 3 was dropped in 4057245 without being
   restated as a cost.

3. **The quarantine boundary and the sharing goal pull in opposite
   directions, and we kept moving the shared mutable state instead of
   eliminating it.** Zero-copy sharing needs shared state (store bytes plus
   validity metadata). Isolation forbids the cave from holding it. Era 1
   put it in the cave (DB seed plus sync: incoherent), era 2 put it in a
   store the cave locks (substituter: impossible unprivileged), era 3 put
   it in a root daemon that reconciles after the fact (correct, but the
   reconciliation is what makes launches copy).

4. **Nix's trust model was learned by incident, not read up front.** The
   inverted idmap syntax, canonicalisation-on-open seizing stores, the
   substituter lock, EXDEV through the read-only bind, and reference-scanned
   flake URLs in build.json (which drag a 467MB nixpkgs checkout with 52k
   files into every launch copy) were each discovered in production. All are
   documented nix/kernel behaviors.

## Where the launch time goes today

Measured on a fresh agent: 8.6s of a 9.4s launch is populate_cave_store
nix-copying the cav closure (980MB, 159 paths) plus the user env into an
empty per-cave store. 3.4s of that is the pinned nixpkgs source tree alone.
A warm resume costs 25ms. A root-side hardlink clone of the same store
costs 0.7s. So the cost is not the sandbox, the git plumbing, or nix
evaluation; it is exclusively re-streaming bytes the disk already holds,
because the launcher lacks the privilege to link to them.

## Options

**A. Shared nix daemon.** Bind the host (or a fleet-dedicated) nix daemon
socket and /nix/store read-only into every cave. This is nix's native
multi-user model: instant start (nothing to seed), one store (nothing to
deduplicate or converge; the daemon and cav-store machinery largely
disappear), zero copies, and the most normal dev environment possible.
Costs: the daemon socket is a privileged, root-backed attack surface
reachable by a hostile agent (the daemon protocol is battle-tested but has
had CVEs); builds and fetches execute host-side as nixbld outside the bwrap
boundary, so cave egress policy no longer covers package fetches; per-cave
GC rooting via profiles. This deletes the most code and adds the most trust
surface.

**B. local-overlay-store.** Each cave's store is nix's experimental
local-overlay-store: lower is the root-owned shared store mounted
read-only, upper is the cave's private store; nix manages the merged DB.
Zero-copy reads, no duplication for anything shared, instant start (an
empty upper is a valid store), normal nix inside the cave, and the
quarantine model is unchanged (promotion into the lower stays gated and
root-mediated). Costs: an experimental feature that could change under us;
the lower must not lose paths while caves reference them, so shared-store
GC becomes generational (collect only into a new lower, or only when no
caves are live). The stability objection recorded in nix-store-redesign.md
(changing a live lower) applies to *removals*; append-only lowers avoid it.

**C. Root-side template seeding.** Keep the current architecture; cav-store
maintains a pre-linked template store per (cav closure, user env) pair and
clones it (hardlinks plus a copied DB) into place at launch, or on request
over a small socket. Startup drops to well under a second; disk stays
deduplicated. Costs: it is still the converge architecture with all its
moving parts, "no copying" is met only in spirit (DB files still copy), a
template invalidates on every cav or user-env rebuild, and agent-fetched
packages still arrive as private copies that converge collapses later.
Smallest change, weakest fit to the stated requirements.

**D. One VM for all caves, shared nix daemon inside.** The vm_cave branch's
settled direction (one long-running VM, all caves as bwrap sandboxes inside
it, crinj and git approval on the host) changes the trust calculus that
disqualified option A. With a fifth requirement on the table, no host
escape for a compromised agent, the VM boundary becomes the load-bearing
isolation: an agent that defeats bwrap and even gains guest root is still
inside the guest, and the host surface shrinks to the VMM's virtio devices.
That makes a root nix daemon *inside the guest* acceptable, since its blast
radius is already contained. All caves talk to one in-guest daemon over the
socket: instant startup, one in-guest copy, zero copying, fully normal nix,
and the promotion gate / converge machinery (the source of most incidents
above) is no longer what carries the security guarantee and can largely be
deleted. Costs: the guest store duplicates host-present packages once,
total (hardlinks cannot cross a kernel boundary, so two copies is the floor
for any VM design; better than N copies, worse than one). Re-downloads are
avoidable by projecting the host store read-only over virtio-fs and using
it as a local substituter (read-only store, no lock taken), so the second
copy is made at disk speed, not network speed. One flag: the current plan's
RW virtio-fs of the host home is a host-write channel that undercuts the
boundary (guest root writing ~/.zshrc is host code execution); it should
become one-way materialization with explicit copy-out, as project worktrees
already are via the approval gate.

Orthogonal to all four, and worth doing regardless: stop shipping the
nixpkgs checkout as a 52k-file tree in the cav runtime closure (reference
it as a tarball or lazily), and slim the sandbox closure (glibc-locales,
git-with-python) which pads every seed, template, and lower.

A cross-cutting constraint from 004fef4: neither hardlinks, daemon sockets,
nor overlayfs cross a VM boundary cleanly, so options A to C are
container-world designs. Option D avoids the boundary problem by putting
the entire sharing domain (daemon, store, caves) on one side of it and
accepting exactly one host/guest duplication.
