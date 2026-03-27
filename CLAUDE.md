# Coding Cave

Coordinates multiple Claude agents working in parallel on a Git project, using Git as the sole coordination mechanism. Each agent runs in a bwrap sandbox with nix packages.

## Testing

Three test tiers, separated by what they require:

- **Fast (pure Python):** `nix flake check` runs `test_hook`, `test_cli`, `test_alternates`, `test_lint`. No git subprocesses.
- **Git integration:** `python3 -m unittest -v tests.test_git tests.test_daemon tests.test_security`. Spawns real git repos, exercises CLI and daemon isolation.
- **Cave integration:** `python3 -m unittest -v tests.test_cave`. Requires running inside a cave (GPG socket, git identity). Tests environment features.

Run fast + git tests before committing changes to core modules. Run cave tests when changing sandbox or environment setup.

## Build

Nix-only. `nix build` produces the `cav` binary. `cave-env` is a symlinkJoin of all packages available inside the sandbox. The `@CAVE_ENV@` placeholder in `sandbox.py` is substituted at build time.

## Layout

- `cav/` - Python package (cli, config, daemon, handler, init, sandbox, workspace, pull, push, status, git, hooks/)
- `tests/` - test_hook (hook security), test_cli (CLI usability), test_alternates (shared clone setup), test_lint (import smoke, Path.home() audit, env completeness), test_git (full integration), test_daemon (daemon isolation), test_security (protocol fuzzing, agent isolation, stress)
- `.coding-cave/` - per-project data dir created by `cav init` (bare repo with pre-receive hook)

## Naming

The project is "Coding Cave" (not an acronym). Executables are `coding-cave` and `cav`. Agent IDs are numeric only (1, 2, 3). Display names are `<slug>@<n>` (e.g. `coding-cave@1`). Agent branches are `agent-<n>/<target>`.
