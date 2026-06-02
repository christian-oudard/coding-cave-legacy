# Nix Store Inside the Cave

Agents can install packages with `nix shell`, `nix develop`, etc. This
document specifies what the nix store system must do and explains how
it works.

## Requirements

### Visibility

All host store paths must be readable inside the cave. Host paths are
owned by root (uid 0, gid 0) with mode 555 (dirs) and 444 (files).
Inside the cave's user namespace, host uid 0 is unmapped (appears as
65534/nobody). Kernel overlayfs reads these paths without issue since
the CVE-2023-0386 check only triggers on copy-up, not reads.

Ancestor store paths are readable by descendants without duplication.
Sibling agents at the same nesting level do NOT share installed packages
(see "Per-agent overlay" below): a package installed by one agent is
private to that agent until it is moved into an ancestor layer.

### Writes

New packages land in a cave-local writable layer, never in the host
store. Nix's path registration sequence (NAR extract, chmod 0444/0555,
chown 0:0, SQLite insert) must complete without errors. Metadata
canonicalization (chmod, chown, mtime) only runs on paths the cave
just created, which are already in the upper layer. No copy-up of
host content is ever triggered.

This is the central invariant: **the cave never modifies host store
paths.** The only overlay operations on host content are reads (stat,
readdir, readlink, open O_RDONLY). With correct DB seeding, nix treats
host paths as already registered and never re-downloads or re-registers
them.

### Lock file isolation

Nix lock files are always at `/nix/store/{hash}-{name}.lock`, owned by
root, mode 0600, empty, coordinated via `flock()`. They are transient,
created and deleted within a single build. Host lock files coordinate
host builds, not cave builds. The cave does not want them.

Host lock files must not cause EPERM or EACCES errors inside the cave.
Cave lock files must work independently. No copy-up of host lock file
content may ever be triggered.

### DB management

The cave's nix DB must include all host store paths so nix does not
re-register or re-download them. Cave-only paths from previous sessions
must be preserved if their files still exist. Stale entries (host-GC'd
paths) must be cleaned on launch.

The DB must support concurrent access from multiple sibling agents.
SQLite WAL mode is required. The DB file must reside on the native
filesystem, not under a FUSE mount, to ensure correct file locking.

The DerivationOutputs table must be correctly populated for
overlay-built paths (nix does not populate this for builds that land
in the upper layer). Nix caches (fetcher cache, eval cache) must be
pruned of stale entries on launch.

### Per-agent overlay

Each agent gets its own upper, work, and merged directories plus its
own DB. Kernel overlayfs treats shared upperdir/workdir across mounts
as undefined behavior: orphan inodes, ESTALE errors, and failed
unlinks. Per-agent dirs are the only safe configuration when the
same kernel overlay is simultaneously active in multiple mount
namespaces (one per agent).

The tradeoff is that a package installed by one agent is not visible
to its siblings. Promoting packages to an ancestor layer (where all
descendants share them) is the intended mechanism for sharing, not
a shared upper at the same level.

### Nesting

Cave-in-cave works. Each nesting level gets its own persistent home,
overlay directories (upper, work, merged), and DB.

### No host configuration

The system works with a stock NixOS kernel. No capabilities
(CAP_DAC_READ_SEARCH, CAP_SYS_ADMIN), no setuid helpers, no systemd
units, no special host-side changes.

## Architecture

Each agent gets its own kernel overlayfs mount for `/nix/store`,
stacked on top of its parent level's merged view:

```
upper:  this agent's writable layer (persists across sessions)
lower:  parent level's /nix/store (read-only from this agent's view)
merged: bind-mounted into this agent's bwrap namespace
```

At the host level, the lower layer is the real host `/nix/store`. At a
nested level, the lower layer is the parent cave's merged view, which
transparently stacks all ancestor upper layers. Reads fall through to
whichever ancestor first has the path. Writes land in this agent's own
upper.

### Why kernel overlayfs, not fuse-overlayfs

Kernel overlayfs is mounted directly via `mount()` with options
`userxattr,uuid=off`. Requires kernel 5.11+ (unprivileged overlayfs
in user namespaces) and `CONFIG_OVERLAY_FS`. Tested on kernel 6.18,
ext4, NixOS.

