# Coding Cave

Coordinates multiple Claude agents working in parallel on a Git project, using Git as the sole coordination mechanism. Each agent runs in a bwrap sandbox with nix packages.

## Cave hierarchy

`cav` only ever operates one level *below* itself. The cave a `cav` instance is running in is its "host cave" — opaque to it, managed by the parent `cav` that spawned it. There is no "operate on the current cave" mode and no "reach back to the host machine" mode; both are physically impossible across the sandbox.

- On the host machine, `cav agent` starts a cave (a child).
- Inside cave A, `cav agent` starts a subcave (cave B). From B's perspective, A is its host cave.
- Inside cave A, `cav status` lists subcaves of A — never A itself, never the host machine's caves.

`CODING_CAVE_VERSION` is set inside every cave and is the detection marker for "I'm inside a cave." Its value is the version of the `cav` that opened this cave (the parent's version), not the version of the `cav` binary on PATH here — those can differ if `cav` was rebuilt inside the cave for subcave testing.

Subcave operations (`cav agent`, `cav init`, `cav status`, `cav approve`, etc.) are gated inside a cave: they refuse by default with "Refusing to nest coding-cave sessions by default." To opt in, `export CODING_CAVE_NESTED=1` before running them — required for the cave-integration test tier below.

## Development

Built in Rust. Use `nix develop --command cargo build` to build locally, or `nix build` for a full release build.

## Testing

Three test tiers:

- **Unit tests (inline):** `nix develop --command cargo test --bin cav --bin cav-git-pre-receive` — tests `check_ref` hook logic, `cp` symlink security, glob matching. No git subprocesses.
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

Nix-only. `nix build` produces the `cav` binary and all helper binaries (`cav-git-pre-receive`, `cav-git-post-receive`, `cav-git-daemon`, `cav-supervisor`, `cav-git`, `cav-seccomp-gen`, `cav-comms-broker`, `cav-comms-channel-server`). Build-time values (version, sandbox config path, flake URLs) are emitted as a single JSON file at `$out/share/coding-cave/build.json`; the wrapper sets `CODING_CAVE_BUILD_CONFIG` to point at it. `cav-rs/src/constants.rs` loads the JSON at import time. Running without a full `nix build` leaves the env var unset. Most CLI commands work, but `cav version` and `cav agent` need a full `nix build`.

## Layout

- `cav-rs/` - Rust crate (cli, config, daemon, init, sandbox, workspace, pull, push, status, git, user_env, plus the helper binaries — git daemon, git hooks, comms broker, MCP channel server — in src/bin/)
- `lib/` - Nix library (mkCaveEnv.nix, sandbox.nix, exported as `lib.mkCaveEnv` in the flake)
- `.coding-cave/` - per-project data dir created by `cav init` (bare repo with pre-receive hook)

## Naming

The project is "Coding Cave" (not an acronym). Executables are `coding-cave` and `cav`. Agent IDs are numeric only (1, 2, 3). Display names are `<slug>@<n>` (e.g. `coding-cave@1`). Agent branches are `agent-<n>/<target>`.
