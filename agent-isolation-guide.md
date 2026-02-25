# Agent Sandbox: bwrap + Nix

A portable, reproducible sandbox for running Claude Code agents with full autonomy. Replaces Claude Code's built-in sandbox with a stronger bwrap-based isolation layer. Claude Code runs unsandboxed (`--dangerously-skip-permissions`) inside the box.

This is a standalone system, not tied to any particular dotfiles or machine configuration. It should work on any Linux machine with Nix installed.

## Architecture

```
Host machine
  |
  +-- claude-sandbox run ~/projects/foo "implement feature X"
        |
        +-- parses .claude-sandbox.toml (if present)
        +-- creates git worktree on agent branch
        +-- builds nix environment (base + per-project packages)
        +-- injects credentials (explicit env vars + auth paths)
        +-- launches bwrap namespace:
              - /workspace (read-write, git worktree)
              - /nix/store (read-only)
              - /home/agent (ephemeral, writable)
              - /tmp (tmpfs)
              - full network access (v1)
              - no access to host home, keys, or config
              - only explicitly forwarded credentials
              |
              +-- claude --dangerously-skip-permissions
        |
        +-- on exit: agent's work is on its git branch
        +-- user reviews via local diff, branch inspection, or PR
```

## Design Principles

1. **The isolation boundary is the box itself.** No restrictions on shell, commands, or tools inside the sandbox. The agent can install packages, run arbitrary code, modify any file in its workspace. Internal restrictions (per-command sandboxing, read-only project mounts) add friction without proportional security benefit when the outer boundary is strong.

2. **Instant recreation.** If anything breaks, destroy and recreate in seconds. bwrap has zero startup overhead. Nix provides the environment from cache. Git worktrees are near-instant.

3. **Nix defines the environment.** A base flake provides common tooling. Per-project config adds project-specific dependencies. Declarative and reproducible.

4. **Portable.** Ships as a Nix flake. Works on any NixOS machine or any Linux machine with Nix. No dependency on a particular user's dotfiles.

5. **Per-project configuration.** Each project can specify what tools, runtimes, and credentials the agent needs via a config file committed to the repo.

6. **Explicit credentials.** The sandbox clears all host environment variables and hides the host home directory. Credentials the agent needs (API keys, git auth) must be explicitly declared in config. This makes credential exposure auditable.

## Versioning

- **v1 (current target):** bwrap isolation, nix environments, git worktrees, full network access, per-project config, explicit credential forwarding.
- **v2 (future):** Network filtering via reverse proxy (allowlist for Anthropic API, GitHub, package registries), scoped/short-lived credentials, resource limits (CPU, memory, disk), audit logging.

## Project Structure

This should be a standalone Nix flake, in its own repository.

```
claude-sandbox/
  flake.nix              # Nix flake: packages the sandbox tool
  flake.lock             # Pinned dependencies
  claude-sandbox.sh      # Single orchestrator script (entry point)
  base-env.nix           # Base Nix environment for all sandboxes
  README.md
```

A single orchestrator script handles all phases: parse config, create worktree, build environment, invoke bwrap, clean up. Two scripts with unclear boundaries will drift; one entry point with clear phases is easier to maintain.

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
- Include `nix-ld` support inside the sandbox so pip-installed packages with C extensions can find shared libraries. This requires ensuring `/lib64/ld-linux-x86-64.so.2` exists and points to the nix-ld dynamic linker inside the bwrap mount namespace. Spike this early — it may need special handling.

### bwrap Strategy

The orchestrator invokes bwrap directly with shell-constructed arguments. The bwrap setup for a headless agent sandbox is straightforward (no GUI, Wayland, D-Bus, or GPU concerns), so a library like jail.nix is not required for v1. If we later need more sophisticated permissions (selective nix store exposure, seccomp filters, etc.), jail.nix can be adopted as a flake input — it's pure Nix and would slot in without runtime cost.

