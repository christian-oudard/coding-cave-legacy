# Coding Cave

Coordinates multiple Claude agents working in parallel on a Git project, using Git as the sole coordination mechanism. Each agent runs in a bwrap sandbox with nix packages.

## Development

Built in Rust. Use `nix develop --command cargo build` to build locally, or `nix build` for a full release build.

## Testing

Three test tiers:

- **Unit tests (inline):** `nix develop --command cargo test --bin cav --bin cav-pre-receive` — tests `check_ref` hook logic, `cp` symlink security, glob matching. No git subprocesses.
- **Integration tests:** `nix develop --command cargo test --test hook --test cli` — runs the hook and CLI binaries against real git repos. Requires git in PATH (provided by devShell).
- **Cave integration:** Run `cav agent` with a prompt that exercises `cav init`, `cav status`, `cav agent` nested — verifies sandbox, workspace isolation, and nested cave support.

Run unit + integration tests before committing changes to core modules.

## Coverage

`nix develop --command cargo llvm-cov` runs the full test suite and prints
a per-file summary. Add `--html` for line-by-line drill-down at
`target/llvm-cov/html/index.html`, or `--text` for annotated source.

## Benchmarking

`nix run .#cav-debug -- -v` builds with the dev profile and emits an elapsed-time prefix on every verbose log line — `[cav +X.XXXs]` instead of plain `[cav]`. Coverage spans the full launch path: cav itself, the supervisor, and the in-cave entry script. The release build (`nix run .` / `nix build`) drops the timing code entirely. Use this to localize startup regressions.

## Build

Nix-only. `nix build` produces the `cav` binary and all helper binaries (`cav-pre-receive`, `cav-post-receive`, `cav-handler`, `cav-supervisor`, `cav-git`, `cav-seccomp-gen`, `cav-broker`, `cav-channel-server`). Build-time values (version, sandbox config path, flake URLs) are emitted as a single JSON file at `$out/share/coding-cave/build.json`; the wrapper sets `CODING_CAVE_BUILD_CONFIG` to point at it. `cav-rs/src/constants.rs` loads the JSON at import time. Running without a full `nix build` leaves the env var unset. Most CLI commands work, but `cav version` and `cav agent` need a full `nix build`.

## Layout

- `cav-rs/` - Rust crate (cli, config, daemon, handler, init, sandbox, workspace, pull, push, status, git, user_env, and hook binaries in src/bin/)
- `lib/` - Nix library (mkCaveEnv.nix, sandbox.nix, exported as `lib.mkCaveEnv` in the flake)
- `.coding-cave/` - per-project data dir created by `cav init` (bare repo with pre-receive hook)

## Naming

The project is "Coding Cave" (not an acronym). Executables are `coding-cave` and `cav`. Agent IDs are numeric only (1, 2, 3). Display names are `<slug>@<n>` (e.g. `coding-cave@1`). Agent branches are `agent-<n>/<target>`.
