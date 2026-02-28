# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**coding-cave**: a portable, reproducible sandbox for running Claude Code agents with full autonomy inside bwrap (bubblewrap) namespaces, with Nix-defined environments. Uses jail.nix for composable bwrap configuration.

## Architecture

Two-layer isolation: bwrap namespace provides the security boundary, Nix provides reproducible environments. Claude Code runs with `--dangerously-skip-permissions` inside the box.

Each agent session runs in its own **per-session bwrap namespace** with per-session overlays and per-session gitdirs. Multiple agents get independent working trees, HEAD, index, and refs. They share a common object store via git alternates and interact only through commits merged into the shared gitdir on session cleanup. `cav shell` joins a running session via `nsenter`. Projects are mounted as overlays at `/projects/<name>`.

### Source Files

- **`flake.nix`**: Nix flake that builds two sandbox derivations (`sandbox`, `sandboxGpu`) using jail.nix combinators. Inputs: nixpkgs (unstable), jail.nix (`sourcehut:~alexdavid/jail.nix`), claude-code-nix (`github:sadjow/claude-code-nix`). Defines all bwrap arguments, environment variables, overlays, and the entrypoint (`exec "$@"`).
- **`wrapper.py`**: Python CLI that manages per-session bwrap lifecycle, project registry, and git sync. Each `cav` invocation starts a new bwrap process with per-session overlays. Nix substitutes `@SANDBOX_BIN@` and `@SANDBOX_GPU_BIN@` at build time via `replaceVars`. The flake wraps this into a shell script that adds `util-linux` to `PATH`.
- **`claude-settings.json`**: Fallback `~/.claude/settings.json` seeded on first cave run when no per-user settings exist. Sets model to opus, high reasoning effort, and skips the dangerous mode permission prompt.

### Configuration Files (on host, outside this repo)

- **Global config** (`~/.config/coding-cave/config.toml`): OAuth tokens (`credentials.claude-oauth-token`). Credentials are forwarded via env vars only, never written to disk inside the cave.
- **Claude settings** (`~/.config/coding-cave/settings.json`): Per-user Claude Code settings, seeded into `~/.claude/settings.json` on first cave run. Falls back to the repo's `claude-settings.json` if not present.
- **User CLAUDE.md** (`~/.config/coding-cave/CLAUDE.md`): Optional instructions file, bind-mounted read-only into cave home at `~/.claude/CLAUDE.md`.
- **Custom skills** (`~/.config/coding-cave/skills/`): Optional directory of skill folders (each containing `SKILL.md`), seeded into `~/.claude/skills/` on startup (missing skills only). Built-in skills (claude-loop) are seeded automatically.
- **Shell dotfiles**: User's `~/.bashrc`/`~/.bash_profile` (ro-bind) or `~/.config/zsh/` (overlay for writable history) are forwarded into the cave.

### Persistent State (`~/.local/share/coding-cave/`)

- `sessions/<sid>.json` — session metadata (project, bwrap PID, start time)
- `sessions/<sid>.manifest.json` — per-session project manifest for bwrap
- `home/` → `~` inside cave (persistent home directory bind mount; Claude Code state lives here naturally at `.claude/` and `.claude.json`)
- `projects.json` — project registry (slug → source path)
- `projects/<slug>/gitdir/` — shared object store and canonical refs; `pull`/`push` operate on this
- `session-overlays/<sid>/` — per-session overlay upper/work dirs and per-session gitdirs (`gitdir-<slug>/`)
- `nix-overlay/upper/`, `work/`, `merged/` — writable nix store overlay (fuse-overlayfs, shared across sessions)

### Key Design Decisions

- jail.nix over raw bwrap: composable combinators, nix store closure binding, fake-passwd
- `--clearenv` with explicit credential forwarding only (auditable)
- Per-session bwrap: each agent gets its own namespace and overlay, no persistent idle process
- fuse-overlayfs for all overlays (project overlays and nix store) — kernel overlayfs in user namespaces has two issues: (1) forced `uuid=on` remaps inodes, causing EOVERFLOW on ext4 with high inode numbers, and (2) CVE-2023-0386 copy-up check requires all lower-layer UIDs/GIDs to be mapped in the mounter's namespace, but unprivileged users can only map 1 UID/GID. fuse-overlayfs handles copy-up in userspace, bypassing both. The wrapper mounts all overlays before spawning bwrap, passing merged dirs via `--bind`. `/dev/fuse` is bind-mounted into the cave for nested caves.
- Per-session gitdirs — each session gets its own gitdir (HEAD, index, refs) with `objects/info/alternates` pointing to the shared object store. Bind-mounted at `/projects/.gitdirs/<slug>` with a `.git` file in the overlay upper pointing to it. Shared objects are ro-bind-mounted at `/projects/.shared-objects/<slug>`. On cleanup, branch refs are fast-forwarded into the shared gitdir; divergent refs are saved under `refs/sessions/<sid>/`.
- `/nix/store` overlay — host nix store as lower layer, cave-local upper for autonomous `nix shell` package management
- `pull` fetches from gitdir (host filesystem), rebases to re-author (strips cave-coder identity), then syncs gitdir back. Commits only — no working tree transfer.
- `push` updates gitdir refs/objects from host. Commits only — no working tree transfer.
- Session cleanup: session gitdir refs merged into shared gitdir, then overlays and session dirs removed when bwrap exits
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
coding-cave cp <src>... <slug>:<path>  copy host files into a running session
coding-cave cp <slug>:<path> <dest>   copy cave files out to host
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
