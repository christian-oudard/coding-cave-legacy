# Design

Intended behavior for coding-cave, described in the abstract.

## Full autonomy inside, full control outside

The cave gives agents unrestricted access (`--dangerously-skip-permissions`) within a security boundary. The agent can install packages, run arbitrary commands, modify any file. But nothing escapes: the host filesystem is untouched until the user explicitly pulls. The user reviews and controls what crosses the boundary.

## The host is the source of truth

The host repo is canonical. The cave works on an overlay copy. `pull` brings cave work to the host; `push` brings host state to the cave. The cave never modifies the host directly.

`reset` is the escape hatch: wipe everything in the cave and start fresh from the host. It always works, regardless of how broken the cave state is.

## Projects are folders, not names

On the host, the user thinks in terms of project directories, not slugs. The project name (slug) is always derived from the directory name. If a project folder is moved or renamed, `cav move` updates the registry to point to the new location and renames internal state to match. Commands that accept a project argument also detect the current project from CWD.

## Session isolation

Each agent session runs in its own bwrap namespace with its own overlay and its own gitdir. Multiple agents working on the same project get independent working trees and independent git state — they cannot see or interfere with each other's uncommitted work or HEAD position.

### Session branches

Sessions work directly on real branches (e.g. `main`). Each session gets its own gitdir with independent HEAD, index, and refs, so multiple sessions can commit on the same branch without interfering with each other. `_forward_refs` handles all concurrency: fast-forward, rebase, ownership-based adopt, and force-update with backup.

- A `shared` remote is configured pointing to `/projects/.shared-gitdirs/<slug>` (the shared gitdir bind-mounted rw into the cave).
- A post-commit hook pushes the current branch to shared (FF only, non-FF pushes fail silently), records the pushed SHA in `refs/pushed/<branch>`, and fetches latest from shared. `refs/pushed` lets `_forward_refs` know that this session was the last to update shared, enabling ownership detection even when `refs/synced` is stale.

`cav shell` joins a running agent's namespace via `nsenter`, giving the user direct access to that agent's in-progress files for inspection, testing, and debugging.

### Session identity

Sessions are identified by their first task message and start time, displayed in `cav status`:

    my-project
      [10:23] "Fix the pull crash when..."    running
      [10:45] "Add network isolation..."      running

When multiple sessions exist for a project, `cav shell` prompts the user to choose which session to join. If only one session is running, it joins directly.

### Session lifecycle

Each `cav` invocation starts a new bwrap process. The namespace lives as long as the agent does — no persistent idle process, no PID 1 init, no zombie reaper needed. When the agent exits, its bwrap exits.

### What persists across sessions

- Home directory (Claude Code config, conversation history)
- Nix store overlay (packages installed via `nix shell`)
- Shared gitdirs (commits merged from session gitdirs on cleanup)

What does not persist: each session's working tree overlay and session gitdir. Uncommitted changes and unpushed refs live only in the session. To preserve work, commit it.

## Git isolation via separate gitdir

The cave's `.git` is not on the overlay. It's a separate gitdir bind-mounted into place. This avoids overlayfs bugs with atomic rename (which git uses for index and ref updates) and keeps git operations fast and reliable inside the cave.

A shared gitdir holds the canonical objects and refs for each project. Each session gets its own lightweight gitdir that shares the object store via `objects/info/alternates`. This gives each session an independent HEAD, index, and refs — one agent's commits don't move another agent's HEAD or make its working tree appear dirty.

Per-session gitdirs are small (index file + refs + config). The bulk of git data (packfiles, loose objects) lives in the shared gitdir and is never duplicated. New objects created during a session live in the session gitdir until cleanup, when they're pushed to the shared gitdir.

On session end, refs and new objects from the session gitdir are merged into the shared gitdir via `_forward_refs`, then the session gitdir is removed. `pull` and `push` operate on the shared gitdir as before.

### Alternates path management

Session gitdirs use `objects/info/alternates` to share the object store with the shared gitdir. The alternates path must differ between two contexts:

- **Inside the cave** (bwrap namespace): `/projects/.shared-objects/<slug>` — the ro-bind mount of the shared object store.
- **On the host** (during pull/push/cleanup): the real host path like `~/.local/share/coding-cave/projects/<slug>/gitdir/objects`.

`create_session_gitdir` writes the host path. `start_session` rewrites it to the cave path before bwrap. Code that reads git objects from a session gitdir on the host must ensure the alternates point to the host path first.

### Live sync vs final cleanup

