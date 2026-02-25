# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**coding-cave**: a portable, reproducible sandbox for running Claude Code agents with full autonomy inside bwrap (bubblewrap) namespaces, with Nix-defined environments. Uses jail.nix for composable bwrap configuration.

## Architecture

Two-layer isolation: bwrap namespace provides the security boundary, Nix provides reproducible environments. Claude Code runs with `--dangerously-skip-permissions` inside the box.

The cave is a **persistent bwrap namespace** that survives across agent sessions. Multiple Claude Code agents can run simultaneously inside the same cave via `nsenter`. Projects are mounted as overlays at `/projects/<name>`.

### Source Files

- **`flake.nix`**: Nix flake that builds two sandbox derivations (`sandbox`, `sandboxGpu`) using jail.nix combinators. Inputs: nixpkgs (unstable), jail.nix (`sourcehut:~alexdavid/jail.nix`), claude-code-nix (`github:sadjow/claude-code-nix`). Defines all bwrap arguments, environment variables, overlays, and the cave init entrypoint (`tini -s -- sleep infinity`).
- **`wrapper.py`**: Python CLI that manages cave lifecycle, project registry, overlay pulling, and agent/shell launching via `nsenter`. Nix substitutes `@SANDBOX_BIN@` and `@SANDBOX_GPU_BIN@` at build time via `replaceVars`. The flake wraps this into a shell script that adds `fuse-overlayfs`, `fuse3`, and `util-linux` to `PATH`.
- **`claude-settings.json`**: Default `~/.claude/settings.json` seeded on first cave run. Currently sets model to opus, high reasoning effort, and skips the dangerous mode permission prompt.

### Configuration Files (on host, outside this repo)

- **Global config** (`~/.config/coding-cave/config.toml`): OAuth tokens (`credentials.claude-oauth-token`), sandbox settings (`sandbox.home-size-gb`). Credentials are forwarded via env vars only, never written to disk inside the cave.
- **User CLAUDE.md** (`~/.config/coding-cave/CLAUDE.md`): Optional instructions file, bind-mounted read-only into cave home at `~/.claude/CLAUDE.md`.
- **Shell dotfiles**: User's `~/.bashrc`/`~/.bash_profile` (ro-bind) or `~/.config/zsh/` (overlay for writable history) are forwarded into the cave.

### Persistent State (`~/.local/share/coding-cave/`)

- `cave.pid`, `bwrap.pid` — process tracking for the running cave
- `sessions/` — per-session PID files for tracking active agents
- `claude-state/` → `~/.claude` inside cave (Claude Code config/history)
- `claude.json` → `~/.claude.json` inside cave (onboarding/theme prefs)
- `projects.json` — project registry (slug → source path)
- `loaded-projects.json` — snapshot of projects loaded at cave start
- `projects/<slug>/upper/`, `work/`, `fuse-work/`, `merged/` — overlay dirs per project
- `projects/<slug>/gitdir/` — bind-mounted at `/projects/<slug>/.git` inside cave (regular filesystem, not on overlayfs)
- `nix-overlay/upper/`, `work/` — writable nix store overlay

### Key Design Decisions

- jail.nix over raw bwrap: composable combinators, nix store closure binding, fake-passwd
- `--clearenv` with explicit credential forwarding only (auditable)
- Persistent cave with `nsenter` for multi-agent: first invocation starts bwrap, subsequent invocations join via `nsenter`
- fuse-overlayfs for project isolation — kernel overlayfs in user namespaces can't copy-up files with unmapped GIDs (EOVERFLOW). The wrapper pre-mounts fuse-overlayfs on the host, and bwrap bind-mounts the merged dir. Falls back to kernel overlayfs if fuse-overlayfs isn't available.
- `.git` on bind mount, not overlay — kernel overlayfs in user namespaces breaks atomic rename (git index, refs). A dedicated `gitdir/` is bind-mounted at `/projects/<slug>/.git` so git operations work normally inside the cave.
- `/nix/store` overlay — host nix store as lower layer, cave-local upper for autonomous `nix shell` package management
- `pull` fetches from gitdir (host filesystem), rebases to re-author (strips cave-coder identity), then syncs gitdir back
- `push` updates gitdir refs/objects from host; working tree on the overlay is left untouched (uncommitted cave changes preserved)
- Auto-stop: cave shuts down when the last session exits (via `fork` + `stop_cave` in `enter_cave`)
- GPU access opt-in via `--gpu` flag (two separate sandbox derivations)
- CWD-based project detection: running `coding-cave` from inside a registered project dir auto-selects it; from an unregistered dir, prompts to add it

## CLI Commands

```
coding-cave [--gpu]                   enter cave, run claude (auto-detects project from CWD)
coding-cave shell [--gpu]             enter cave with user's shell
coding-cave add [dir]                 register a project (default: CWD)
coding-cave pull [project]            pull commits from cave gitdir, re-author
coding-cave push [project]            push host commits into cave gitdir (preserves uncommitted changes)
coding-cave reset [project]           wipe overlay + gitdir to match host (cave must be stopped)
coding-cave remove [project]          unregister a project and delete overlay data
coding-cave status                    show cave status and registered projects
coding-cave help                      show usage help
```

The cave starts automatically on first entry and stops when the last session exits.

## Building

```bash
nix build        # output: result/bin/coding-cave
```

The sandbox hostname is `coding-cave`. The user inside is `cave-coder`.
