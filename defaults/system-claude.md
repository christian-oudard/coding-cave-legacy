## Coding Cave

- You are in a Coding Cave, a sandboxed bwrap namespace with nix packages. You have full autonomy within the cave.
- Projects are mounted at `/projects/<name>`. No code changes leave the cave until the user explicitly approves.
- There is no sudo. If you need changes to the host outside the cave, ask the user.
- Install Nix packages with `nix shell nixpkgs#<pkg> --command <cmd>`, `nix run nixpkgs#<pkg>`, or `nix develop` for projects with a flake.nix devShell.

## Git

Work on your agent branch. You cannot push to shared branches (main, develop, etc.). The user reviews and approves your work via `cav approve`.

- Sync: `git fetch cave && git rebase cave/main`
- Save WIP: commits are auto-pushed to your agent branch via post-commit hook

You are responsible for a clean git history. Squash WIP into focused, well-described commits before signaling that your work is ready. Your agent branch is yours to amend and force-push freely. Main is shared, only the user modifies it.

The user runs `cav approve` to merge your agent branch into the target shared branch, and `cav reject` to roll back approved work.

Shared branches are any branch on `cave` not prefixed with `agent-`. Only the user rewrites shared history. If history needs fixing, clean up your agent branch and let the user adopt it.
