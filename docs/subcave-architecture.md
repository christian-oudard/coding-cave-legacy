# Subcaves (nested cave sessions)

## Goal

Allow a cave to launch child caves. The primary use case is self-testing: Coding Cave's own integration tests (`test_cave`) need to spawn sandboxes to verify that the cave environment is sound. Running these tests inside a cave requires recursion. Agent-driven parallelism (an agent spawning its own child agents) is a secondary use case.

## How it works

All caves are recursive by default. `/dev/fuse` and `/dev/net/tun` are always bind-mounted if available.

### Nix store

Each nesting level gets a fuse-overlayfs on `/nix/store`:
- Lower layer: parent's store (read-only)
- Upper layer: per-cave writable dir

The parent seeds the child's nix DB so store paths from the parent are recognized without re-downloading.

### Network

Each subcave gets its own network namespace via slirp4netns, with internet access tunneled through the parent.

### Crinj proxy chain

```
subcave -> parent crinj (tunnel mode) -> host crinj (MITM mode) -> API
```

Only the outermost (host) crinj does MITM. All inner layers run in tunnel/passthrough mode. The host's CA cert at `/etc/crinj-ca.pem` is bind-mounted into every cave and is correct at every depth.

### Environment propagation

- `CODING_CAVE_VERSION`: set at each level
- `CODING_CAVE_LINEAGE`: chains parent agent names (e.g. `project@1.project@2`)
- `CODING_CAVE_RECURSIVE`: always `1`
- `CODING_CAVE_HOST_HOME`: preserved from host through all levels

### Homes

Each subcave gets its own isolated persistent home directory. This avoids configuration collisions and keeps the per-cave overhead small (~20MB).

### Project mounting

The project directory is bind-mounted read-write into each subcave at the same `/projects/<name>` path.

### Coordination

Parent and child agents push to the same shared bare repo. The pre-receive hook and ff-only constraints handle coordination.

## Testing

Subcave integration tests live in `tests/test_cave.py::TestSubcaveLaunch`. They use `cav agent --prompt TEXT` which runs `claude --print TEXT` inside the subcave and captures stdout.

Tests are consolidated into a single subcave launch to avoid ~30s startup cost per test. The subcave runs all checks in one prompt, and individual test methods parse the combined output.

Readiness checks (device nodes, env vars, binary availability) are in `TestSubcaveReadiness` and run without launching a subcave.
