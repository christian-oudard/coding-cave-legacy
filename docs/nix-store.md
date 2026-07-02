# Nix Store Inside the Cave

Agents can install packages with `nix shell`, `nix develop`, etc. This
document specifies what the nix store system must do and explains how
it works. See `nix-store-redesign.md` for the rationale behind the
current design and the alternatives that were rejected.

## Requirements

- A package is stored once on disk and shared by the host and every cave.
  Installing the same package in ten caves, or a package the host already
  has, must not cost ten (or eleven) copies of disk.
- A cave runs ordinary nix against an ordinary store. It never mounts a
  privileged filesystem, never owns or copies-up host-owned files, and
  never needs to know the host user's uid or gid.
- Cave-originated content never enters the host store. The daemon only
  ever reads the host store; nothing an agent produces can be written
  where the host would later trust it.
- A package one agent installs becomes usable by sibling agents promptly,
  without waiting for the installing agent to exit.
- Sharing is safe against a hostile agent: only content-addressed or
  trusted-signed packages become shared, and shared content is root-owned
  so a cave cannot rewrite it.
- The system works on a stock NixOS kernel. The one host-side component is
  a root systemd unit (`coding-cave-host.service`); no idmapped mounts, no
  extra capabilities, no experimental nix features.

## The three-level tree

Deduplication spans three levels, deepest first:

```
level 0  host /nix/store        nix-managed, trusted, read-only to the daemon
level 1  shared store           daemon-owned, root-owned, one per host
level 2  per-cave chroot stores  one per agent, owned by the launching user
```

- **Host store** (`/nix/store`). The host's own nix store, managed by the
  host's nix as usual. The daemon reads it (to learn which packages the
  host already has) but never writes it.
- **Shared store** (`/var/lib/coding-cave/store`). A root-owned nix chroot
  store the daemon owns exclusively. It is the quarantine boundary: cave
  content only ever rises this far, and only through the trust gate.
- **Per-cave stores** (`/var/lib/coding-cave/nix-stores/agent-<uuid>`).
  Each cave's own writable nix chroot store, bound at `/nix/store` and
  `/nix/var` inside the sandbox. The cave builds and fetches into it as
  itself. Created under a sticky, world-writable base (mode 1777, like
  `/tmp`) so any user can make their own cave store while the sticky bit
  stops one user removing another's.

All three sit on one filesystem so hardlinks work across them. The
canonical inode for a package is the host's when the host has it, else the
shared store's; every other copy is a hardlink onto that inode. Because
the canonical copy is root-owned (mode 555), a cave can read and run a
shared package but cannot modify, delete, or rename it.

Caves do not substitute from the shared store: an unprivileged cave can't
lock a root-owned store to substitute from it, and making the shared store
writable to caves would be a poisoning hole. A cave fetches packages the
normal way (binary cache or build); the daemon's dedup then collapses the
duplicate onto the canonical inode. A cave may re-fetch a package a sibling
already has, but it never becomes a second copy on disk.

## Convergence

The daemon (`cav-store watch`, the ExecStart of the systemd unit) runs a
timer loop. Each pass walks every cave store and, for each valid path in
it, runs one idempotent `converge` operation:

1. **Ensure the shared copy exists.** If the host has the path, import it
   (and its closure) straight from the host store: the host is trusted, so
   no gate. Otherwise the path is cave-originated and must clear the trust
   gate (below) before it is promoted from the cave into the shared store.
   An unsigned local cave build that fails the gate stays private to that
   cave, which is correct.
2. **Relink shared onto the host inode.** When the host has the path,
   hardlink the shared copy's files onto the host's inodes, so host and
   shared share one copy. (Skipped for the daemon's lifetime if this ever
   fails with EXDEV, meaning the host store is on a different filesystem;
   then the shared store stays canonical and cross-cave dedup still works.)
3. **Collapse the cave copy onto the shared inode.** Hardlink the cave's
   files onto the shared copy (which, after step 2, is the host inode).

Every step is "hardlink onto the canonical copy, skip if already there", so
a pass is safe to repeat and, crucially, self-healing. If the host acquires
a package later, or an inode moves for any reason (a nix GC deletes and
re-creates a path, `nix-store --optimise` relinks files), the next pass
relinks the shared copy and re-collapses every cave onto the new canonical
inode. There is no state to keep and no invariant that inodes never move —
the earlier design depended on exactly that invariant, and a single moved
inode silently stranded every cave onto a dead copy until the disk filled.

A full pass is cheap: it is readdir + a sqlite read per cave + stat-guarded
hardlinks, and the stat guard turns an already-deduped tree into a walk that
writes nothing. The first pass after a deploy does the real import and link
work; later passes are nearly all skips.

**Error policy: fail loudly and fast.** The first serious error — a failed
import or hardlink, an unreadable DB, a GC or quota failure — exits the
daemon, and the systemd unit does not restart it. A failed unit that stays
failed points at exactly what to fix; a daemon that tolerates a store
problem and moves on leaves a scar that compounds (tolerating one is how
the disk filled the first time). Only normal lifecycle is a non-error: a
cave or path vanishing mid-pass (`cav gc` removing it), an unsigned local
build staying private behind the gate.

