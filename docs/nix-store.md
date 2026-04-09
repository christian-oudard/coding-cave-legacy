# Nix Store Inside the Cave

Agents can install packages with `nix shell`, `nix develop`, etc. This
document explains how the nix store is structured inside the cave and
how to manage it.

## Architecture

Each nesting level gets its own fuse-overlayfs mount for `/nix/store`,
stacked on top of its parent's merged view. All agents at the same level
share that level's overlay:

```
upper:  this level's writable layer (persists across sessions)
lower:  parent level's /nix/store (read-only from this level's view)
merged: bind-mounted into each agent's bwrap namespace
```

At the host level, the lower layer is the real host `/nix/store`. At a
nested level, the lower layer is *the parent cave's merged view*, which
transparently stacks all ancestor upper layers. Reads fall through to
whichever ancestor first has the path; writes land in this level's own
upper.

The overlay is mounted with `squash_to_uid` / `squash_to_gid` set to
the cave user. This makes all lower-layer paths appear as owned by the
cave user, allowing nix to `chmod` them during store path registration
(fuse-overlayfs performs a copy-up to the upper layer transparently).

- **Parent store** (`/nix/store` as seen from this level). Read-only
  lower layer. At host level this is the real host store; at a nested
  level it's the parent cave's merged overlay. Agents can use every
  package their ancestors have, without duplication.

- **Per-level overlay**
  (`~/.local/share/coding-cave/nix-overlay/shared/`). Each nesting
  level gets its own persistent home, so every level uses the same
  relative path (`shared/`) but it resolves to a distinct location on
  disk. Holds packages installed by any agent at this level via
  `nix shell` etc. Shared across siblings so a package installed by
  one agent is immediately available to all others without
  re-downloading.

- **Per-level DB** (`shared/db/`). The nix SQLite database for each
  level lives on the native filesystem, NOT under the FUSE mount.
  This is required for correct SQLite WAL locking with concurrent
  multi-agent access. A level's DB is seeded from its parent's DB at
  launch time, so nix treats already-built packages as registered and
  skips the `chmod` dance that would otherwise trigger a copy-up.

The overlay persists across agent launches. It is not torn down when an
agent exits.

## Isolation properties

- A level can read every package in any ancestor level (multi-level
  cache).
- A level can only write to its own upper / db. Descendants cannot
  modify their ancestors.
- Siblings at the same level share one overlay (writes by one are
  visible to the others).
- Packages that already exist in an ancestor are never duplicated.
  Subcaves only write their own new installs.

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

### Cache pruning

The DB seed also prunes stale entries from two nix caches in the
persistent home (`~/.cache/nix/`):

- **Fetcher cache** (`fetcher-cache-*.sqlite`): maps flake URLs to
  store paths. Individual rows referencing GC'd paths are deleted.
  Nix re-validates pruned entries cheaply via HTTP etags.

- **Eval cache** (`eval-cache-v6/*.sqlite`): maps flake fingerprints
  to derivation paths. Each file is one flake evaluation. If any
  sampled path is missing, the whole file is removed.

## Garbage Collection

A level's overlay upper can be cleaned up manually. For the host level:

```
$ fusermount3 -uz ~/.local/share/coding-cave/nix-overlay/shared/merged
$ rm -rf ~/.local/share/coding-cave/nix-overlay/shared/{upper,work,db}
```

For a subcave level, replace `shared` with
`subcaves/<parent-lineage>`. This removes all packages installed at
that level (and inherited by its descendants); they will be
re-downloaded on next use. Ancestor levels are unaffected.

## Promoting Packages to Host

To permanently keep a cave-installed package on the host, copy it from
the level's upper layer:

```
$ cp -a ~/.local/share/coding-cave/nix-overlay/shared/upper/<hash>-<name> /nix/store/
$ nix-store --register-validity <<< "<path> <hash> <narSize> <deriver> 0"
```

The package then becomes a first-class host store path, survives host
`nix-store --gc`, and appears in the lower layer for all future caves.
