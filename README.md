# coding-cave

A portable, reproducible sandbox for running Claude Code agents with full autonomy inside [bubblewrap](https://github.com/containers/bubblewrap) namespaces, with Nix-defined environments.

Uses [jail.nix](https://git.sr.ht/~alexdavid/jail.nix) for composable bwrap configuration. Claude Code runs with `--dangerously-skip-permissions` inside the sandbox — all restrictions are at the bwrap boundary.

## Status

Early implementation. Currently working:

- Bash shell inside a bwrap namespace (hostname `coding-cave`)
- Network access
- Claude Code available on PATH
- OAuth token loaded from config file and forwarded into the sandbox
- Ephemeral home directory (lost on exit)

Not yet implemented:

- Workspace directory mounting
- Git worktree creation/cleanup
- Per-project configuration (`.claude-sandbox.toml`)
- Dev tools (git, ripgrep, fd, etc.)
- Network filtering (v2)

## Setup

### Prerequisites

- Linux with [Nix](https://nixos.org/) installed (flakes enabled)

### Configure credentials

Generate a Claude Code OAuth token:

```bash
claude setup-token
```

Create `~/.config/coding-cave/config.toml`:

```toml
[credentials]
claude-oauth-token = "<your-token>"
```

### Build and run

```bash
nix build
nix run
```

Inside the sandbox, Claude Code is available:

```bash
claude
```

## Architecture

Two-layer isolation:

1. **bwrap namespace** — the security boundary. Clears host environment, hides host home directory, provides ephemeral filesystems.
2. **Nix** — reproducible environments. All tools delivered via the Nix store.

A Python wrapper (`wrapper.py`) reads credentials from the config file and forwards them as environment variables into the sandbox.

See `agent-isolation-guide.md` for the full design spec.