Host store paths are owned by root (uid 0), which is unmapped in the
cave's user namespace (appears as uid 65534). Kernel overlayfs reads
these paths without issue. The CVE-2023-0386 check only triggers on
copy-up (writes that promote lower-layer content to the upper layer),
not on reads. With correct DB seeding and lock file shadowing, copy-up
of host content never happens.

fuse-overlayfs was the previous approach but had multiple problems:
FUSE daemon lifecycle management, O(N) FUSE overhead at nesting
depth N, stale dentry cache (billion-second ENTRY_TIMEOUT causing
invisible new packages), and the daemon's inability to read root-owned
mode-600 lock files (triggering copy-up failures).

### Components

- **Parent store** (`/nix/store` as seen from this level). Read-only
  lower layer. At host level this is the real host store. At a nested
  level it's the parent cave's merged overlay. Agents can use every
  package their ancestors have, without duplication.

- **Per-agent overlay**
  (`~/.local/share/coding-cave/nix-overlay/agent-<id>/`). Each agent
  at each level gets its own directory. Host agents sharing the
  persistent home, and siblings at the same nesting level, both scope
  by agent id rather than colliding on a single `shared/` path. Holds
  packages that agent installed via `nix shell` etc.

- **Per-agent DB** (`agent-<id>/db/`). The nix SQLite database for
  each agent lives on the native filesystem, not under the overlay
  mount. (Required for correct SQLite WAL locking, even though each
  agent's DB is now single-writer.) Seeded from the parent level's DB
  at launch time so nix treats already-built packages as registered
  and skips the `chmod` dance that would otherwise trigger a copy-up.

The overlay persists across agent launches. It is not torn down when an
agent exits.

## Lock file isolation

### The problem

Host nix lock files are owned by root (uid 0) with mode 0600. Inside
the cave's user namespace, host root is unmapped (appears as uid
65534). When a cave nix operation references a path whose lock file
exists on the host, overlayfs attempts copy-up: it tries to preserve
the file's ownership, but the uid is unmapped, so the operation fails
with EPERM.

### Why shadow files

Nix hardcodes the lock path as `storePath + ".lock"` in
`pathlocks.cc`. No configuration option exists to redirect lock files
elsewhere. Whiteout entries require `CAP_MKNOD` (char device 0/0) or
`trusted.overlay.*` xattrs, neither of which is available in user
namespaces.

Shadow files are the viable mechanism: create empty files with the same
names in the upper directory before mounting the overlay. The overlay
prefers upper-layer entries, so copy-up of the unreadable host
originals never happens. The cave's nix then creates, flocks, and
unlinks its own independent lock files in the upper layer.

### Startup shadow pass

Before mounting the overlay, the supervisor globs `/nix/store/*.lock`
and creates empty shadow files in the upper directory for each file
that is a transient nix lock file (regular file, zero bytes). Source
derivations ending in `.lock` (e.g. Cargo.lock, which have non-zero
size) are skipped.

### Runtime inotify watcher

After mounting, a background thread watches `/nix/store` for
`IN_CREATE` and `IN_MOVED_TO` events. When a new `.lock` file
appears, the watcher immediately creates an empty shadow in the upper
directory.

### Race window

There is a window between the startup glob and the inotify watch
becoming active. A host lock file created during this window is not
shadowed. This is benign: the cave's nix only accesses lock files for
paths it is building. With correct DB seeding, it never re-registers
host paths, so it never opens host lock files.

## Nix DB Seeding

On every launch, the launcher takes a fresh backup of the parent nix
DB (the real host DB at the top level, or the parent cave's
bind-mounted DB at a nested level) and merges back level-only paths
(packages any agent at this level installed) whose files still exist
on the merged overlay. It also removes stale entries for paths the
parent GC'd after the snapshot was taken.

The "backup + merge-back + stale cleanup" approach is self-cleaning:
paths the parent has GC'd are dropped, and level-only paths are only
preserved if their files still exist on the overlay.

