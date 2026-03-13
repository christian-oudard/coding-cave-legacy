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

### Branch-per-session

Each session works on a private `session/<sid>` branch, not `main`. This mirrors how human developers use feature branches:

- `create_session_gitdir` creates `refs/heads/session/<sid>` pointing to the current HEAD and sets it as the session's HEAD via `symbolic-ref`.
- A `shared` remote is configured pointing to `/projects/.shared-gitdirs/<slug>` (the shared gitdir bind-mounted rw into the cave).
- A post-commit hook pushes the session branch to shared (force-push for session branches, FF-only for other branches) and fetches latest from shared. This gives agents visibility into each other's work via `shared/session/<other-sid>`.
- Agents can rebase onto `shared/main` periodically and merge to main when done.

On cleanup (`merge_session_gitdirs`), after forwarding all refs via `_forward_refs`, the session branch is merged to main:

1. **Same tree**: session branch tree == main tree → delete branch (nothing to merge, e.g. after re-authoring).
2. **Fast-forward**: session branch is a descendant of main → update main, delete branch.
3. **Behind**: session branch is an ancestor of main → delete branch.
4. **Diverged**: attempt `_rebase_commits` onto main. If clean → update main, delete branch. If conflict → leave branch under `refs/heads/session/<sid>` for manual recovery.

During live sync (`sync_live_sessions`), after forwarding session refs, `ff_session_branches` attempts FF-merges of all session branches to main so pull/push can see session commits.

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

On session end, refs and new objects from the session gitdir are merged into the shared gitdir, the session branch is FF-merged to main (or rebased if diverged), then the session gitdir is removed. `pull` and `push` operate on the shared gitdir as before.

### Alternates path management

Session gitdirs use `objects/info/alternates` to share the object store with the shared gitdir. The alternates path must differ between two contexts:

- **Inside the cave** (bwrap namespace): `/projects/.shared-objects/<slug>` — the ro-bind mount of the shared object store.
- **On the host** (during pull/push/cleanup): the real host path like `~/.local/share/coding-cave/projects/<slug>/gitdir/objects`.

`create_session_gitdir` writes the host path. `start_session` rewrites it to the cave path before bwrap. Code that reads git objects from a session gitdir on the host must ensure the alternates point to the host path first.

### Live sync vs final cleanup

There are two distinct operations that move refs from session gitdirs to the shared gitdir. They have different safety requirements because one operates on live sessions and the other on dead ones.

**Live sync** (`forward_session_refs` + `ff_session_branches`): called by `pull` and `push` to pick up in-flight commits from running sessions. `forward_session_refs` forwards each session's refs (including `session/<sid>` branches) to shared. Then `ff_session_branches` attempts FF-merges of all session branches to main so pull/push can see session commits. Decision logic for each branch ref in `_forward_refs`:

1. **Unchanged** (ref == `refs/init` or `refs/synced`) → skip. Session never touched this branch, or hasn't changed since last sync.
2. **Fast-forward** → update shared, record `refs/synced`.
3. **Session behind shared** → skip.
4. **Diverged, same tree** → update if session changed since sync (squash/message-amend preserving content), skip otherwise (re-authoring metadata).
5. **Diverged, different tree, session rewrote its own commits** → session wins. Detected by: `refs/synced` exists AND synced tree == shared tree (shared only re-authored, no new content from push/other session).
6. **Diverged, different tree, first sync after amend** → session wins. Detected by: `refs/init` == shared ref.
7. **Diverged, different tree, shared has new content** → try rebase (`_rebase_onto_reauthored`, then `_rebase_commits`). If rebase fails, skip (cleanup will handle it).

Key safety invariant: live sync only lets a session "win" (replace the shared ref) when it can verify shared has no new content since the last sync. If shared has new content (from push or another session), live sync either rebases or skips. This prevents one session's rewrite from clobbering another session's commits or host pushes.

Tracking refs in session gitdirs:

- `refs/init/<branch>` — shared ref at session creation. Identifies sessions that never touched a branch.
- `refs/synced/<branch>` — session ref at last successful sync. Identifies sessions unchanged since last sync. Also used to detect whether shared only re-authored: if `tree(refs/synced) == tree(shared)`, shared's content hasn't changed.

Safety constraints:

