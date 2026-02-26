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

Each agent session runs in its own bwrap namespace with its own overlay and its own gitdir. Multiple agents working on the same project get independent working trees and independent git state — they cannot see or interfere with each other's uncommitted work or HEAD position. Agents interact only through the shared gitdir, which receives commits on session cleanup.

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

On session end, refs and new objects from the session gitdir are merged into the shared gitdir, then the session gitdir is removed. `pull` and `push` operate on the shared gitdir as before.

## Pull

Pull brings committed cave work to the host. Uncommitted changes are not transferred — with multiple sessions per project, there is no single working tree to pull from. Agents must commit their work for it to be pullable.

Pull fetches from the cave's gitdir, rebases cave commits onto the host branch, and re-authors them to replace the throwaway cave identity (`coding-cave <coding-cave@noreply>`) with the host user's git identity. After rebasing, it syncs the gitdir back so both repos share the same commit hashes.

The rebase uses `--no-reapply-cherry-picks` to skip commits already present on the host. Conflicts pause the rebase and print instructions for manual resolution.

### History rewrites require --force

Pull must not silently rewrite the host's history. When the cave rewrites history (squash, amend, rebase), pull detects this via `refs/cave-sync` — a marker of the last commit both repos agreed on. If the host hasn't moved since the last sync but the merge-base is older, the cave rewrote history.

Without `--force`, pull warns and exits:

    Cave rewrote history for project (5 host commit(s) → 1 cave commit(s)).
    Run 'coding-cave pull --force' to adopt the cave's history.

With `--force`, pull adopts the cave's history directly (`reset --hard`) rather than rebasing. This preserves commit hashes that may already exist on a remote, keeping the host fast-forwardable from its tracking branch.

### Preserving remote-pushed history

Pull must not create commits that diverge from an already-pushed remote branch:

- Commits previously pulled and pushed to a remote keep their exact hashes in subsequent pulls.
- The result is always fast-forwardable from the remote tracking branch (assuming no independent host-side changes).

This is why the force path uses `reset --hard` (adopting exact commit objects) rather than rebase+re-author (which creates new hashes).

## Push

Push brings host commits to the cave. It updates the gitdir's refs and objects to match the host. Uncommitted host changes are not transferred.

Push refuses if the cave has commits that haven't been pulled yet, since updating the gitdir would lose them. It shows the unpulled commits and suggests `coding-cave pull` first, or `--force` to override.

The gitdir is bind-mounted into the cave, so push updates are immediately visible to running agents. No cave restart needed.

## Cave-originated projects

Projects can be created inside the cave without a host source directory. The agent runs `cav-init <name>`, which creates a git repo at `~/unsynced-projects/<name>/` (persistent home). The agent works there directly.

On the host, `cav pull <name>` discovers the unsynced project, clones it to the host CWD, and registers it as a normal project. The next cave session mounts it at `/projects/<name>/` with overlay + gitdir like any other project.

Two locations make the state obvious: `/projects/` = synced with host, `~/unsynced-projects/` = cave-only.

## Credential isolation

Credentials enter the cave only as environment variables (`--clearenv` + explicit forwarding). Nothing is written to disk inside the cave. OAuth tokens, API keys, and GitHub tokens are forwarded from the host config at entry time.

## Nix for reproducibility, fuse-overlayfs for mutability

The environment is defined in a Nix flake. The nix store is mounted as an overlay so agents can `nix shell` additional packages without affecting the host. Project files are also on overlays: the host's files are the read-only lower layer, cave modifications go to the upper layer.

All overlays use fuse-overlayfs. Kernel overlayfs in user namespaces has two problems: forced `uuid=on` remaps inodes causing EOVERFLOW on ext4, and the CVE-2023-0386 copy-up check requires all lower-layer UIDs/GIDs to be mapped but unprivileged users can only map one. fuse-overlayfs handles copy-up in userspace, bypassing both. The wrapper mounts all overlays before spawning bwrap and passes merged dirs via `--bind`. `/dev/fuse` is exposed inside the cave for nested caves.
