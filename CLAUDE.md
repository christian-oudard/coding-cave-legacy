# Coding Cave

Coordinates multiple Claude agents working in parallel on a Git project, using Git as the sole coordination mechanism. Each agent runs in a bwrap sandbox with nix packages.

## Development

The base Python in the cave does not include project dependencies (click, vulture). Prefix commands with `nix develop --command` to get them, or use `nix flake check` / `nix build` which bring their own dependencies.

To add a new Python dependency, add it to the `python3.withPackages` calls in `flake.nix` (build, devShell, and checks). There is no pyproject.toml.

## Testing

Three test tiers, separated by what they require:

- **Fast (pure Python):** `nix flake check` runs `test_hook`, `test_cli`, `test_alternates`, `test_nixdb`, `test_lint`, `test_cp_security`. No git subprocesses.
- **Git integration:** `nix develop --command python3 -m unittest -v tests.test_git tests.test_daemon tests.test_security`. Spawns real git repos, exercises CLI and daemon isolation.
- **Cave integration:** `nix develop --command python3 -m unittest -v tests.test_cave`. Requires running inside a cave (GPG socket, git identity). Tests environment features.

Run fast + git tests before committing changes to core modules. Run cave tests when changing sandbox or environment setup.

## Build

Nix-only. `nix build` produces the `cav` binary. Build-time placeholders (`@VERSION@`, `@VERSION_DATE@`, `@SANDBOX_BIN@`, `@CAV_BIN@`, `@NIXPKGS_URL@`, `@CLAUDE_CODE_NIX_URL@`, `@NIX_CLAUDE_URL@`, `@MKCAVEENV_PATH@`, `@SYSTEM_CLAUDE_MD_PATH@`, `@DEFAULT_STATUSLINE_PATH@`) in cli.py, sandbox.py, and user_env.py are substituted by the Nix derivation. Running from source via `nix develop` works for tests and most CLI commands, but `cav version` and `cav agent` require a full `nix build` since they depend on substituted values.

## Layout

- `cav/` - Python package (cli, config, daemon, handler, init, sandbox, workspace, pull, push, status, git, user_env, hooks/)
- `lib/` - Nix library (mkCaveEnv.nix, exported as `lib.mkCaveEnv` in the flake)
- `tests/` - test_hook (hook security), test_cli (CLI usability), test_alternates (shared clone setup), test_nixdb (nix DB seeding, cache pruning), test_lint (import smoke, Path.home() audit, env completeness), test_git (full integration), test_daemon (daemon isolation), test_security (protocol fuzzing, agent isolation, stress)
- `.coding-cave/` - per-project data dir created by `cav init` (bare repo with pre-receive hook)

## Naming

The project is "Coding Cave" (not an acronym). Executables are `coding-cave` and `cav`. Agent IDs are numeric only (1, 2, 3). Display names are `<slug>@<n>` (e.g. `coding-cave@1`). Agent branches are `agent-<n>/<target>`.
