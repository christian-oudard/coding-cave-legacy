# Coding Cave

Coordinates multiple Claude agents working in parallel on a Git project, using Git as the sole coordination mechanism. Each agent runs in a bwrap sandbox with nix packages.

See [SPEC.md](SPEC.md) for the full specification.

## Testing

```
nix develop --command cargo test
```

Integration tests (real git repos, cave launches) are not yet ported. See `cav-rs/` for the Rust test suite.

## Build

Nix-only. `nix build` produces the `cav` binary and all helper binaries.