- **Read-only from the session's perspective**: must not modify the session gitdir in any way visible to the running agent. Alternates must be temporarily rewritten for host-side git operations, then restored.
- **Never clobber new content**: only replace shared refs when the session is the sole authority (shared hasn't gained new content since last sync).

**Final cleanup** (`merge_session_gitdirs`): called when a session exits. The session is dead, its overlay is about to be deleted. Safety constraints are relaxed:

- **Force-update is acceptable**: this is the session's last chance to push its refs. If it diverges from shared, force-update and back up the old shared ref under `refs/sessions/<sid>/`.
- **Alternates don't need restoration**: the session gitdir is about to be deleted.
- **All refs must be preserved**: even divergent refs get backed up, so no work is lost.

### Host identity forwarding and GPG signing

The cave forwards the host's git identity (`user.name`, `user.email`) via environment variables. When available, cave commits use the host's real identity. Pull detects this by comparing the cave commit author email to the host's configured email. When they match, pull skips re-authoring entirely — commit hashes are preserved through the pull/push cycle.

This eliminates the fundamental source of sync complexity: re-authoring created new commit hashes for the same tree content, causing hash divergence between sessions and the shared gitdir. With host identity forwarding, there is no hash divergence. Sessions, shared gitdir, and host all use the same commit objects.

GPG commit signing is supported via agent socket forwarding. The host's `gpg-agent` socket is bind-mounted into the cave (read-write). The host's public keyring is bind-mounted read-only. The private key never enters the cave. When the cave runs `git commit -S`, gpg talks to the forwarded socket, the host's gpg-agent signs with the private key, and returns the signature.

Falls back to throwaway identity (`coding-cave <coding-cave@noreply>`) when the host's git identity is not configured. In this mode, pull re-authors commits as before.

### Session merge and re-authoring (legacy mode)

When the cave uses the throwaway identity (host identity not available), pull re-authors cave commits to replace the cave identity with the host user's. This creates new commit hashes for the same tree content. Push then writes these re-authored hashes back to the shared gitdir. But running sessions still have the OLD (pre-re-authoring) commit chain. When a session makes new commits, they build on the old chain, creating a divergence from the shared gitdir.

Without handling this, every session merge would force-update the shared gitdir (undoing the re-authoring), and every subsequent pull would require `--force`. With multiple agents, this cycle never ends.

The fix: when `merge_session_gitdirs` detects that a session's chain diverges from the shared gitdir, it checks whether the divergence is caused by re-authoring — commits with the same tree but different hashes. If it finds a re-authoring boundary, it rebases only the session's NEW commits (those beyond the boundary) onto the shared HEAD.

Algorithm:

1. Session has `initial -> A -> B -> C` (A, B are old; C is new work)
2. Shared has `initial -> A' -> B'` (A', B' are re-authored: same trees as A, B but different hashes)
3. Build a tree->commit map from the shared chain
4. Walk the session chain backward from HEAD. For each commit, check if its tree matches one in the shared chain
5. The first match is the "re-authoring boundary" — session commit B has the same tree as shared commit B'
6. Commits above the boundary (C) are "new work" to rebase
7. For each new commit, create a re-parented copy via `git commit-tree`, preserving the original tree and author metadata but changing the parent to the shared HEAD
8. Update the shared ref to the final rebased commit

This uses `git commit-tree` (not `git rebase`) because it works on bare gitdirs without a working tree. It's correct because re-authoring preserves tree hashes: if B and B' have the same tree, then C's tree (which includes B's changes) is already correct when parented on B'.

The result: shared gitdir has `initial -> A' -> B' -> C'`, which is a clean fast-forward from the host's perspective. The next pull produces a normal fast-forward pull instead of requiring `--force`.

## Pull

Pull brings committed cave work to the host. Uncommitted changes are not transferred — with multiple sessions per project, there is no single working tree to pull from. Agents must commit their work for it to be pullable.

Pull is branch-aware: it iterates all branches in the cave gitdir and pulls each one that has new commits to the corresponding host branch. Branches are never crossed — cave commits on `develop` go to host's `develop`, not to whatever branch the host has checked out.

For the host's currently checked-out branch, pull uses checkout-based rebase (with stash/unstash to preserve uncommitted changes). For other branches, pull uses git plumbing (merge-tree + commit-tree + update-ref) so it never touches the working tree.

When the cave uses the host's git identity (the default when `user.name`/`user.email` are configured on the host), pull preserves commit hashes — fast-forwards for the host identity case are true fast-forwards. After pulling, it syncs refs back to the cave gitdir so both repos share the same commit objects.

When the cave uses the throwaway identity (legacy mode), pull additionally re-authors commits to replace `coding-cave <coding-cave@noreply>` with the host user's git identity.

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

This is why the force path uses `reset --hard` (adopting exact commit objects) rather than rebase+re-author (which creates new hashes).

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

The project slug can be omitted (`:path`) if CWD is inside a registered project. When multiple sessions are running for the same project, `cp` prompts which to use.

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

`coding-cave update-plugins` runs `nix flake update` in the user-env directory.

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