Key bwrap flags:
- `--unshare-user --unshare-pid --unshare-uts --unshare-ipc --unshare-cgroup` (namespace isolation)
- `--share-net` (v1: full network; v2: `--unshare-net` with proxy)
- `--new-session --die-with-parent`
- `--clearenv` (clear all host env vars, then set only what's declared)
- `--ro-bind /nix/store /nix/store` (tools, read-only)
- `--bind <worktree> /workspace` (project, read-write)
- `--tmpfs /home/agent` (ephemeral home)
- `--tmpfs /tmp`
- `--proc /proc --dev /dev`
- `--dev /dev` then `--tmpfs` over dangerous subdirs (`/dev/snd`, `/dev/input`, `/dev/usb`, `/dev/disk`, etc.) per sandkasten's pattern
- Per-project `--ro-bind` paths from config
- Credential bind-mounts (e.g., `--ro-bind ~/.ssh/id_ed25519 /home/agent/.ssh/id_ed25519`)
- `--setenv` for each forwarded env var

### Per-Project Configuration

Each project repo can contain a `.claude-sandbox.toml` to declare what the agent needs. If absent, the base environment is used. TOML was chosen over Nix expressions because this config is committed to the repo and should be editable by any contributor, not just Nix users.

```toml
# .claude-sandbox.toml

[packages]
# Nixpkgs attribute names for tools the agent needs
nix = ["python3", "uv", "nodejs", "postgresql"]

[environment]
# Extra environment variables (static values)
DATABASE_URL = "postgresql://localhost/dev"

[credentials]
# Host env vars to forward into the sandbox (must exist on host)
forward-env = ["ANTHROPIC_API_KEY", "GITHUB_TOKEN"]

# Host paths to bind-mount read-only for auth
# Paths are relative to the user's home directory
ro-bind = [".ssh/id_ed25519"]

[filesystem]
# Additional host paths to bind-mount (read-only by default)
ro-bind = ["/var/run/postgresql"]

[git]
# Branch naming for the agent's worktree
branch-prefix = "agent"
# Whether the agent should push its branch to the remote
push = false
```

The sandbox reads this file from the project and merges it with the base environment.

This config format is provisional. We'll discover the right shape by actually using the system on real projects. Patterns to borrow from agent-of-empires: config merge hierarchy (global defaults -> per-project overrides) and worktree path templates with variables like `{repo-name}` and `{branch}`.

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

### Orchestrator (claude-sandbox.sh)

Single entry point handling all phases:

```
claude-sandbox run <project-dir> ["task description"]
claude-sandbox shell <project-dir>    # interactive shell, no claude
claude-sandbox cleanup <sandbox-name> # remove preserved sandbox
```

Phases for `run`:
1. Parse `.claude-sandbox.toml` from project dir (if present)
2. Validate that required credentials exist on host
3. Build nix environment (base + per-project packages, cached after first build)
4. Create git worktree on a dedicated branch
5. Construct bwrap arguments (mounts, env vars, credentials)
6. Launch bwrap namespace
7. Inside namespace: run `claude --dangerously-skip-permissions` with task description (headless) or drop to interactive shell
8. On exit: clean up worktree (unless `--keep`)

If a task description is provided, runs Claude in headless mode. Otherwise launches an interactive session.

## What the Agent Sees

Inside the sandbox:
- `/workspace` — the project (git worktree, read-write)
- `/home/agent` — ephemeral home directory
- `/tmp` — tmpfs
- `/nix/store` — read-only, all tools available
- Full network access (v1)
- Forwarded credentials only (API keys as env vars, auth files at expected paths)

Not visible:
- Host home directory
- SSH keys, GPG keys (except those explicitly declared in config)
- Other users' files and processes
- Host environment variables (except those explicitly forwarded)

## How the Agent's Work Gets Back to the User

The agent works on a dedicated git branch in a worktree. On exit, the branch contains the agent's commits. The user has several options for review:

- **Local diff:** `git diff main..agent/feature-x` against the worktree
- **Branch inspection:** check out the agent's branch, run tests, review changes
- **PR workflow:** if `[git] push = true` is set, the agent pushes its branch; the user (or a CI hook) creates a PR

For v1, the default is local-only (no push). The user reviews the branch and merges manually. This is the safest default — the agent can't modify the remote without explicit opt-in.

## Credentials

Credentials are the main tension between isolation and functionality. The agent needs at minimum:

- `ANTHROPIC_API_KEY` — or Claude Code won't work
- Git auth — SSH key or token, or the agent can't commit/push
- Possibly `GITHUB_TOKEN` — for `gh` CLI operations (creating PRs, reading issues)

The sandbox takes an explicit-forwarding approach:
- All host env vars are cleared (`--clearenv`)
- Only env vars listed in `[credentials] forward-env` are set inside the sandbox
- Only auth files listed in `[credentials] ro-bind` are mounted (read-only)
- The orchestrator validates that declared credentials exist before launching

This means: if `.claude-sandbox.toml` doesn't list `ANTHROPIC_API_KEY` in `forward-env`, the sandbox launches without it and Claude Code fails. This is intentional — credentials should never leak implicitly.

For v1, credentials inside the sandbox are fully accessible to the agent. If the agent is compromised, so are the forwarded keys. v2 should explore scoped tokens (e.g., GitHub fine-grained PATs with repo-only access) and short-lived credentials.

## CLAUDE.md for Sandboxed Projects

Projects using the sandbox should add a section to their existing CLAUDE.md (not a separate file):

```markdown
## Sandbox

You are running in an isolated bwrap sandbox with full autonomy.

- Workspace: /workspace (git worktree on a dedicated branch)
- Home: /home/agent (ephemeral, lost on exit)
- Commit freely on your branch. The user will review and merge.
- You have full network access.
- Install packages as needed (pip, npm, etc.)
- Push only if you've been told the project allows it.
```

## nix-ld Inside the Sandbox

Python packages installed via pip that include C extensions (numpy, pandas, etc.) need to find shared libraries. On NixOS, `nix-ld` provides a dynamic linker at `/lib64/ld-linux-x86-64.so.2` that handles this. Inside a bwrap sandbox with a synthetic filesystem, we need to ensure:

1. The nix-ld dynamic linker is included in the sandbox's nix environment
2. `/lib64/ld-linux-x86-64.so.2` is bind-mounted or symlinked to it
3. `NIX_LD_LIBRARY_PATH` is set to include common shared libraries

This should be spiked early in implementation. If it proves difficult, an alternative is to include pre-built Nix packages for common Python libraries rather than relying on pip + nix-ld.

## Prior Art

See `prior-art/SUMMARY.md` for detailed analysis of 8 related projects. Key influences:

- **sandkasten**: Proof that a simple bash script gets 80% of the way. bwrap mount patterns (which system dirs to bind, tmpfs-over-dangerous-devices).
- **jail.nix**: Pure Nix combinator library for bwrap. Available as a future upgrade path if we need selective nix store exposure or more sophisticated permissions.
- **agent-of-empires**: Per-project TOML config schema, config merge hierarchy, worktree path templates. The orchestration patterns to borrow from.
- **nix-sandbox-mcp**: Demonstrated that Nix + jail.nix + bwrap works for agent sandboxing. Their project-agnostic sandbox artifacts with runtime mounting is a pattern worth considering.
- **sandbox-runtime (Anthropic)**: The baseline we're replacing. Per-command sandboxing with proxy-based network filtering. Good network filtering design for v2 reference.
- **NixPak**: Most mature Nix + bwrap project. The sloth system (runtime path resolution) and Go launcher (multi-process orchestration) are relevant if we need more sophistication later.

## Implementation Plan

1. Create the `claude-sandbox` repo with flake.nix and base-env.nix
2. Implement the orchestrator script with basic bwrap invocation (hardcoded flags, no config)
3. Test on a real project: can Claude Code run, install packages, edit files, commit?
4. Spike nix-ld: can pip-installed C extensions find shared libraries?
5. Add `.claude-sandbox.toml` parsing (per-project packages, credentials, filesystem)
6. Add git worktree creation and cleanup
7. Iterate on the config format based on actual usage

## Open Questions

- Should we mount all of `/nix/store` read-only (simple) or only the runtime closure (secure, requires jail.nix)? For v1, full store mount is probably fine — the threat model is "agent accidentally damages host," not "agent exfiltrates nix store contents."
- Should the sandbox support running multiple agents concurrently on the same project (separate worktrees)?
- What resource limits (CPU, memory, disk) should be configurable? Skip for v1, revisit if agents start running expensive builds.
- Should we support non-git projects?
- For v2 network filtering: sandbox-runtime's proxy approach (HTTP + SOCKS5 + socat bridges) or NixPak's pasta approach (subnet isolation)? The proxy approach is more proven but more complex.
- Global config file (`~/.config/claude-sandbox/config.toml`) for default credentials and preferences? agent-of-empires uses a global -> profile -> repo merge hierarchy that works well.
