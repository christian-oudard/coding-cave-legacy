## Coding Cave

- You are in a Coding Cave, a sandboxed bwrap namespace. You have full autonomy within the cave.
- Projects are mounted at `/projects/<name>`. `/home/cave-coder` persists across sessions.
- There is no sudo. If you need changes to the cave environment, ask the user.
- Install Nix packages with `nix shell nixpkgs#<pkg> --command <cmd>`, `nix run nixpkgs#<pkg>`, or `nix develop` for projects with a flake.nix devShell.
- Web requests work (credentials are injected automatically), but there are no SSH keys or API tokens in the environment.

## Git

Work on your dev branch, named `agent-<n>/<branch>`, where `<n>` is your agent number, and `<branch>` is the base branch. You cannot push to base branches (main etc.). The user reviews your work and approves it to merge to the base branch. Your commits are automatically pushed to the shared remote at `host-cave`. Commits from the user and other coding cave agent sessions come in on `host-cave/<branch>`. To sync with the base branch, run `git fetch host-cave && git rebase host-cave/<branch>`

You are responsible for a clean git history. Organize work-in-progress into focused commits. You can freely amend, and squash commits on your dev branch. To undo commits, use `git reset`; do not use `git revert`.
