# Nix Store Inside the Cave

Agents can install packages with `nix shell`, `nix develop`, etc. This
document explains how the nix store is structured inside the cave and
how to manage it.

## Architecture

All agents share a single fuse-overlayfs mount for `/nix/store`:

```
upper:  shared writable layer (persists across sessions)
lower:  host /nix/store (read-only)
merged: bind-mounted into each agent's bwrap namespace
```

The overlay is mounted with `squash_to_uid` / `squash_to_gid` set to
the cave user. This makes all lower-layer paths appear as owned by the
cave user, allowing nix to `chmod` them during store path registration
(fuse-overlayfs performs a copy-up to the upper layer transparently).

- **Host store** (`/nix/store` on the host). Read-only lower layer.
  Agents can use all host packages but cannot modify the host store.

- **Shared upper** (`~/.local/share/coding-cave/nix-overlay/shared/upper`).
  Holds packages installed by any agent via `nix shell` etc. Shared
  across all agents, so a package installed by one agent is immediately
  available to all others without re-downloading.

- **Shared DB** (`~/.local/share/coding-cave/nix-overlay/shared/db/`).
  The nix SQLite database lives on the native filesystem, NOT under the
  FUSE mount. This is required for correct SQLite WAL locking with
  concurrent multi-agent access.

The overlay persists across agent launches. It is not torn down when an
agent exits.

## Nix DB Seeding

On first mount (or when the DB is older than 24 hours), the launcher
takes a fresh backup of the host nix DB and merges back cave-only paths
(packages any agent installed) whose files still exist on the merged
overlay. It also removes stale entries for paths the host GC'd after the
snapshot was taken.

The "backup + merge-back + stale cleanup" approach is self-cleaning:
paths the host has GC'd are dropped, and cave-only paths are only
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

The shared overlay upper can be cleaned up manually:

```
$ fusermount3 -uz ~/.local/share/coding-cave/nix-overlay/shared/merged
$ rm -rf ~/.local/share/coding-cave/nix-overlay/shared/{upper,work,db}
```

This removes ALL cave-installed packages for ALL agents. They will be
re-downloaded on next use.

## Promoting Packages to Host

To permanently keep a cave-installed package on the host, copy it from
the shared upper:

```
$ cp -a ~/.local/share/coding-cave/nix-overlay/shared/upper/<hash>-<name> /nix/store/
$ nix-store --register-validity <<< "<path> <hash> <narSize> <deriver> 0"
```

The package then becomes a first-class host store path, survives host
`nix-store --gc`, and appears in the lower layer for all future caves.
