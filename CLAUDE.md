# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**coding-cave**: a portable, reproducible sandbox for running Claude Code agents with full autonomy inside bwrap (bubblewrap) namespaces, with Nix-defined environments. Uses jail.nix for composable bwrap configuration. Currently in early implementation phase. The original design spec lives in `agent-isolation-guide.md`.

## Architecture

Two-layer isolation: bwrap namespace provides the security boundary, Nix provides reproducible environments. Claude Code runs with `--dangerously-skip-permissions` inside the box.

- **Flake** (`flake.nix`): Packages the sandbox using jail.nix combinators. Inputs: nixpkgs (unstable) and jail.nix from `sourcehut:~alexdavid/jail.nix`.
- **jail.nix** (cloned locally for reference): Pure Nix combinator library for bwrap. Provides composable permissions (network, filesystem mounts, env forwarding, etc.).
- **Per-project config** (`.claude-sandbox.toml`): TOML files in client repos declaring packages, credentials, filesystem mounts, and git settings.
- **Global config** (`~/.config/coding-cave/config.toml`): User credentials (OAuth tokens) and global settings. Keeps secrets out of shell environment and repo.

Key design decisions:
- jail.nix over raw bwrap: composable combinators, nix store closure binding, fake-passwd
- All restrictions at the bwrap layer, full autonomy inside the box
- `--clearenv` with explicit credential forwarding only (auditable)
- bash as default shell (standard for agents and general users)
- Git worktrees isolate agent work to dedicated branches
- Full network access in v1; proxy-based filtering planned for v2

## Tech Stack

- **Nix**: Environment definitions, flake packaging
- **jail.nix**: Composable bwrap configuration
- **bubblewrap (bwrap)**: Linux namespace isolation
- **TOML**: Configuration format (per-project and global)

## Building and Running

```bash
nix build        # build the sandbox
nix run          # run the sandbox (drops into bash inside the jail)
```

The sandbox hostname is `coding-cave`.
