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

## Options that share below the nix layer

Options A to D all negotiate with nix: they hand it a daemon, an overlay
lower, a pre-linked template, or an in-guest daemon, and each therefore
has to satisfy a nix invariant we do not control. That is root cause 1
above. Options E and F change the layer instead of the negotiation. Each
cave keeps an ordinary, fully private store with its own DB, so nix sees
nothing shared and no invariant applies; deduplication happens
underneath, in the storage layer the VMM already interposes on. Two
production systems have converged on this shape, which is why it is worth
recording even though neither is cheap to build.

**E. Block-level CoW under a private per-cave store (Cortex model).** The
cave's disk is a manifest of fixed-size content-addressed chunks rather
than a file. Cortex splits at 16 MiB, hashes with SHA-256, stores each
distinct chunk once, skips zeroed chunks, and serves the result to
Firecracker through a userspace NBD server; the guest sees an ordinary
ext4 disk. Reads resolve manifest to local NVMe LRU cache to blob store
on miss, so boot fetches only the working set instead of the advertised
disk size. Writes copy-on-write into private chunks, so a session that
dirties twenty chunks adds 320 MiB no matter how large the disk claims to
be. For: every cave from the same base image shares its base bytes, and
launch becomes a manifest hand-off rather than a copy, which is
requirement 3 met directly. Against: it needs a block device, so it is a
VM-only design with nothing to offer the current bwrap caves; and block
granularity dedups the *base image* well but divergence poorly, because
two caves that each install the same package independently produce
byte-identical store paths at different ext4 offsets with different inode
metadata, so the chunk hashes do not match. It is a base-image sharing
technique, not a general package-dedup technique. Cost is a chunk store,
an NBD server, a cache, and a GC over chunks.

**F. File-level lazy CAS under a private per-cave store (Modal model).**
Same idea one layer up: content-address whole files, not disk blocks.
Modal ships a roughly 5 MB index of pointers into content-addressed file
data, FUSE-mounts it in about 2 ms, and fetches file content lazily from
memory, disk, or network on access, with a writable upper for mutations.
For: file-level addressing removes E's main weakness. Nix store paths are
hash-named with deterministic content, so two caves that independently
install the same package dedup exactly, base image and runtime installs
alike. This is the best requirement-fit of any option here: instant
start, true sharing, and no nix invariant touched. Against: it is
structurally era 1's shape (read-only lower, private upper), which failed
before. The distinction that matters is that era 1's lower was the *live
mutable host store*, and the incidents came from it drifting under
running caves (host builds, GC, sibling promotions). An immutable
content-addressed lower deletes that entire class. The residual risk is
the other era 1 failure, canonicalisation-on-open provoking copy-ups,
which needs content that is already canonical (store paths are 0555
root:root, so this is close to free) and a DB pre-seeded to match the
lower. The real unknown is FUSE latency in the store read path, which is
hot on every exec; Modal's answer is aggressive local caching, and ours
would have to be measured, not assumed.

Both are large builds. Cortex and Modal each built a chunk or file store,
a cache, a fetch path, and a GC, which is a substantial subsystem and the
main argument for exhausting A to D first.

**Not the same thing: snapshot memory sharing.** Both systems also
deduplicate guest *memory*, and it is easy to conflate with the above.
Cortex keeps one sparse shared-memory file per host holding an image's
canonical memory, maps it into each guest with `MAP_PRIVATE` so clean
pages share physical backing while writes go private through kernel CoW,
and fills it on demand with `userfaultfd`: `UFFDIO_CONTINUE` for a
canonical page, which preserves the sharing, and `UFFDIO_COPY` for a page
the session has diverged. Modal's Memory Snapshots restore a container
into post-initialization memory state, skipping work like torch's 20k
file reads, for a claimed 3 to 10x faster start. This is orthogonal to
package deduplication: it addresses RAM and resume latency, not disk, and
it should not be counted toward any of the four requirements.

References: Cortex, "Snapshots, copy-on-write, and the economics of agent
sandboxes" (2026-07-23); Modal, "Fast, lazy container loading" (2024-09-08)
and the Memory Snapshots guide.

Orthogonal to all six, and worth doing regardless: stop shipping the
nixpkgs checkout as a 52k-file tree in the cav runtime closure, and slim
the sandbox closure (glibc-locales, git-with-python) which pads every
seed, template, and lower.

The locale and git halves of that landed in 972ed93 (978MB to 526MB).
The nixpkgs half did not, and the attempt is worth recording.
`build.json` embeds `nixpkgsUrl = "path:${nixpkgs}"`, a literal store
path that nix's reference scanner turns into a runtime dep of cav, so
`populate_cave_store` copies the checkout into every cave: measured, 467M
of the closure's 816M on disk. (188MB of content; `du` reports 467M
because roughly 52k small files hit the 4K block floor.) Rev-pinning that
URL removes the reference but not the checkout, because `lib/sandbox.nix`
holds a second and deliberate one, `NIX_PATH=nixpkgs=${nixpkgs}`, which
is what gives caves offline `<nixpkgs>`. Rev-pinning alone therefore buys
nothing on disk while trading a guaranteed-offline user-env build for a
network-resolvable one, so it was tried and reverted.

What remains is a real trade, recorded here undecided. The unexplored
option is to bind the host's nixpkgs read-only at a *non-store* path and
point NIX_PATH there: 467M per cave saved with offline evaluation
preserved, and it avoids the era 1 failure class specifically by staying
out of `/nix/store`, where nix's GC deletes directory entries it cannot
reach from a root. Its costs are a host gcroot, which if botched strands
running caves (era 3's stranding class in miniature), and a host-to-cave
shared path in a bwrap design that is deliberately being kept minimal and
correct rather than clever while the VM work is still distant. 972ed93
weighed only one alternative, a lazy tarball, and rejected it for trading
disk against a per-cave refetch; the bind option was never considered.

One fix did land. The cave's flake registry was left at nix's global
default, so `nix shell nixpkgs#pkg`, the workflow the project documents,
resolved through channels.nixos.org and unpacked a *second* nixpkgs into
a per-cave tarball cache while the seeded copy went unread. Pinning the
registry to the same tree NIX_PATH uses cut that from 144MB of
`~/.cache/nix` per cave to 68KB, measured on a cold cache.

Worth recording as method: the per-cave cost turned out to be two
independent references plus a stale registry default, and no sharing
architecture would have addressed any of them. Measure the closure, and
measure what actually reads it, before designing a mechanism to share it.

A cross-cutting constraint from 004fef4: neither hardlinks, daemon sockets,
nor overlayfs cross a VM boundary cleanly, so options A to C are
container-world designs. Option D avoids the boundary problem by putting
the entire sharing domain (daemon, store, caves) on one side of it and
accepting exactly one host/guest duplication.

E and F answer that constraint differently: rather than moving the sharing
domain to one side of the boundary, they make the boundary itself the
sharing mechanism, since the VMM already interposes on every guest read.
That is also why they do not degrade to a container design. Without a
block device (E) or a mountable index (F) there is nothing to interpose
on, so neither offers anything to the current bwrap caves.
