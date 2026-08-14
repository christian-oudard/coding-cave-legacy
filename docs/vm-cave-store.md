# VM cave: host store passthrough review

Reviews the store-sharing portion of the since-deleted bring-up plan (its
Phase 3 and "Store sharing" carry-over conclusion) against the nix-store
redesign that landed on `main` after the plan was written. See
`docs/nix-store.md` and `docs/nix-store-redesign.md` on `main` for the
current design this review takes as given.

Status: resolved. `vm-cave-design.md` took the decision, adopting option B in
on-demand form: the host projection became a binary cache served over vsock
(`internal/nixcache`) rather than a mounted tree, so the manifests, want-lists
and sqlite crossing the boundary that this review worries about are all
avoided. Options A and C were rejected or deferred as written, and option D's
shared-store projection is unnecessary because point queries already prevent
enumeration.

Retained for the trade-off analysis that produced that answer.

Its premises have also moved on. This review takes the three-level hardlink
dedup tree and its `cav-store` daemon as the current design, but that whole
subsystem was removed in 5ef19eb; each cave now has a private chroot
store and nothing is shared. Read `store-design-review.md` first for why
it was removed and for the current option set. What survives here and is
still load-bearing is the analysis of the VM boundary itself: the five
facts below (EXDEV, bytes-visible-is-not-paths-valid, no cross-boundary
sqlite, references do not pin bytes, the design recurses inside the VM)
constrain any future design, and option D's argument for projecting the
shared store rather than raw `/nix/store` is a privacy argument that does
not depend on the removed machinery.

## What the plan says today

The plan carries this conclusion from the Nix exploration:

> Store sharing: virtio-fs of host `/nix/store`, RO, with a writable
> upper-layer overlay backed by a per-VM volume.

and Phase 3's acceptance criterion is "a host-built derivation visible
from inside the guest."

Both predate the store redesign. Two assumptions no longer hold:

1. The plan assumes visibility is availability. It is not: nix decides
   path validity from database rows, not from bytes on disk. A host
   store path visible through a mount, with no row in a DB the guest's
   nix consults, is invisible to substitution. Nix will re-download it,
   and under an overlay the download shadows the identical lower copy.
   Phase 3's acceptance test would pass while delivering nothing.
2. The plan predates the three-level dedup tree, whose sole mechanism
   is hardlinks. Hardlinks do not cross the VM boundary, so the
   redesign's host-facing half does not port.

## What changed on main since the plan was written

The store subsystem is now a three-level tree, deduplicated by
hardlinks, converged by a root daemon (`cav-store`):

- level 0: host `/nix/store`, trusted, read-only to the daemon.
- level 1: shared store (`/var/lib/coding-cave/store`), root-owned
  quarantine. Cave content enters only through a trust gate
  (content-addressed or trusted-signed paths).
- level 2: per-cave chroot stores. Caves fetch normally (binary cache
  or build); the daemon collapses duplicates afterward.

Properties the VM design must preserve, because the redesign exists to
provide them:

- One copy of a package on disk across host and all caves.
- Quarantine: nothing an agent produces reaches a store the host
  trusts, and the gate runs outside anything an agent controls.
- Self-healing idempotence: no invariant that inodes never move. The
  previous design's stranding class (a moved inode leaving caves on
  dead private copies until the disk filled) must not come back.
- Host GC needs no coordination today *because hardlinks pin inodes*.
- No experimental nix features. Fail loudly.

## Facts the VM boundary imposes

These constrain every option below.

1. **`link()` across the boundary is EXDEV, always.** The guest's
   virtio-fs mount and its VM volume are different superblocks under
   different kernels. The daemon's mount-namespace trick solves
   same-filesystem-different-mount; this is genuinely different
   devices. Hardlink dedup between host and VM is physically out.
   The existing `HOST_RELINK_DISABLED` fallback would run, but its
   outcome is every host-present package fully duplicated on the VM
   volume, which is the disk cost this subsystem exists to avoid.
2. **Bytes visible is not paths valid.** Any zero-copy design is
   really a DB-registration design. This is exactly what nix's
   experimental `local-overlay` store type is, and why it exists.