There are two distinct operations that move refs from session gitdirs to the shared gitdir. They have different safety requirements because one operates on live sessions and the other on dead ones.

**Live sync** (`forward_session_refs`): called by `pull` and `push` to pick up in-flight commits from running sessions. `forward_session_refs` forwards each session's branch refs to shared. Decision logic for each branch ref in `_forward_refs`:

1. **Unchanged** (ref == `refs/init` or `refs/synced`) → skip. Session never touched this branch, or hasn't changed since last sync.
2. **New ref** (doesn't exist in shared) → create it, record `refs/synced`.
3. **Already in sync** (shared ref == session ref) → skip.
4. **Fast-forward** → update shared, record `refs/synced`.
5. **Session behind shared** → skip.
6. **Session owns branch** → session wins. Ownership = `existing_sha in (init, synced, pushed)`. Handles amend, amend + new commits, squash, message-only amend, any rewrite — regardless of whether trees match.
7. **Diverged, session doesn't own** → try rebase (`_rebase_commits`). If rebase succeeds, update shared. If fails: skip during live sync, force-update with backup during cleanup.

Ownership is the central mechanism. It answers: "has any other source (push, other session) modified shared since this session last knew about it?" Three independent signals, any one sufficient:

- `refs/init/<branch>` — shared ref at session creation. If `refs/init` == current shared ref, no one else has modified the branch since this session was born.
- `refs/synced/<branch>` — session ref at last successful `_forward_refs` sync. If `refs/synced` == current shared ref, no one else has modified the branch since the last sync.
- `refs/pushed/<branch>` — session ref at last successful post-commit hook push. If `refs/pushed` == current shared ref, this session's hook was the last to update shared. Bridges the gap between hook pushes and `_forward_refs` syncs — without it, the hook moves shared past `refs/init`, `refs/synced` doesn't exist yet, and the ownership check fails even though the session is the sole authority.

Safety invariant: a session can only replace a shared ref when it can prove no other source has modified it. If another source modified shared (none of the three signals match), _forward_refs either rebases (preserving both sides' work) or force-updates with backup (cleanup only, no data loss).

Safety constraints:

- **Read-only from the session's perspective**: `_forward_refs` must not modify the session gitdir in any way visible to the running agent. In practice, it writes `refs/synced` (internal bookkeeping, not visible to normal git operations) and temporarily rewrites alternates (restored immediately after). The running agent is unlikely to observe these, and the agent's git operations don't depend on them.
- **Never clobber new content**: only replace shared refs when the session is the sole authority (ownership proven) or via rebase (preserving both sides).

**Cleanup** (`merge_session_gitdirs`): called when a session exits. Uses the same `_forward_refs` logic. The only difference: when rebase fails and ownership is unproven, cleanup force-updates shared and backs up the old ref under `refs/sessions/<sid>/`. Live sync skips instead.

#### Scenario analysis

**Single session, normal commit:**
Session commits A → hook pushes `main=A` (FF), `pushed=A`. Shared is current.

**Single session, amend:**
Session commits A → hook pushes (FF), `pushed=A` → session amends to A' → hook push fails (non-FF), `pushed` stays at A. `_forward_refs` sees `pushed=A==existing` → ownership proven → adopt A'.

**Single session, amend + new commits:**
Session commits A → hook pushes → amends to A' → commits B on top → hook push fails (non-FF from A). `pushed=A==existing` → ownership → adopt B (which includes A').

**Two sessions on main (normal concurrent):**
Session 1 commits A, hook pushes (FF). Session 2 commits B (based on pre-A HEAD), hook push fails (non-FF). `_forward_refs` for session 2: `pushed` doesn't match existing (A) → no ownership → rebase B onto A → shared gets both. No data loss, no race.

**Session commits, host pushes new content (`cav push`):**
Session commits A, syncs (`synced=A, shared=A`). Host `cav push` updates `shared=H`. Session amends A to A'. `synced=A ≠ existing=H` → no ownership → rebase A' onto H. Host's content preserved.

**Cleanup with failed hook (force=True):**
Session amends, hook fails, session exits. `_forward_refs(force=True)`: ownership check fires (same as live sync). If ownership proven → adopt directly (clean history). If not (another source modified shared) → rebase. If rebase fails → force-update with backup.

### Host identity forwarding and GPG signing

The cave forwards the host's git identity (`user.name`, `user.email`) via environment variables. Cave commits use the host's real identity, so commit hashes are preserved through the pull/push cycle. Sessions, shared gitdir, and host all use the same commit objects — no hash divergence.

GPG commit signing is supported via agent socket forwarding. The host's `gpg-agent` socket is bind-mounted into the cave (read-write). The host's public keyring is bind-mounted read-only. The private key never enters the cave. When the cave runs `git commit -S`, gpg talks to the forwarded socket, the host's gpg-agent signs with the private key, and returns the signature.

## Pull

Pull brings committed cave work to the host. Uncommitted changes are not transferred — with multiple sessions per project, there is no single working tree to pull from. Agents must commit their work for it to be pullable.

Pull is branch-aware: it iterates all branches in the cave gitdir and pulls each one that has new commits to the corresponding host branch. Branches are never crossed — cave commits on `develop` go to host's `develop`, not to whatever branch the host has checked out.

For the host's currently checked-out branch, pull uses checkout-based rebase (with stash/unstash to preserve uncommitted changes). For other branches, pull uses git plumbing (merge-tree + commit-tree + update-ref) so it never touches the working tree.

Pull preserves commit hashes — fast-forwards are true fast-forwards since the cave uses the host's git identity. After pulling, it syncs refs back to the cave gitdir so both repos share the same commit objects.

The rebase uses `--no-reapply-cherry-picks` to skip commits already present on the host. Conflicts on the current branch pause the rebase and print instructions for manual resolution. Conflicts on non-current branches are skipped with a warning.

### History rewrites require --force

Pull must not silently rewrite the host's history. When the cave rewrites history (squash, amend, rebase), pull detects this via `refs/remotes/host/<branch>` — a per-branch marker of the last commit both repos agreed on. If the host hasn't moved since the last sync but the merge-base is older, the cave rewrote history.

`--force` only applies to the currently checked-out branch. History rewrites on non-current branches always require checking out that branch first.

Without `--force`, pull warns, shows what changed, and exits:

    Cave rewrote history for project (2 -> 3 commit(s)).
      33a0f15 -> ae72b25 Per-user custom slash commands
      + 3953fd8 Set NIX_PATH so <nixpkgs> resolves
    Run 'coding-cave pull --force' to adopt.

Amended commits show `old_hash -> new_hash`, new commits show `+`.

With `--force`, pull adopts the cave's history directly (`reset --hard`) rather than rebasing. This preserves commit hashes that may already exist on a remote, keeping the host fast-forwardable from its tracking branch.

### Preserving remote-pushed history

Pull must not create commits that diverge from an already-pushed remote branch:

- Commits previously pulled and pushed to a remote keep their exact hashes in subsequent pulls.
- The result is always fast-forwardable from the remote tracking branch (assuming no independent host-side changes).

This is why the force path uses `reset --hard` (adopting exact commit objects) rather than rebase (which creates new hashes).

## Push

Push brings host commits to the cave. It updates the shared gitdir's refs and objects to match the host. Uncommitted host changes are not transferred.

Push is branch-aware: it iterates all host branches and pushes each one to the corresponding cave gitdir branch. Branches are never crossed — host commits on `develop` go to cave's `develop`, not to whatever the cave gitdir HEAD points to.

Push first syncs refs from any running sessions into the shared gitdir (same as pull), so it can detect unpulled commits even when they exist only in a session gitdir. For each branch, push checks whether the cave has unpulled commits. Branches with unpulled commits are skipped with a warning suggesting `coding-cave pull` first. Clean branches are pushed regardless. This means a partial push can succeed — some branches update while others are blocked.

`--force` only applies to the currently checked-out host branch. It skips the unpulled check for that branch and force-updates it in the cave gitdir. Other branches are not touched.

The gitdir is bind-mounted into the cave, so push updates are immediately visible to running agents. No cave restart needed.

## Cave-originated projects

Projects can be created inside the cave without a host source directory. The agent runs `cav-init <name>`, which creates a git repo at `~/unsynced-projects/<name>/` (persistent home). The agent works there directly.

On the host, `cav pull <name>` discovers the unsynced project, clones it to the host CWD, and registers it as a normal project. The next cave session mounts it at `/projects/<name>/` with overlay + gitdir like any other project.

Two locations make the state obvious: `/projects/` = synced with host, `~/unsynced-projects/` = cave-only.

## File transfer

`cp` copies files between the host and a running cave session without requiring a commit or restart. It operates directly on the session's fuse-overlayfs merged directory, which is accessible from the host even while bwrap is running.

    coding-cave cp <file>... <project>:<path>    host → cave
    coding-cave cp <project>:<path> <dest>       cave → host
    coding-cave cp [-f] <project>:~/path <dest>  cave home → host
    coding-cave cp [-R] [-f] <src>... <dest>     -R for directories, -f to skip overwrite prompt

The project slug can be omitted (`cave:path` or `:path`) if CWD is inside a registered project. When multiple sessions are running, `cp` prompts which to use.

**Tilde expansion**: paths starting with `~/` or `/home/cave-coder/` resolve to the persistent home directory (`~/.local/share/coding-cave/home/`). Home paths do not require a running session.

**Cross-project access**: `slug:/projects/<other>/path` resolves to the other project's overlay within the same session. Direct slug syntax (`other-project:path`) also works — `cp` searches all running sessions for the requested project's overlay, not just sessions started for that project.

**Clobber protection**: when copying cave → host, `cp` prompts before overwriting existing files. Use `-f`/`--force` to skip the prompt. Host → cave copies never prompt (cave overlay is ephemeral).

Files copied into the cave appear immediately — no restart needed. Files copied out of the cave capture the current overlay state (host lower + cave upper merged), including uncommitted modifications.

## Credential isolation

Credentials enter the cave only as environment variables (`--clearenv` + explicit forwarding). Nothing is written to disk inside the cave. OAuth tokens and API keys are forwarded from the host config at entry time.

## User/system configuration split

The cave separates system-level configuration (baked into the nix derivation) from user-level configuration (`~/.config/coding-cave/`).

### System layer (flake.nix)

Base packages are infrastructure only: claude-code, nix, git, coreutils, and standard unix tools. No editors, language runtimes, or dev tools — those come from the user layer. `system-claude.md` is baked into the derivation and concatenated with the user's CLAUDE.md at session startup.

### User layer (cave.nix)

`~/.config/coding-cave/cave.nix` declares extra packages, settings, environment variables, and skills:

    {
      inputs = {
        # "name" = "github:owner/repo";
      };
      config = { pkgs }: {
        packages = with pkgs; [ python3 uv nano ];
        settings = { };
        env = { EDITOR = "nano"; };
        skills = { };
      };
    }

On session start, `build_user_env()` evaluates cave.nix:

1. Copies cave.nix to `~/.local/share/coding-cave/user-env/`
2. Extracts `inputs` via `nix eval --json`
3. Generates a flake.nix that resolves inputs and builds a derivation
4. Runs `nix build` (cached via content hash)
5. Returns the output path

The output derivation contains `packages/` (symlinks to nix packages), `claude/settings.json`, `claude/env.json`, and `claude/skills/`. Lifecycle.py consumes this: adds package bin dirs to PATH, merges settings, seeds skills, and passes env vars via bwrap.

If cave.nix doesn't exist, a minimal default is generated. If the build fails, the previous result is used.

`coding-cave flake update [INPUT...]` runs `nix flake update` in the user-env directory. Pass input names to update selectively (e.g., `coding-cave flake update persist`). `update-plugins` is a deprecated alias.

### Mirror config

`config.toml` controls which host dotfiles are forwarded into the cave:

    [mirror]
    bash = true     # default: forward ~/.bashrc and ~/.bash_profile
    zsh = false     # forward ~/.config/zsh/ (opt-in)
    git = false     # forward ~/.gitconfig (opt-in)
    nvim = false    # forward ~/.config/nvim/ (opt-in, ro-bind)

### CLAUDE.md concatenation

Every session gets `~/.claude/CLAUDE.md` = system-claude.md + user CLAUDE.md. The system portion describes the cave environment (overlay model, no sudo, nix package management, git rules). The user portion comes from `~/.config/coding-cave/CLAUDE.md` on the host.

## Nix for reproducibility, fuse-overlayfs for mutability

The environment is defined in a Nix flake. The nix store is mounted as an overlay so agents can `nix shell` additional packages without affecting the host. Project files are also on overlays: the host's files are the read-only lower layer, cave modifications go to the upper layer.

All overlays use fuse-overlayfs. Kernel overlayfs in user namespaces has two problems: forced `uuid=on` remaps inodes causing EOVERFLOW on ext4, and the CVE-2023-0386 copy-up check requires all lower-layer UIDs/GIDs to be mapped but unprivileged users can only map one. fuse-overlayfs handles copy-up in userspace, bypassing both. The wrapper mounts all overlays before spawning bwrap and passes merged dirs via `--bind`. `/dev/fuse` is exposed inside the cave for nested caves.
