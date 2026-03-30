# Nix Store Inside the Cave

Agents can install packages with `nix shell`, `nix develop`, etc. This
document explains how the nix store is structured inside the cave and
how to manage it.

## Architecture

The cave uses a per-agent fuse-overlayfs mount for `/nix/store`:

```
upper:  per-agent writable layer (persists across sessions)
lower:  host /nix/store (read-only)
```

- **Host store** (`/nix/store` on the host). Read-only lower layer.
  Agents can use all host packages but cannot modify the host store.
  This preserves the sandbox boundary.

- **Per-agent upper** (`~/.local/share/coding-cave/nix-overlay/<id>/upper`).
  Holds packages installed by the agent via `nix shell` etc. Persists
  across sessions so agents don't re-download packages.

The cave also maintains its own nix state directory
(`~/.local/share/coding-cave/nix-state/`) with a separate DB and GC
roots, independent from the host's `/nix/var/nix/`.

## Nix DB Seeding

Each agent startup takes a fresh backup of the host nix DB, then
merges back cave-only paths (packages the agent installed) whose files
still exist on disk. This tells nix about all host store paths so it
doesn't try to re-download or `chmod` them. Without seeding, nix fails
with `Operation not permitted` because lower-layer store paths are
owned by the host nix daemon (uid 65534) and the cave user (uid 1000)
cannot `chmod` them.

The "backup + merge-back" approach is self-cleaning: paths the host
has GC'd are dropped from the fresh backup, and cave-only paths are
only preserved if their files still exist. This prevents the DB from
growing unboundedly.

Cave-only paths must be preserved because nix would otherwise try to
re-substitute them, which fails with EPERM on chmod of the existing
overlay path.

### Cache pruning

The DB seed also prunes stale entries from two nix caches in the
persistent home (`~/.cache/nix/`):

- **Fetcher cache** (`fetcher-cache-*.sqlite`): maps flake URLs to
  store paths. Individual rows referencing GC'd paths are deleted.
  Nix re-validates pruned entries cheaply via HTTP etags.

- **Eval cache** (`eval-cache-v6/*.sqlite`): maps flake fingerprints
  to derivation paths. Each file is one flake evaluation. If any
  sampled path is missing, the whole file is removed. Nixpkgs eval
  rebuild costs ~10s, so this only triggers when actually stale.

## Garbage Collection

The per-agent overlay upper can be cleaned up manually:

```
$ rm -rf ~/.local/share/coding-cave/nix-overlay/<id>/upper
```

This removes all cave-installed packages for that agent. They will be
re-downloaded on next use.

## Promoting Packages to Host

To permanently keep a cave-installed package on the host, copy it from
the agent's overlay upper:

```
$ cp -a ~/.local/share/coding-cave/nix-overlay/<id>/upper/<hash>-<name> /nix/store/
$ nix-store --register-validity <<< "<path> <hash> <narSize> <deriver> 0"
```

The package then becomes a first-class host store path, survives host
`nix-store --gc`, and appears in the lower layer for all future caves.

## Why Per-Agent Overlays?

Each agent gets its own overlay to avoid:

- **Host store pollution**: agent-installed packages leak outside the
  sandbox. Security boundary violation.
- **Concurrent upper writes**: multiple agents sharing one overlayfs
  upper is unsupported by fuse-overlayfs and can corrupt it.