3. **No sqlite across the boundary.** Nix DBs run in WAL mode; WAL
   coordination assumes one kernel (shared-memory file, fcntl locks).
   A design where one side opens the other side's live nix DB through
   virtio-fs is a correctness bug, not a performance cost. All
   cross-boundary metadata must be plain files written by one side,
   atomically renamed, read by the other: manifests, want-lists,
   root-lists.
4. **References do not pin bytes across the boundary.** Host GC is
   coordination-free today because the shared store's hardlink keeps
   the inode alive. A guest referencing host bytes through virtio-fs
   holds no link; a host `nix-collect-garbage` would delete them out
   from under it. Zero-copy schemes therefore need explicit
   cross-boundary GC roots (published fail-closed, like the existing
   rooting pass) plus a self-heal path (re-fetch when a lower path
   vanishes) as backstop.
5. **Inside the VM, the current design recurses unchanged.** The VM
   volume is one filesystem. Per-cave stores, a guest shared store,
   and the guest's own system store all live on it; `cav-store` runs
   as root inside the guest verbatim, hardlinks and all. The user-level
   premise "cave-shared store deduping with agents works fine in the
   VM" is confirmed. The open problem is only the outer edge:
   host filesystem to VM volume.

## The outer edge has three jobs

- **Provisioning.** Get host-present bytes into guest cave stores
  without the network. Today `seed_cave_closure` copies the cav and
  user-env closures into each cave store at launch; a live cave's
  working set measures about 5 GB.
- **Disk.** Bound duplication across the boundary.
- **GC coherence.** Whichever side holds bytes the other references
  must not drop them silently.

## Options

### A. Cave state stays on the host, virtio-fs RW into the guest

Cave stores and shared store remain host directories; the guest mounts
the tree read-write; the host daemon runs unchanged, all hardlinks on
one kernel.

- For: zero new dedup machinery.
- Against: the guest's nix writes its cave DBs over virtio-fs while
  the host daemon reads them, violating fact 3 in the hot path
  (converge and GC rooting read every cave DB). Build workloads
  (many small files, fsync-heavy) are virtio-fs's worst case. The
  guest-owner-uid privilege drop the daemon does when reading cave
  stores now crosses a uid-mapping layer.
- Verdict: rejected unless cross-kernel WAL over virtio-fs is shown
  safe, and it has not been.

### B. Copy-based: the VM volume owns everything; the host tree is a seed source

The guest runs the full current subsystem on the VM volume. A host
tree is shared RO purely as a local substitution and seed source:
`seed_cave_closure` copies from the share at launch instead of over
the network, and optionally caves check the share before the binary
cache. Guest converge collapses every copy to one inode per path on
the VM volume, exactly as on a host today.

- For: no new invariants. No DB registration, no overlay, no
  cross-boundary GC coupling needed for correctness (a vanished share
  path degrades to a cache fetch). The shipped design moves into the
  VM untouched, which fits the plan's phase discipline.
- Against: one full copy of the union working set on the VM volume,
  duplicated against the host. Order 5 to 15 GB. Note the plan's
  one-VM-for-everyone decision makes this per host, not per cave; a
  sparse disk image plus discard keeps it bounded.

### C. Zero-copy: overlay lower plus validity registration

The guest shared store's `nix/store` becomes
overlayfs(lower = RO share, upper = VM volume). The guest daemon
registers lower paths as valid in guest DBs, fed by a host-published
manifest (plain files, per fact 3). Cave stores overlay the merged
view the same way, or nix's experimental `local-overlay` store does
both halves.

- For: host-present paths cost roughly zero VM disk. virtio-fs DAX
  makes lower reads near-native and shares the host page cache.
- Against: this is structurally the pre-redesign architecture (overlay
  lower, DB seed, live lower mutation) that was removed for cause,
  with a kernel boundary added. Of the original objections: the
  idmap/uid coupling is moot inside a VM where we are privileged; the
  live-lower-mutation class is fully alive, since host GC and
  `nix-store --optimize` move inodes under the share, and virtio-fs
  cache coherence over a mutating lower is an additional risk on top.
  Requires cross-boundary gcroot publication (fail-closed) plus
  re-fetch self-heal. The `local-overlay` variant additionally breaks
  the no-experimental-features requirement; hand-rolling avoids the
  flag but re-owns the complexity nix would otherwise own.
