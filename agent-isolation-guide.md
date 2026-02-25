# Agent Sandbox: bwrap + Nix

A portable, reproducible sandbox for running Claude Code agents with full autonomy. Replaces Claude Code's built-in sandbox with a stronger bwrap-based isolation layer. Claude Code runs unsandboxed (`--dangerously-skip-permissions`) inside the box.

This is a standalone system, not tied to any particular dotfiles or machine configuration. It should work on any Linux machine with Nix installed.

## Architecture

```
Host machine
  |
  +-- claude-sandbox run ~/projects/foo "implement feature X"
        |
        +-- creates git worktree on agent branch
        +-- builds nix environment (base + per-project)
        +-- launches bwrap namespace:
              - /workspace (read-write, git worktree)
              - /nix/store (read-only)
              - /home/agent (ephemeral, writable)
              - /tmp (tmpfs)
              - full network access (v1)
              - no access to host home, keys, or config
              |
              +-- claude --dangerously-skip-permissions
```

## Design Principles

1. **Full freedom inside the box.** No restrictions on shell, commands, or tools inside the sandbox. The agent can install packages, run arbitrary code, modify any file in its workspace. The isolation boundary is the box itself.

2. **Instant recreation.** If anything breaks, destroy and recreate in seconds. bwrap has zero startup overhead. Nix provides the environment from cache. Git worktrees are near-instant.

3. **Nix defines the environment.** A base flake provides common tooling. Per-project config adds project-specific dependencies. Declarative and reproducible.

4. **Portable.** Ships as a Nix flake. Works on any NixOS machine or any Linux machine with Nix. No dependency on a particular user's dotfiles.

5. **Per-project configuration.** Each project can specify what tools and runtimes the agent needs via a config file in the repo.

## Versioning

- **v1 (current target):** bwrap isolation, nix environments, git worktrees, full network access, per-project config.
- **v2 (future):** Reverse proxy for network filtering, credential injection, API allowlisting, audit logging.

## Project Structure

This should be a standalone Nix flake, in its own repository.

```
claude-sandbox/
  flake.nix              # Nix flake: packages the sandbox tools
  flake.lock             # Pinned dependencies
  src/
    sandbox.sh           # bwrap wrapper script
    run.sh               # Orchestrator (high-level entry point)
  base-env.nix           # Base Nix environment for all sandboxes
  README.md
```

### flake.nix

Inputs:
- `nixpkgs` (nixos-unstable)
- `claude-code-nix` from `github:sadjow/claude-code-nix` - provides Claude Code as a nix package

Provides:
- `packages.default` - the `claude-sandbox` command
- The base environment definition
- All dependencies (bubblewrap, git, claude-code, common tools)

Notes:
- Allow unfree packages (`nixpkgs.config.allowUnfree = true`) since projects may need CUDA or other unfree deps.
- Include `nix-ld` support inside the sandbox so pip-installed packages with C extensions can find shared libraries.

### Per-Project Configuration

Each project repo can contain a `.claude-sandbox.toml` (or similar) to declare what the agent needs. If absent, the base environment is used.

```toml
# .claude-sandbox.toml

[packages]
# Nixpkgs attribute names for tools the agent needs
nix = ["python3", "uv", "nodejs", "postgresql"]

[environment]
# Extra environment variables
DATABASE_URL = "postgresql://localhost/dev"

[filesystem]
# Additional host paths to bind-mount (read-only by default)
ro-bind = ["/var/run/postgresql"]
```

The sandbox reads this file from the project and merges it with the base environment.

This config format is provisional. We'll discover the right shape by actually using the system on real projects.

### Base Environment (base-env.nix)

Common tools available in every sandbox:

```nix
{ pkgs }:

pkgs.mkShell {
  packages = with pkgs; [
    # Core
    bashInteractive coreutils gnugrep gnused gawk findutils
    which less file tree

    # Development
    git ripgrep fd jq curl wget

    # Claude Code
    claude-code

    # Common runtimes (projects can add more)
    python3 nodejs

    # Package managers
    uv
  ];
}
```

### Sandbox Script (src/sandbox.sh)

The bwrap wrapper. Key responsibilities:
- Create git worktree on a dedicated branch
- Set up bwrap namespaces (user, PID, UTS, IPC, cgroup)
- Bind-mount workspace, nix store, and any per-project paths
- Provide ephemeral /home/agent and /tmp
- Clear all host environment variables
- Launch nix environment, then claude inside it
- Clean up worktree on exit (unless --keep)

v1 uses `--share-net` (full network). v2 will add `--unshare-net` with proxy.

### Orchestrator (src/run.sh)

High-level entry point:

```
claude-sandbox run <project-dir> ["task description"]
claude-sandbox shell <project-dir>    # interactive shell, no claude
claude-sandbox cleanup <sandbox-name> # remove preserved sandbox
```

If a task description is provided, runs Claude in headless mode. Otherwise launches an interactive session.

## What the Agent Sees

Inside the sandbox:
- `/workspace` - the project (git worktree, read-write)
- `/home/agent` - ephemeral home directory
- `/tmp` - tmpfs
- `/nix/store` - read-only, all tools available
- Full network access (v1)

Not visible:
- Host home directory, SSH keys, GPG keys
- Other users' files and processes
- Host environment variables

## CLAUDE.md for Sandboxed Projects

Projects using the sandbox should include instructions for the agent:

```markdown
## Sandbox

You are running in an isolated bwrap sandbox with full autonomy.

- Workspace: /workspace (git worktree on a dedicated branch)
- Home: /home/agent (ephemeral)
- Commit freely on your branch. The user will review and merge.
- You have full network access.
- Install packages as needed (pip, npm, etc.)
```

## Implementation Plan

1. Create the `claude-sandbox` repo with flake.nix
2. Implement base-env.nix with common tools
3. Implement sandbox.sh (bwrap wrapper, worktree management)
4. Implement run.sh (orchestrator)
5. Add per-project config reading (.claude-sandbox.toml)
6. Test on a real project
7. Iterate on the config format based on actual usage

## Open Questions

- What's the right config file format? TOML? Nix expression? Discover through use.
- Should the sandbox support running multiple agents concurrently on the same project (separate worktrees)?
- How should the agent's work be reviewed? PR workflow? Direct branch inspection?
- What resource limits (CPU, memory, disk) should be configurable?
- Should we support non-git projects?
