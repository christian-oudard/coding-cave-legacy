# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**coding-cave**: a portable, reproducible sandbox for running Claude Code agents with full autonomy inside bwrap (bubblewrap) namespaces, with Nix-defined environments. Uses jail.nix for composable bwrap configuration.

## Architecture

Two-layer isolation: bwrap namespace provides the security boundary, Nix provides reproducible environments. Claude Code runs with `--dangerously-skip-permissions` inside the box.

Each agent session runs in its own **per-session bwrap namespace** with per-session overlays and per-session gitdirs. Multiple agents get independent working trees, HEAD, index, and refs. They share a common object store via git alternates and interact only through commits merged into the shared gitdir on session cleanup. `cav shell` joins a running session via `nsenter`. Projects are mounted as overlays at `/projects/<name>`.

### Source Files

- **`flake.nix`**: Nix flake that builds two sandbox derivations (`sandbox`, `sandboxGpu`) using jail.nix combinators. Inputs: nixpkgs (unstable), jail.nix (`sourcehut:~alexdavid/jail.nix`), claude-code-nix (`github:sadjow/claude-code-nix`). Defines all bwrap arguments, environment variables, overlays, and the entrypoint (`exec "$@"`).
- **`coding_cave/`**: Python package with the CLI and all logic:
  - `cli.py` — argument parser and main entry point
  - `commands.py` — CLI command implementations
  - `config.py` — global config loading, git identity forwarding, cave.nix building, mirror settings
  - `lifecycle.py` — session lifecycle (start, wait, nsenter, bwrap arg assembly)
  - `git.py` — git helpers with code-execution neutralization via `SAFE_GIT_OPTS`
  - `gitdir.py` — per-session gitdir creation, post-commit hook, `_forward_refs` merge logic
  - `overlay.py` — overlay mount/unmount and cleanup
  - `sync.py` — pull/push synchronization
  - `projects.py` — project registry
  - `sessions.py` — session tracking and cleanup
  - `_state.py` — constants, paths, Nix substitution markers
- **`defaults/claude-settings.json`**: Fallback `~/.claude/settings.json` seeded on first cave run. Sets model to opus, high reasoning effort, and skips the dangerous mode permission prompt.
- **`defaults/statusline.sh`**: Status line script showing cave session index.
- **`system-claude.md`**: System-level CLAUDE.md baked into the derivation. Concatenated with user CLAUDE.md at session startup.
- **`SPEC.md`**: Full specification covering session isolation, git sync, identity forwarding, live sync decision logic, and all design rationale.

### Configuration Files (on host, outside this repo)

- **Global config** (`~/.config/coding-cave/config.toml`): OAuth tokens (`credentials.claude-oauth-token`), mirror settings (`[mirror]` section). Credentials are forwarded via env vars only, never written to disk inside the cave.
- **cave.nix** (`~/.config/coding-cave/cave.nix`): User environment — extra packages, settings, env vars, and skills. Built via nix on session start. Auto-generated with defaults if missing.
- **User CLAUDE.md** (`~/.config/coding-cave/CLAUDE.md`): Optional instructions file, concatenated after system-claude.md into `~/.claude/CLAUDE.md` at session startup.
- **Shell dotfiles**: User's `~/.bashrc`/`~/.bash_profile` (ro-bind, opt-in via `[mirror]`) or `~/.config/zsh/` (overlay for writable history, opt-in via `[mirror]`) forwarded into the cave.

### Persistent State (`~/.local/share/coding-cave/`)

- `sessions/<sid>.json` — session metadata (project, bwrap PID, start time)
- `sessions/<sid>.manifest.json` — per-session project manifest for bwrap
- `home/` → `~` inside cave (persistent home directory bind mount; Claude Code state lives here naturally at `.claude/` and `.claude.json`)
- `projects.json` — project registry (slug → source path)
- `projects/<slug>/gitdir/` — shared object store and canonical refs; `pull`/`push` operate on this
- `projects/<slug>/saved-upper/` — overlay upper from last session (uncommitted changes, untracked files, deletions)
- `projects/<slug>/saved-index` — git index from last session (staging area preservation)
- `session-overlays/<sid>/` — per-session overlay upper/work dirs and per-session gitdirs (`gitdir-<slug>/`)
- `nix-overlay/upper/`, `db/` — persistent nix store overlay and DB (survive session restarts)
- `nix-overlay/work/`, `merged/` — transient nix overlay state (cleaned on last session exit)
- `user-env/` — generated flake and build output from cave.nix

### Key Design Decisions

