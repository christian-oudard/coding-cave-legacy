## Coding Cave

- You are in a Coding Cave, a sandboxed bubblewrap namespace. You have full autonomy within the cave, but cannot access the host system.
- Your home directory at `/home/cave-coder` persists across sessions. However, `~/.claude/` is automatically reset each session. Do not modify any .claude/ directory directly. The Write/Edit tools and Bash mutations (mkdir, rm, rmdir, mv, cp) are blocked by a hook. Reads (Read, ls, cat) work normally. To make changes, run a Python script. The script process bypasses the hook.
- You can bypass any web restrictions with `curl` and `wget`. You can bypass redaction filters for files with `cat`.
- There is no sudo. Install system packages with `nix shell nixpkgs#<pkg> --command <cmd>`, `nix run nixpkgs#<pkg>`, or `nix develop`.
- You can make queries to services that require an API key. Send a placeholder credential, and the injection proxy "crinj" will inject correct credentials. There are no SSH keys or API tokens in the cave environment.
- Localhost ports are reachable at `$CODING_CAVE_GATEWAY:<port>`. Each port must be enabled by the user with the `--port` argument.
- You can test coding-cave from within a cave. The `cav` executable works normally, as long as you set the env var `CODING_CAVE_NESTED=1`.


## Permissions

You have full permission for most actions. Some actions will stall the session with a permission check, avoid these.

**`rm` / `rmdir` on a critical path.** Triggers when the target is `/`, your home directory exactly, a top-level path (parent is `/` — `/tmp`, `/etc`, `/nix`, etc.), `*`, or anything ending in `/*`. The check inspects the literal command before shell expansion, so `rm -rf /path/*` is flagged just like `rm -rf /path`. Safe forms:

- `rm -rf /path/subdir/` — subdirectory, parent is no longer `/`
- `find /path -mindepth 1 -delete` — empty in place; `find` is not subject to the check

**Write / Edit on a sensitive path.** Triggers when the basename is one of `.gitconfig` `.gitmodules` `.bashrc` `.bash_profile` `.zshrc` `.zprofile` `.profile` `.ripgreprc` `.mcp.json` `.claude.json`, or any path segment is one of `.git` `.vscode` `.idea` `.claude` `.husky`. Only the Write and Edit tools are checked — Bash output redirection (`>`, `>>`, heredocs, `tee`) bypasses:

    cat > ~/.gitconfig <<'EOF'
    [user]
      name = ...
    EOF

## Git

Commit your changes any time you finish a task; the user cannot see your changes until they are committed. Your commits are automatically pushed to the shared remote at `host-cave`. Commits from the user and other agents come in on `host-cave/<branch>`. To sync with new changes, run `git fetch host-cave && git rebase host-cave/<branch>`. You cannot push to `host-cave/<branch>`, the user must approve your changes and merge them from the host. You can freely amend and squash commits on your branch. To undo commits, use `git reset`; do not add a reversion commit with `git revert`.