### DerivationOutputs repair

Nix does not populate the DerivationOutputs table for paths built
through the overlay (the table maps derivation hashes to output store
paths). Missing entries cause `nix-store --realise` to fail with
"map::at". The seeder parses `.drv` files to extract output mappings
and inserts missing entries.

### Cache pruning

The DB seed also prunes stale entries from two nix caches in the
persistent home (`~/.cache/nix/`):

- **Fetcher cache** (`fetcher-cache-*.sqlite`): maps flake URLs to
  store paths. Individual rows referencing GC'd paths are deleted.
  Nix re-validates pruned entries cheaply via HTTP etags.

- **Eval cache** (`eval-cache-v6/*.sqlite`): maps flake fingerprints
  to derivation paths. Each file is one flake evaluation. If any
  sampled path is missing, the whole file is removed.

## Isolation properties

- An agent can read every package in any ancestor level (multi-level
  cache).
- An agent can only write to its own upper / db. Siblings and
  descendants cannot modify its layer, and it cannot modify theirs.
- Siblings at the same level do NOT share an overlay. Installs are
  private per agent until promoted into an ancestor layer.
- Packages that already exist in an ancestor are never duplicated.
  Agents only write their own new installs.

## Garbage Collection

An agent's overlay upper can be cleaned up manually:

```
$ rm -rf ~/.local/share/coding-cave/nix-overlay/agent-<id>
```

For a subcave, the same path resolves under that cave's subcave-home
(`~/.local/share/coding-cave/subcave-homes/<agent-name>/.local/...`).
Removing an agent's directory drops every package that agent installed.
They will be re-downloaded on next use. Sibling agents, ancestors,
and descendants are unaffected.

## Promoting Packages to Host

The `coding-cave-host.service` systemd unit (in `modules/host.nix`)
runs `cav host watch` as root alongside the idmap setup. It maintains
an inotify watch on every agent's `db/` directory; when the agent's
nix DB is written, the watcher diffs `ValidPaths` against the host
`/nix/store`, then for each new path:

1. `rename(2)` the path from `agent-<id>/upper/<basename>` to
   `/nix/store/<basename>`. Same filesystem, so atomic. The agent's
   overlay falls through to the lower (host store) on its next
   lookup, same path string, no whiteout.
2. Recursively `chown` to root:root so the host's idmapped mount maps
   it back to cave-coder inside other caves, matching the rest of
   `/nix/store`.
3. `nix-store --dump-db <new paths>` against a snapshot of the
   agent's DB, piped into `nix-store --load-db` against the host DB.
   Schema knowledge stays in nix.

The DB-write trigger eliminates the install-vs-DB race: nix registers
the path before closing the transaction, so by the time the watcher
fires the metadata is already complete.

Side effects: sibling agents in the same cave (and any later cave or
subcave) inherit the path through the overlay lower with no second
download. The agent that installed it keeps no copy in its upper.

### Known race: rename-vs-chown window

Between step 1 and step 2 the promoted path is on the host fs at uid
1000 (cave-coder, inherited from the agent's upper). Through the
idmapped mount + cave userns chain, files outside the idmap's range
appear as overflow uid (65534) inside other caves. Cave nix operations
that read content are unaffected, but anything that triggers an
overlayfs copy-up (chmod, mainly on `.lock` files) gets EPERM until
the recursive chown completes.

Window size is bounded by the file count in the package: microseconds
for a small derivation, up to ~100ms for something like glibc. Impact
on collision is a transient nix error that a retry resolves, not a
persistent broken state. The mitigations we considered all reintroduce
either a copy or a structural change to promotion timing (see commit
history). Accepted as-is; revisit if it actually bites a workflow.

### Manual smoke test

To verify end-to-end on a host that has the unit running:

```
$ journalctl -u coding-cave-host.service -f &
$ cav agent --prompt 'nix shell nixpkgs#hello -c true'
# wait for the agent to exit, then:
$ ls /nix/store/*-hello-*
```

The hello derivation's store path should be in `/nix/store`, owned by
root, with a row in `/nix/var/nix/db/db.sqlite`.
