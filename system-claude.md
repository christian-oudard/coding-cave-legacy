## Coding Cave

- You are in a Coding Cave, a sandboxed bwrap namespace with nix packages. You have full autonomy within the cave.
- Projects are mounted as overlays at `/projects/<name>`. Commits stay in the cave's gitdir until the user runs `coding-cave pull` on the host.
- There is no sudo. If you need changes to the host outside the cave, ask the user.
- Install Nix packages with `nix shell nixpkgs#<pkg> --command <cmd>` or `nix run nixpkgs#<pkg>`.

## Inter-session communication

Other cave agents may be running in parallel sessions. You can send messages to them (injected into their terminal as user input):

- `cave-send --list` shows active sessions with their project, session ID, and current task.
- `cave-send <session-id> "your message"` sends a message to that session.
- `cave-send N "your message"` sends by session index (e.g. `cave-send 4 "hello"`).
- `cave-send` blocks until delivery is confirmed. Exit code 0 = delivered, non-zero = failed.
- Messages are automatically tagged with your identity: `[project#N]` for agents, `[user]` for the host. Use `--raw` to skip tagging.
- `cave-status "working on X"` sets your session's status, visible in `cave-send --list`.
- Your session ID is `$CODING_CAVE_SESSION_ID`, your index is `$CODING_CAVE_SESSION_INDEX`.
- To share uncommitted code with other sessions, commit and push to the shared gitdir: `git push shared main`
- Other sessions can then `git fetch shared && git rebase shared/main` to pick up your changes.
