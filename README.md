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

[sandbox]
home-size-gb = 32  # default 32
```

### Install

```bash
nix profile install .
```

### Update after changes

```bash
nix profile upgrade coding-cave
```

Or run directly without installing:

```bash
nix run
```

## Usage

### Work on a project

```bash
cd ./my-project
codingcave
```

Mounts the current directory as a read-only overlay inside a bwrap sandbox and launches Claude Code. All writes go to a workspace directory (`~/.local/share/coding-cave/workspaces/`). The original project is never modified.

### Pull changes back

```bash
cd ./my-project
codingcave pull
```

Mounts the most recent workspace, rebases the agent's commits onto your branch, re-authoring each with your identity and GPG signature, then cleans up. If there are conflicts, the overlay stays mounted for manual resolution.

### Run an agent with a task

```bash
codingcave run ./my-project "add unit tests for the auth module"
```

Same overlay workspace as the default command, but targets a specific directory and passes a task prompt to Claude Code in non-interactive mode.

### Review a workspace manually

```bash
codingcave review
```

Lists available workspaces.

```bash
codingcave review <workspace>
```

Mounts the overlay workspace for manual inspection. The mounted directory is a fully functional git repo — the agent's commits are visible on top of your original history.


## Per-project configuration

Add `.claude-sandbox.toml` to a project:

```toml
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

A Python wrapper (`wrapper.py`) reads config, creates overlay workspaces, and sets up environment variables. The jail definition (`flake.nix`) uses jail.nix combinators to construct the bwrap invocation with persistent state bind-mounts, overlay-based workspace mounting, and an entry wrapper that launches Claude Code.

The project directory is mounted as the read-only lower layer of a bwrap overlay. Writes go to an upper directory on the host (`~/.local/share/coding-cave/workspaces/`). The original project is never modified. After the sandbox exits, `pull` uses fuse-overlayfs to mount the workspace, pulls changes via git, and cleans up. For manual inspection, `review` mounts the workspace without pulling.

Credentials are never written to disk — they're read from `config.toml` by the wrapper and forwarded via environment variables through `--clearenv`/`--setenv`.

See `agent-isolation-guide.md` for the full design spec.
