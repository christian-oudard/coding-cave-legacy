# Coding Cave

Coordinates multiple Claude agents working in parallel on a Git project, using Git as the sole coordination mechanism. Each agent runs in a bwrap sandbox with nix packages.

See [SPEC.md](SPEC.md) for the full specification.

## Testing

Tests are organized in three tiers based on their requirements.

### Fast (pure Python)

```
nix flake check
```

Runs `test_hook`, `test_cli`, `test_alternates`. No git subprocesses, no network, no special environment. These test the security boundary (pre-receive hook logic), CLI argument parsing, and shared clone setup.

### Git integration

```
python3 -m unittest -v tests.test_git
```

Spawns real git repos in temp directories and exercises the full workflow: init, workspace creation, push/pull, hook enforcement, multi-agent contention, rollback. Slower due to real git operations but still self-contained.

### Cave integration

```
python3 -m unittest -v tests.test_cave
```

Must run inside a cave. Tests features that depend on the cave environment: GPG commit signing (forwarded socket + key), git identity, and other sandbox-provided services. These tests skip gracefully if the required environment is not present.

## Build

Nix-only. `nix build` produces the `cav` binary.
