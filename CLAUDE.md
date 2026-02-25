# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**coding-cave**: a portable, reproducible sandbox for running Claude Code agents with full autonomy inside bwrap (bubblewrap) namespaces, with Nix-defined environments. Uses jail.nix for composable bwrap configuration. The original design spec lives in `agent-isolation-guide.md`.

## Architecture

Two-layer isolation: bwrap namespace provides the security boundary, Nix provides reproducible environments. Claude Code runs with `--dangerously-skip-permissions` inside the box.

- **Flake** (`flake.nix`): Packages the sandbox using jail.nix combinators. Inputs: nixpkgs (unstable), jail.nix from `sourcehut:~alexdavid/jail.nix`, claude-code-nix from `github:sadjow/claude-code-nix`.
- **Wrapper** (`wrapper.py`): Python script that handles subcommands (run/pull/review/cleanup), reads config, creates overlay workspaces, sets up env vars, and execs the sandbox binary. Nix substitutes `@SANDBOX_BIN@` at build time via `replaceVars`.
- **jail.nix** (cloned in `prior-art/` for reference): Pure Nix combinator library for bwrap. Key combinators used: `network`, `no-new-session`, `add-pkg-deps`, `try-fwd-env`, `add-runtime`, `wrap-entry`, `set-hostname`.
- **Claude settings** (`claude-settings.json`): Default `~/.claude/settings.json` seeded on first run. Configures model, effort level, etc.
- **Per-project config** (`.claude-sandbox.toml`): TOML files in client repos declaring git settings and environment variables.
- **Global config** (`~/.config/coding-cave/config.toml`): User credentials (OAuth tokens), git identity. Keeps secrets out of shell environment and repo.
- **Persistent state** (`~/.local/share/coding-cave/`): Claude Code state (`claude-state/` → `~/.claude`) and preferences (`claude.json` → `~/.claude.json`) persist across sandbox runs. Seeded from templates on first run, then left alone.

Key design decisions:
- jail.nix over raw bwrap: composable combinators, nix store closure binding, fake-passwd
- All restrictions at the bwrap layer, full autonomy inside the box
- `--clearenv` with explicit credential forwarding only (auditable)
- Credentials never written to disk — forwarded via env vars only
- bwrap overlay for workspace isolation — project is read-only lower layer, writes go to upper dir on host
- `fuse-overlayfs` for post-exit review — mounts the overlay as a fully functional git repo
- `add-runtime` for runtime-conditional overlay/bind mounting and persistent state setup
- `wrap-entry` for cd to workspace and claude launch inside the jail
- Entry is always `claude --dangerously-skip-permissions` (use `!` inside claude for shell access)
- Full network access in v1; proxy-based filtering planned for v2

## Tech Stack

- **Nix**: Environment definitions, flake packaging
- **jail.nix**: Composable bwrap configuration
- **bubblewrap (bwrap)**: Linux namespace isolation
- **Python**: Wrapper script (config parsing, workspace management)
- **fuse-overlayfs**: FUSE overlay filesystem for post-exit workspace review
- **TOML**: Configuration format (per-project and global)

## Building and Running

```bash
nix build                                          # build the sandbox
nix run                                            # overlay cwd, launch claude
nix run . -- run ./project "task description"      # overlay project, launch claude with task
nix run . -- pull                                  # mount overlay, git pull, clean up
nix run . -- pull                                  # rebase + re-author agent commits, clean up
nix run . -- review                                # list workspaces
nix run . -- review <workspace>                    # mount workspace for manual inspection
rm -rf ~/.local/share/coding-cave/workspaces/<ws>  # manual cleanup
```

The sandbox hostname is `coding-cave`.