- Verdict: an optimization to reach for only if B's disk cost proves
  to matter in practice. Its correct failure semantics degrade to B
  (self-heal by copying), which is another reason to build B first.

### D. Orthogonal to B vs C, and applicable to both: project the host *shared store*, not host `/nix/store`

Whatever the consumption mechanism, the tree shared into the guest
should be `/var/lib/coding-cave/store`, not `/nix/store`:

- It already contains exactly the cave-relevant packages, hardlinked
  onto host inodes (so it costs the host nothing extra) and
  root-owned.
- Privacy: sharing raw `/nix/store` lets any agent enumerate every
  package on the host. Today a cave sees only its own store. The
  shared store restores that property in the VM world.
- GC: the host daemon already roots every path caves reference before
  collecting. Extending that root set with VM-published root lists is
  the same machinery and the same fail-closed policy.
- Miss path: a guest cave wants a host-present path not yet in the
  shared store. The guest publishes a want-list (plain file, per
  fact 3); the host daemon imports host to shared, which is converge
  step 1 verbatim (trusted source, no gate); the path appears in the
  share within a converge interval. Idempotent and self-healing, and
  the trust gate never leaves the host.
- Quarantine improves. With a guest-local shared store as the terminal
  level for cave-originated content, nothing an agent produces ever
  crosses to the host at all. Today it crosses into a host-side store
  through the gate; in this design the VM boundary and the trust
  boundary coincide.

One consequence for the guest OS itself: if the guest *system* store's
lower were the raw host store share (the microvm.nix pattern the plan
sketched), the privacy leak returns through the guest's own root
filesystem. Either build the guest system closure into a self-contained
image, or have the host daemon import the VM system closure into the
shared store (trusted, host-built, no gate) and boot the guest from
that share.

### Dismissed

- **Block-level dedup by the host filesystem** (reflink or ZFS/VDO
  between host store and the VM disk image): the guest filesystem inside
  the image is opaque to host-side file dedup, store files are small and
  not block-aligned, and pool-wide dedup is heavy and off-stack.

  This dismissal is narrower than it first reads, and does not cover the
  Cortex/Modal design. It rejects having *the host filesystem* dedup an
  image it cannot interpret. It does not reject having *the VMM serve*
  the disk from a content-addressed chunk store, where the base image is
  shared by construction rather than discovered by scanning, and the
  opacity objection therefore never arises: we choose the chunk
  boundaries, so nothing has to be inferred from the guest's ext4
  layout. That variant is option E in `store-design-review.md`, along
  with the file-level (FUSE/CAS) variant F, which is the better fit for
  nix because it dedups identical store paths across caves regardless of
  block placement. Both remain expensive to build and neither is
  recommended ahead of the options above; they are recorded as live
  candidates, not as dismissed ones.
- **Guest substitutes straight from the host store** as a
  `local?root=...&read-only=true` substituter: reads the host DB
  across the boundary (fact 3), and the redesign already found the
  read-only local store insufficient once.

## Recommendation

D plus B as the shipping design: the guest runs the current subsystem
unmodified on the VM volume; the host shared store is projected RO as
the seed and substitution source; want-lists and root-lists as plain
files are the only cross-boundary protocol. Revisit C only with a
measured disk problem, and build it as an optimization over B so its
failure mode is B.

Strongest case against: it accepts a real extra copy of the working
set per host, and the want-list adds up to a converge interval of
latency before a host package becomes locally available to the guest.
If either is unacceptable from day one, C is forced, and its
live-lower risks have to be engineered through rather than avoided.

## Impact on the bring-up plan (since deleted)

- The "Store sharing" carry-over conclusion should name the host
  shared store, not host `/nix/store`, as the shared tree.
- Phase 3's acceptance criterion tests the wrong thing. Replace
  "a host-built derivation visible from inside the guest" with:
  a guest cave installs a host-present package with networking
  disabled, and it costs no network fetch (B), or additionally no
  VM-volume bytes (C).
- A phase is missing between Phase 5 and Phase 8: bring up
  `cav-store` inside the guest (the recursion from fact 5), then the
  outer-edge protocol (seed source, want-list, root-list).
- The guest system store choice interacts with the privacy point in
  option D and should be decided with it.
