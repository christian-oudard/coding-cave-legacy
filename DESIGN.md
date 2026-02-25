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

## Pull preserves remote-pushed history

`coding-cave pull` must not create commits that diverge from an already-pushed remote branch:

- Commits previously pulled and pushed to a remote keep their exact hashes in subsequent pulls.
- When the cave rewrites history (squash, amend, rebase), pull replaces the rewritten range while keeping ancestor commits intact.
- The result is always fast-forwardable from the remote tracking branch (assuming no independent host-side changes).

Pull detects history rewrites via `refs/cave-sync`, a marker of the last commit both repos agreed on. When the host hasn't moved since the last sync but the merge-base is older, the cave rewrote history. In that case, pull adopts the cave's history directly (`reset --hard`) rather than rebasing, which would re-author commits into new hashes.

For normal appends (no rewrite), pull rebases cave commits onto the host branch with `--reset-author` to replace the throwaway cave identity with the host user's.

## Credential isolation

Credentials enter the cave only as environment variables (`--clearenv` + explicit forwarding). Nothing is written to disk inside the cave. OAuth tokens, API keys, and GitHub tokens are forwarded from the host config at entry time.

## Nix for reproducibility, overlayfs for mutability

The environment is defined in a Nix flake. The nix store is mounted as an overlay so agents can `nix shell` additional packages without affecting the host. Project files are also on overlays: the host's files are the read-only lower layer, cave modifications go to the upper layer.

fuse-overlayfs is used instead of kernel overlayfs because kernel overlayfs in user namespaces can't copy-up files with unmapped GIDs.