- jail.nix over raw bwrap: composable combinators, nix store closure binding, fake-passwd
- `--clearenv` with explicit credential forwarding only (auditable)
- Per-session bwrap: each agent gets its own namespace and overlay, no persistent idle process
- fuse-overlayfs for all overlays (project overlays and nix store) — kernel overlayfs in user namespaces has two issues: (1) forced `uuid=on` remaps inodes, causing EOVERFLOW on ext4 with high inode numbers, and (2) CVE-2023-0386 copy-up check requires all lower-layer UIDs/GIDs to be mapped in the mounter's namespace, but unprivileged users can only map 1 UID/GID. fuse-overlayfs handles copy-up in userspace, bypassing both. The wrapper mounts all overlays before spawning bwrap, passing merged dirs via `--bind`. `/dev/fuse` is bind-mounted into the cave for nested caves.
- Per-session gitdirs — each session gets its own gitdir (HEAD, index, refs) with `objects/info/alternates` pointing to the shared object store. Bind-mounted at `/projects/.gitdirs/<slug>` with a `.git` file in the overlay upper pointing to it. Shared objects are ro-bind-mounted at `/projects/.shared-objects/<slug>`. On cleanup, branch refs are fast-forwarded into the shared gitdir; divergent refs are saved under `refs/sessions/<sid>/`.
- Branch-per-session — each session works on `refs/heads/session/<sid>`, not `main`. A post-commit hook force-pushes the session branch to the shared gitdir and fetches latest, giving agents visibility into each other's work via `shared/session/<other-sid>`.
- `/nix/store` overlay — host nix store as lower layer, cave-local upper for autonomous `nix shell` package management. Upper and DB persist across sessions so installed packages survive restarts; DB is only seeded from host on first run.
- Host git identity forwarding — the host's `user.name`, `user.email`, `user.signingkey`, and `commit.gpgsign` are forwarded into the cave via env vars. The host GPG agent socket and public keyring are bind-mounted in. Commits inside the cave are authored and signed as the host user, preserving commit hashes through pull/push. Fallback: if identity isn't configured, a throwaway `cave-coder` identity is used and `pull` re-authors commits.
- `pull` fetches from the cave gitdir and fast-forwards or rebases each branch onto the host. Multi-branch: non-current branches use plumbing (merge-tree + commit-tree), current branch uses checkout-based rebase with stash. Detects history rewrites and blocks without `--force`. Commits only — no working tree transfer.
- `push` updates cave gitdir refs from the host. Checks for unpulled cave commits first (blocks unless `--force`, which only applies to current branch). Commits only — no working tree transfer.
- Live sync — before pull/push, `sync_live_sessions()` forwards refs from running sessions into the shared gitdir via `_forward_refs`. Uses `refs/synced/<branch>` to track what was last synced; a session can only update a shared ref when shared has no new content since last sync.
- Code-execution neutralization — `SAFE_GIT_OPTS` disables `core.pager`, `core.fsmonitor`, `diff.external`, `core.sshCommand`, and filter drivers to prevent untrusted repos from executing code via git.
- Session continuity: on exit, overlay upper is saved to `saved-upper/` (preserves uncommitted changes, untracked files, deletions); on start, restored into the new session's overlay. Git stash (ref + reflog) and index (with loose objects) are also preserved. Concurrent sessions: uppers with only `.git` and clean indexes are not saved, preventing idle sessions from clobbering another session's state. **Limitation**: uncommitted state (saved-upper, saved-index, stash) uses a single per-project slot — when multiple sessions with uncommitted changes to the same project exit, last-to-exit wins. Committed changes are safe (refs are merged/rebased/backed up).
- Session cleanup: session gitdir refs merged into shared gitdir, overlay upper saved, then session dirs removed when bwrap exits
- GPU access opt-in via `--gpu` flag (two separate sandbox derivations)
- CWD-based project detection: running `coding-cave` from inside a registered project dir auto-selects it; from an unregistered dir, prompts to add it

## CLI Commands

```
coding-cave [--gpu] [-c] [-r [SID]]   start new agent session (-c continue, -r resume)
coding-cave shell [--gpu]              start shell or join running session
coding-cave add [dir]                  register a project (default: CWD)
coding-cave pull [project] [--force]   pull commits from cave gitdir to host
coding-cave push [project] [--force]   push host commits into cave gitdir
coding-cave reset [project]            wipe overlay + gitdir to match host (no running sessions)
coding-cave remove [project]           unregister a project and delete overlay data
coding-cave cp <src>... <slug>:<path>  copy host files into a running session
coding-cave cp <slug>:<path> <dest>    copy cave files out to host
coding-cave move <slug> <new-path>     update a project's source path (and slug if dir name changed)
coding-cave status [-a/--all]          show projects and running sessions
coding-cave update-plugins             update cave.nix flake inputs
coding-cave help                       show usage help
```

Each invocation starts a new bwrap process that exits when the agent/shell exits.

## Building

```bash
nix build        # output: result/bin/coding-cave
```

The sandbox hostname is `coding-cave`. The user inside is `cave-coder`.
