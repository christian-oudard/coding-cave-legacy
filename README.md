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
home-size-gb = 32  # optional, default 32
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
cd ~/code/my-project
coding-cave
```

The cave starts automatically on first entry. Your project directory is mounted as a read-only overlay — all writes go to `~/.local/share/coding-cave/projects/<name>/upper/`. The original project is never modified.

If you're not in a registered project directory, you'll be prompted to add it.

### Multiple agents

Open another terminal and enter the same cave:

```bash
cd ~/code/other-project
coding-cave
```

Both agents share the same cave namespace. The cave stops automatically when the last session exits.

### Get a shell

```bash
coding-cave shell
```

Enters the cave with your shell (bash or zsh) instead of Claude Code. Useful for debugging or manual work inside the sandbox. Nix is available for installing additional tools:

```bash
nix shell nixpkgs#python3 --command python3 script.py
```

### Register a project

```bash
coding-cave add ~/code/my-project
```

Adds a project to the registry without entering the cave. If the cave is already running, agents must restart to access the new project.

### Pull changes back

```bash
cd ~/code/my-project
coding-cave pull
```

Mounts the project overlay using fuse-overlayfs, fetches the agent's commits, and rebases them onto your branch — re-authoring each with your identity. If there are conflicts, instructions are printed for manual resolution.

Running `pull` from outside any project directory prints an error — specify a project or cd into one.

### Push host changes

```bash
cd ~/code/my-project
coding-cave push
```

Updates the cave's gitdir to match the host branch. The cave can be running — only the git refs and objects are updated, the working tree on the overlay is left untouched, so uncommitted changes in the cave are preserved. Running `push` from outside any project directory pushes all registered projects.

### Remove a project

```bash
cd ~/code/my-project
coding-cave remove
```

Unregisters the project and deletes its overlay data. The host project directory is never touched. If the project is loaded in a running cave, you must stop the cave first (exit all sessions).

You can also remove by slug: `coding-cave remove my-project`.

### Check status

```bash
coding-cave status
```


Shows whether the cave is running, how many sessions are active, and lists registered projects.

## Per-project configuration

Add `.claude-sandbox.toml` to a project:

```toml
[environment]
DATABASE_URL = "postgresql://localhost/dev"
```

Variables under `[environment]` are set when entering the cave for that project.

## User instructions

Place a `CLAUDE.md` at `~/.config/coding-cave/CLAUDE.md` to have it bind-mounted read-only into `~/.claude/CLAUDE.md` inside the cave. This is useful for instructions you want applied across all projects.

## Persistent state

State persists across cave sessions in `~/.local/share/coding-cave/`:

- `claude-state/` — bind-mounted as `~/.claude` (settings, session history)
- `claude.json` — bind-mounted as `~/.claude.json` (onboarding state, preferences)
- `projects.json` — project registry (slug → source path)
- `projects/<name>/` — per-project overlay upper/work dirs
- `nix-overlay/` — writable nix store overlay for packages installed inside the cave

On first run, Claude Code state is seeded from `claude-settings.json`. After that it's managed by Claude Code.

To reset to defaults, delete `~/.local/share/coding-cave/`.

## Architecture

Two-layer isolation:

1. **bwrap namespace** — the security boundary. `--clearenv` clears the host environment, ephemeral tmpfs provides the home directory, overlays isolate project writes.
2. **Nix** — reproducible environments. All tools delivered via the Nix store closure. A `/nix/store` overlay allows agents to install additional packages inside the cave.

The cave is a **persistent bwrap namespace**. The first `coding-cave` invocation starts bwrap with `tini` as init (`tini -s -- sleep infinity`). Subsequent invocations join the existing namespace via `nsenter`. The cave stops automatically when the last session exits.

Projects are mounted as bwrap overlays at `/projects/<name>` — the host directory is the read-only lower layer, writes go to an upper directory on the host. The `pull` command uses `fuse-overlayfs` outside the cave to mount the overlay as a git repo, fetches commits back to the host, and rebases them with the user's identity.

Credentials are never written to disk inside the cave — they're read from `config.toml` by the wrapper and forwarded via environment variables.