One class is excluded rather than failed on: **derivations (`.drv`) stay
cave-private.** Importing a `.drv` makes nix check that its entire
input-drv closure is valid in the destination store, and a cave that has
garbage-collected some inputs can't meet that (a dangling `.drv` is legal
in a store; only a transfer checks it). They are kilobyte build recipes,
so sharing them saves nothing.

### Concurrency

The one place the daemon writes a live cave's store is the hardlink in
step 3. It holds a shared lock on that cave's `nix/var/nix/gc.lock` for the
pass. nix's garbage collector takes that lock exclusively, so the shared
lock lets the cave's normal operations run but blocks its GC from deleting
a path mid-hardlink (and vice versa). The shared store has a single writer
(the daemon), and each cave's own nix uses that store's normal locking, so
nothing else collides.

The daemon also *reads* a cave's store to gate and promote a candidate, but
never as root. nix opening a writable local store as root rewrites the store
directory to its canonical `root:nixbld` 1775 ownership, which seizes the
store from its unprivileged owner and locks that user out. So the gate
(`nix store verify`) and the promotion export run as the cave's owner uid
(privileges dropped); only the import into the root-owned shared store runs
as root, which is the ownership that store wants.

## The trust gate

Promotion from a cave into the shared store is the only way bytes an agent
produced become shared, so it is where poisoning is blocked. The agent
controls its own store and DB, so any check against the agent's own recorded
hash is self-referential. The daemon instead gates on authenticity the agent
cannot forge:

    nix store verify --sigs-needed 1 -r <path>

This passes only when the path (and its whole closure, hence `-r`) is
**content-addressed** — the name is the content hash, so forging it is a
preimage attack — or carries a **signature from a trusted key** (e.g. a
cache.nixos.org substitution; the agent has no private key to forge one). It
also re-checks contents against the recorded hash, catching corruption or a
post-build swap. **Input-addressed local builds** (an agent's own unsigned
build outputs) cannot be authenticated this way and are refused: they stay
private to the cave that built them. Symlinks at a store-path root are never
promoted, since one would resolve wherever the agent pointed it.

Packages imported from the host store skip the gate: the host store is
level 0, root-managed and trusted by definition. Cave content never rises
into the host store, so the host cannot be poisoned by an agent regardless
of the gate.

## Garbage collection

The daemon garbage-collects the shared store hourly. Before collecting it
roots every shared path any cave still references, by symlinking each into
the shared store's gcroots dir, then runs `nix-store --gc` on the shared
store.

The rooting pass **fails closed**: if any cave's DB can't be read (a hot
WAL during a build makes a read-only open fail transiently), the pass reads
each DB with a bounded retry and, if a read still fails, refuses to collect
at all that interval rather than collect with incomplete knowledge of what
is in use. Collecting a path a cave still references is exactly what strands
caves: the path is deleted, later re-imported under a new inode, and every
cave collapsed onto the old inode keeps a full private copy. Failing closed
and retrying next interval is safe; collecting on partial knowledge is not.

Host GC needs no coordination with the daemon. `nix-collect-garbage` on the
host unlinks host directory entries, but a path the shared store still holds
survives through the shared store's hardlink; the inode is freed only once
nothing — host, shared, or cave — links it. The reverse holds too: the
shared store's GC unlinking a shared entry never disturbs the host's own
copy.

Per-cave stores are reclaimed by `cav gc` (removing an archived cave's store
entirely), which is separate from this shared-store collection.

## Disk quotas

Every cave's writable nix store sits on one shared host filesystem under
`/var/lib/coding-cave`. Without a cap, one runaway or hostile agent can fill
it, and because the cave nix databases live there too, a full disk breaks
nix for every cave at once. Optional ext4 project quotas bound that.

There are two caps, both off unless a size is set in the host config
(`/etc/coding-cave/host.json`, rendered by the NixOS module):

- **`perAgentQuota`** caps each agent's own per-cave nix store. Each agent
  gets a stable project ID derived from its key (FNV-1a, never 0 or the
  reserved home ID), set with `chattr -p <id> +P` so new files inherit it,
  and a block limit via `setquota -P`.
- **`sharedHomeQuota`** caps the shared cave home under one reserved project
  ID. It is a single global limit, not per-agent, because sibling agents at
  one level share a home. The cave home lives on the user's filesystem, so
  this cap must be applied there, not by the root store manager.

The daemon reads the config at startup. If a quota is configured, it probes
whether project quota is active on the cave-state filesystem; if not, it
exits non-zero so `coding-cave-host.service` fails loudly rather than running
unprotected. This is the deliberate exception to the project's usual graceful
handling: a quota you asked for that cannot be enforced is a misconfiguration,
not something to paper over. Applying a cap as caves appear is best-effort
and off the convergence path: a failure is logged, never fatal.

## Manual smoke test

On a host running the unit:

```
$ journalctl -u coding-cave-host.service -f &
$ cav agent --prompt 'nix shell nixpkgs#hello -c true'
# within one converge interval, then:
$ stat -c '%i %n' /nix/store/*-hello-*/bin/hello \
    /var/lib/coding-cave/store/nix/store/*-hello-*/bin/hello
```

If the host has (or gains) the hello package, the host and shared-store
copies share one inode. A package no cave references is collected by the
next hourly GC pass.
