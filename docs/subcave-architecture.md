# Feature Request: Subcaves (nested cave sessions)

## Goal

Allow a cave to launch child caves. The primary use case is self-testing: Coding Cave's own integration tests (`test_cave`) need to spawn sandboxes to verify that the cave environment is sound. Running these tests inside a cave requires recursion. Agent-driven parallelism (an agent spawning its own child agents) is a secondary use case.

## Current blockers

Tested from inside a running cave session. Results:

| Requirement | Status | Blocker |
|---|---|---|
| nix available | yes | - |
| bwrap available | yes | - |
| nested user namespaces | yes | max_user_namespaces = 2147483647 |
| fuse-overlayfs binary | yes | in nix store |
| /dev/fuse device | **no** | not bind-mounted into cave |
| nix build | **no** | can't chmod lower-layer store paths |
| git clone --local | **no** | cross-device hardlink failure (different overlay mounts) |

### /dev/fuse (hard blocker)

The nix store overlay requires fuse-overlayfs, which requires `/dev/fuse`. The device node exists on the host but isn't exposed into the bwrap namespace. Without it, the subcave can't create its own writable nix store layer.

Exposing `/dev/fuse` lets any process inside the cave mount arbitrary FUSE filesystems. This should not be the default. Instead, gate it behind a `--recursive` flag on `cav agent`:

```
cav agent --recursive    # enables subcave support
cav agent                # default, no /dev/fuse, no subcaves
```

**Implementation in flake.nix:**

```nix
(try-fwd-env "CODING_CAVE_RECURSIVE")
(add-runtime ''
  if [ "''${CODING_CAVE_RECURSIVE:-}" = "1" ] && [ -e /dev/fuse ]; then
    RUNTIME_ARGS+=(--dev-bind /dev/fuse /dev/fuse)
  fi
'')
```

The host-side `cav agent --recursive` sets `CODING_CAVE_RECURSIVE=1` in the sandbox env. The jail runtime conditionally binds `/dev/fuse` only when the flag is present.

### nix build (medium blocker)

`nix build` inside the cave fails because it tries to `chmod` store paths fetched from the cache. These paths live on the overlay's read-only lower layer (the host nix store). The overlay allows creating new store paths (written to upper layer) but not modifying existing ones.

Options:
- Use the host-built `cav` binary instead of rebuilding. The nix store already contains built versions. The subcave just needs the right one on PATH.
- Build with `--store /tmp/subcave-store` to use a separate writable store (heavyweight).

### git clone --local (easy fix)

`git clone --local` fails with "Invalid cross-device link" because `/projects/` and `~/.local/` are on different overlay filesystems. Git tries to hardlink objects across them.

**Fix:** use `--no-hardlinks` instead of `--local` in the clone step.

## Architecture sketch

```
Host
  cav agent  (launches cave 1)
    Cave 1  (agent-1)
      subcav agent  (launches cave 1.1)
        Cave 1.1  (agent-1.1?)
      subcav agent  (launches cave 1.2)
        Cave 1.2  (agent-1.2?)
```

### Open questions

**ID scheme.** Current IDs are flat integers (1, 2, 3). Subcaves could use dotted notation (1.1, 1.2) or stay flat with a separate namespace. Flat is simpler but loses the parent-child relationship.

**Branch naming.** Current: `agent-<id>/<target>`. Subcave branches could nest: `agent-1/agent-1/main` is ugly. Alternative: `agent-1.1/main` if dotted IDs are used.

**Credential forwarding.** The parent cave has the crinj proxy address and CA cert. These would need to pass through to the subcave, which means the subcave's bwrap must bind-mount the CA cert and forward the proxy env vars. The proxy itself runs on the host and listens on localhost, which is shared (no network isolation), so it should just work.

**Coordination.** The parent agent and child agents all push to the same shared bare repo. The existing pre-receive hook and ff-only constraints handle coordination. No new mechanism needed.

**Autonomy boundary.** Today the user is the trust boundary. With subcaves, the parent agent becomes a sub-boundary. The parent can launch, monitor, and pull from children. The user still controls the outermost boundary via `cav pull`.
