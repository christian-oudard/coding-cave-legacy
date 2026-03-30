# Nix Store Inside the Cave

Agents can install packages with `nix shell`, `nix develop`, etc. This
document explains how the nix store is structured inside the cave and
how to manage it.

## Architecture

The cave uses a three-layer fuse-overlayfs mount for `/nix/store`:

```
upper:  per-agent writable layer (ephemeral, small)
lower1: cave shared store (read-only to agents)
lower2: host /nix/store (read-only to agents)
```

- **Host store** (`/nix/store` on the host). Read-only lower layer.
  Agents can use all host packages but cannot modify the host store.
  This preserves the sandbox boundary.

- **Cave shared store** (`~/.local/share/coding-cave/nix-store/`).
  Read-only lower layer shared across all agents. When an agent exits,
  `cav` promotes completed store paths from the agent's upper into this
  shared store so other agents can use them without re-downloading.

- **Per-agent upper** (ephemeral). Holds in-progress installs. Small
  and cleaned up on agent exit after promotion.

The cave also maintains its own nix state directory
(`~/.local/share/coding-cave/nix-state/`) with a separate DB and GC
roots, independent from the host's `/nix/var/nix/`.

## Nix DB Seeding

The cave's nix DB is seeded from the host's DB on each agent startup
(simple copy, not merge). This tells nix about all host store paths so
it doesn't try to re-download or `chmod` them. without seeding, nix
fails with `Operation not permitted` because lower-layer store paths
are owned by the host nix daemon (uid 65534) and the cave user (uid
1000) cannot `chmod` them.

Cave-installed packages from previous sessions may lose their DB
registration after a fresh seed, but the files persist in the shared
store. Nix re-registers them on demand, which works because shared
store paths (promoted from agent uppers) are owned by the cave user.

## Garbage Collection

The cave shared store is a standard nix store with its own state
directory. Standard nix GC works on it:

```
$ NIX_STATE_DIR=~/.local/share/coding-cave/nix-state \
  nix-store --gc
```

This follows GC roots and deletes unreachable paths from the cave
shared store. It does not affect the host store.

## Promoting Packages to Host

To permanently keep a cave-installed package on the host:

```
$ nix copy --no-check-sigs \
  --from ~/.local/share/coding-cave/nix-store \
  /nix/store/abc123-some-package
```

This registers the path in the host nix DB. The package becomes a
first-class host store path, survives host `nix-store --gc`, and
appears in the lower layer for all future caves.

## Why Not Overlay the Host Store Directly?

Earlier versions tried sharing a single overlayfs upper across agents
or writing directly to the host store. Problems encountered:

- **Host store pollution**: agent-installed packages leak outside the
  sandbox. Security boundary violation.
- **Concurrent upper writes**: multiple agents sharing one overlayfs
  upper is unsupported by fuse-overlayfs and can corrupt it.
- **Duplication**: per-agent uppers without promotion duplicate large
  packages across agents.

The three-layer design avoids all three: host store is untouched, each
agent has its own upper, and promotion deduplicates into the shared
store.
