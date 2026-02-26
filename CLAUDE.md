# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**coding-cave**: a portable, reproducible sandbox for running Claude Code agents with full autonomy inside bwrap (bubblewrap) namespaces, with Nix-defined environments. Uses jail.nix for composable bwrap configuration.

## Architecture

Two-layer isolation: bwrap namespace provides the security boundary, Nix provides reproducible environments. Claude Code runs with `--dangerously-skip-permissions` inside the box.

Each agent session runs in its own **per-session bwrap namespace** with per-session overlays. Multiple agents get independent working trees and interact only through git commits to a shared gitdir. `cav shell` joins a running session via `nsenter`. Projects are mounted as overlays at `/projects/<name>`.

### Source Files

- **`flake.nix`**: Nix flake that builds two sandbox derivations (`sandbox`, `sandboxGpu`) using jail.nix combinators. Inputs: nixpkgs (unstable), jail.nix (`sourcehut:~alexdavid/jail.nix`), claude-code-nix (`github:sadjow/claude-code-nix`). Defines all bwrap arguments, environment variables, overlays, and the entrypoint (`exec "$@"`).
- **`wrapper.py`**: Python CLI that manages per-session bwrap lifecycle, project registry, and git sync. Each `cav` invocation starts a new bwrap process with per-session overlays. Nix substitutes `@SANDBOX_BIN@` and `@SANDBOX_GPU_BIN@` at build time via `replaceVars`. The flake wraps this into a shell script that adds `util-linux` to `PATH`.
- **`claude-settings.json`**: Default `~/.claude/settings.json` seeded on first cave run. Currently sets model to opus, high reasoning effort, and skips the dangerous mode permission prompt.

### Configuration Files (on host, outside this repo)

- **Global config** (`~/.config/coding-cave/config.toml`): OAuth tokens (`credentials.claude-oauth-token`). Credentials are forwarded via env vars only, never written to disk inside the cave.
- **User CLAUDE.md** (`~/.config/coding-cave/CLAUDE.md`): Optional instructions file, bind-mounted read-only into cave home at `~/.claude/CLAUDE.md`.
- **Shell dotfiles**: User's `~/.bashrc`/`~/.bash_profile` (ro-bind) or `~/.config/zsh/` (overlay for writable history) are forwarded into the cave.

### Persistent State (`~/.local/share/coding-cave/`)

- `sessions/<sid>.json` — session metadata (project, bwrap PID, start time)
- `sessions/<sid>.manifest.json` — per-session project manifest for bwrap
- `home/` → `~` inside cave (persistent home directory bind mount; Claude Code state lives here naturally at `.claude/` and `.claude.json`)
- `projects.json` — project registry (slug → source path)
- `projects/<slug>/sessions/<sid>/upper/`, `work/` — per-session overlay dirs (on tmpfs)
- `projects/<slug>/gitdir/` — bind-mounted at `/projects/<slug>/.git` inside cave (regular filesystem, not on overlayfs)
- `nix-overlay/upper/`, `work/` — writable nix store overlay

### Key Design Decisions

- jail.nix over raw bwrap: composable combinators, nix store closure binding, fake-passwd
- `--clearenv` with explicit credential forwarding only (auditable)
- Per-session bwrap: each agent gets its own namespace and overlay, no persistent idle process
- Kernel overlayfs for per-session project overlays — upper/work dirs on tmpfs (separate device from project source) to prevent `uuid=on` auto-enabling, which would block nested overlayfs in inner user namespaces.
- `.git` on bind mount, not overlay — kernel overlayfs in user namespaces breaks atomic rename (git index, refs). A dedicated `gitdir/` is bind-mounted at `/projects/<slug>/.git` so git operations work normally inside the cave.
- `/nix/store` overlay — host nix store as lower layer, cave-local upper for autonomous `nix shell` package management
- `pull` fetches from gitdir (host filesystem), rebases to re-author (strips cave-coder identity), then syncs gitdir back. Commits only — no working tree transfer.
- `push` updates gitdir refs/objects from host. Commits only — no working tree transfer.
- Session cleanup: overlays unmounted and session dirs removed when bwrap exits
- GPU access opt-in via `--gpu` flag (two separate sandbox derivations)
- CWD-based project detection: running `coding-cave` from inside a registered project dir auto-selects it; from an unregistered dir, prompts to add it

## CLI Commands

```
coding-cave [--gpu]                   start new agent session (auto-detects project from CWD)
coding-cave shell [--gpu]             start shell or join running session
coding-cave add [dir]                 register a project (default: CWD)
coding-cave pull [project]            pull commits from cave gitdir, re-author
coding-cave push [project]            push host commits into cave gitdir
coding-cave reset [project]           wipe overlay + gitdir to match host (no running sessions)
coding-cave remove [project]          unregister a project and delete overlay data
coding-cave move <slug> <new-path>    update a project's source path (and slug if dir name changed)
coding-cave status [-a/--all]         show projects and running sessions
coding-cave help                      show usage help
```

Each invocation starts a new bwrap process that exits when the agent/shell exits.

## Building

```bash
nix build        # output: result/bin/coding-cave
```

The sandbox hostname is `coding-cave`. The user inside is `cave-coder`.
