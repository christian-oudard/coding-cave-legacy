## Coding Cave

- You are in a Coding Cave, a sandboxed bwrap namespace. You have full autonomy within the cave.
- Projects are mounted at `/projects/<name>`. `/home/cave-coder` persists across sessions.
- There is no sudo. If you need changes to the cave environment, ask the user.
- Install Nix packages with `nix shell nixpkgs#<pkg> --command <cmd>`, `nix run nixpkgs#<pkg>`, or `echo '<cmd>' | nix develop` for projects with a flake.nix devShell.
- Web requests work (credentials are injected automatically), but there are no SSH keys or API tokens in the environment.
- If the cave was launched with `--port`, host TCP ports are reachable at `$CODING_CAVE_GATEWAY:<port>`.

## Git

You work on the base branch (e.g. `main`). Your commits are automatically pushed to the shared remote at `host-cave`, where they appear under your dev branch name (managed by the cave, invisible to you). Commits from the user and other agents come in on `host-cave/<branch>`. To sync, run `git fetch host-cave && git rebase host-cave/<branch>`. You cannot overwrite the base branch on the remote. The user reviews your work and approves it to merge.

You are responsible for a clean git history. Organize work-in-progress into focused commits. You can freely amend, and squash commits on your dev branch. To undo commits, use `git reset`; do not use `git revert`.
