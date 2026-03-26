## Coding Cave

- You are in a Coding Cave, a sandboxed bwrap namespace with nix packages. You have full autonomy within the cave.
- Projects are mounted at `/projects/<name>`. No code changes leave the cave until the user explicitly pulls.
- There is no sudo. If you need changes to the host outside the cave, ask the user.
- Install Nix packages with `nix shell nixpkgs#<pkg> --command <cmd>`, `nix run nixpkgs#<pkg>`, or `nix develop` for projects with a flake.nix devShell.

## Git

Work on your agent branch, deliver to `main` via pushing the `cave` remote (ff-only, enforced by hook).

- Sync: `git fetch cave && git rebase cave/main`
- Save WIP: `git push cave HEAD`
- Deliver: `git push cave HEAD:main`

You are responsible for delivering a clean git history. Squash WIP into focused commits before delivering. Don't deliver until the change is final. Your agent branch is yours to amend and force-push freely. Main is shared, clean history only.

The user runs `cav pull` on the host to fetch your delivered commits, and `cav push` to send theirs into the cave.

Shared branches are any branch on `cave` not prefixed with `agent-`. Only the user rewrites shared history. If history needs fixing, clean up your agent branch and let the user adopt it.
