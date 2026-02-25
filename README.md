# coding-cave

A portable, reproducible sandbox for running Claude Code agents with full autonomy inside [bubblewrap](https://github.com/containers/bubblewrap) namespaces, with Nix-defined environments.

Uses [jail.nix](https://git.sr.ht/~alexdavid/jail.nix) for composable bwrap configuration. Claude Code runs with `--dangerously-skip-permissions` inside the sandbox — all restrictions are at the bwrap boundary.

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

[git]
name = "Your Name"
email = "you@example.com"
```

### Build

```bash
nix build
```

## Usage

### Launch Claude Code in sandbox

```bash
codingcave
```

Launches Claude Code with `--dangerously-skip-permissions` inside a bwrap namespace. Ephemeral home directory, full network access, dev tools on PATH.

### Run an agent on a project

```bash
codingcave run ./my-project "add unit tests for the auth module"
```

Creates a git worktree on a dedicated branch (e.g., `agent/add-unit-tests-for-the-auth-module-20260218-091500`), launches Claude Code with the task inside the sandbox, and leaves the work on the branch for review.

### Interactive shell in a project

```bash
codingcave shell ./my-project
```

Drops into a bash shell inside the sandbox with the project directory mounted read-write. No worktree — edits go to the working directory directly.

### Clean up a worktree

```bash
codingcave cleanup ./my-project agent/add-unit-tests-20260218-091500
```

Removes the worktree and deletes the branch.

## Per-project configuration

Add `.claude-sandbox.toml` to a project:

```toml
[git]
branch-prefix = "agent"

[environment]
DATABASE_URL = "postgresql://localhost/dev"
```

## Persistent state

Claude Code state persists across sandbox runs in `~/.local/share/coding-cave/`:

- `claude-state/` — bind-mounted as `~/.claude` (settings, session history, plugins)
- `claude.json` — bind-mounted as `~/.claude.json` (onboarding state, preferences)

On first run, these are seeded from templates (`claude-settings.json` and a built-in default). After that, Claude Code manages them and they're left alone.

To reset to defaults, delete `~/.local/share/coding-cave/`.

## Architecture

Two-layer isolation:

1. **bwrap namespace** — the security boundary. Clears host environment, hides host home directory, provides ephemeral filesystems.
2. **Nix** — reproducible environments. All tools delivered via the Nix store.

A Python wrapper (`wrapper.py`) reads config, creates git worktrees, and sets up environment variables. The jail definition (`flake.nix`) uses jail.nix combinators to construct the bwrap invocation with persistent state bind-mounts, runtime-conditional workspace mounting, and an entry wrapper that launches Claude Code.

Credentials are never written to disk — they're read from `config.toml` by the wrapper and forwarded via environment variables through `--clearenv`/`--setenv`.

See `agent-isolation-guide.md` for the full design spec.
