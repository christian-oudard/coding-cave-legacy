## Coding Cave

- You are in a Coding Cave, a sandboxed bubblewrap namespace. You have full autonomy within the cave, but cannot access the host system.
- Projects are mounted at `/projects/<name>/`.
- Your home directory at `/home/cave-coder` persists across sessions. However, `~/.claude/` is automatically reset each session. You cannot write directly to any `.claude` directory, and if you try to use the Write tool on it, the session will block; instead use a Python script to access these Claude config folders.
- There is no sudo. Install system packages with `nix shell nixpkgs#<pkg> --command <cmd>`, `nix run nixpkgs#<pkg>`, or `nix develop`.
- You can make queries to services that require an API key. Send a placeholder credential, and the injection proxy "crinj" will inject correct credentials. There are no SSH keys or API tokens in the cave environment.
- Localhost ports are reachable at `$CODING_CAVE_GATEWAY:<port>` if the user has used the `--port` argument.

## Git

Commit your changes any time you finish a task; the user cannot see your changes until they are committed. Your commits are automatically pushed to the shared remote at `host-cave`. Commits from the user and other agents come in on `host-cave/<branch>`. To sync with new changes, run `git fetch host-cave && git rebase host-cave/<branch>`. You cannot push to `host-cave/<branch>`, the user must approve your changes and merge them from the host. You can freely amend and squash commits on your branch. To undo commits, use `git reset`; do not add a reversion commit with `git revert`.
