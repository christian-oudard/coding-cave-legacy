# Design

Intended behavior for coding-cave, described in the abstract.

## Full autonomy inside, full control outside

The cave gives agents unrestricted access (`--dangerously-skip-permissions`) within a security boundary. The agent can install packages, run arbitrary commands, modify any file. But nothing escapes: the host filesystem is untouched until the user explicitly pulls. The user reviews and controls what crosses the boundary.

## The host is the source of truth

The host repo is canonical. The cave works on an overlay copy. `pull` brings cave work to the host; `push` brings host state to the cave. The cave never modifies the host directly.

`reset` is the escape hatch: wipe everything in the cave and start fresh from the host. It always works, regardless of how broken the cave state is.

## Persistence across sessions

The cave is a long-lived namespace, not a container that starts and stops per invocation. Multiple agents can work simultaneously inside the same cave via `nsenter`. The cave starts on first entry and stops when the last session exits.

State that persists: home directory (Claude Code config, history), nix store overlay (installed packages), project overlays (file changes), and gitdirs (commits). A fresh `coding-cave` invocation picks up exactly where the last one left off.

## Git isolation via separate gitdir

The cave's `.git` is not on the overlay. It's a separate clone (gitdir) bind-mounted into place. This avoids overlayfs bugs with atomic rename (which git uses for index and ref updates) and keeps git operations fast and reliable inside the cave.

The gitdir is the shared channel between host and cave for committed work. `pull` fetches from it; `push` updates it.

## Pull

Pull brings cave work to the host. It handles two kinds of changes: commits and uncommitted working tree modifications.

### Committed changes

Pull fetches from the cave's gitdir, rebases cave commits onto the host branch, and re-authors them to replace the throwaway cave identity (`coding-cave <coding-cave@noreply>`) with the host user's git identity. After rebasing, it syncs the gitdir back so both repos share the same commit hashes.

The rebase uses `--no-reapply-cherry-picks` to skip commits already present on the host. Conflicts pause the rebase and print instructions for manual resolution.

### Working tree changes

After pulling commits, pull checks for uncommitted changes in the cave (tracked/staged diffs against HEAD). If found, it generates a patch and applies it to the host working tree, staging the result. This is refused if the host already has uncommitted changes, to avoid conflicts.

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

Push brings host state to the cave. It updates the gitdir's refs and objects to match the host, then optionally transfers uncommitted working tree changes.

### Safety: unpulled cave commits

Push refuses if the cave has commits that haven't been pulled yet, since updating the gitdir would lose them. It shows the unpulled commits and suggests `coding-cave pull` first, or `--force` to override.

### Working tree changes

After updating commits, push checks for uncommitted host changes (tracked/staged diffs against HEAD). If found, it generates a patch and applies it to the cave overlay, staging the result. This is refused if the cave already has uncommitted changes, to avoid conflicts.

### Immediate visibility

The gitdir is bind-mounted into the cave, so push updates are immediately visible to running agents. No cave restart needed.

## Credential isolation

Credentials enter the cave only as environment variables (`--clearenv` + explicit forwarding). Nothing is written to disk inside the cave. OAuth tokens, API keys, and GitHub tokens are forwarded from the host config at entry time.

## Nix for reproducibility, overlayfs for mutability

The environment is defined in a Nix flake. The nix store is mounted as an overlay so agents can `nix shell` additional packages without affecting the host. Project files are also on overlays: the host's files are the read-only lower layer, cave modifications go to the upper layer.

fuse-overlayfs is used instead of kernel overlayfs because kernel overlayfs in user namespaces can't copy-up files with unmapped GIDs.
