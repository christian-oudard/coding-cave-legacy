## Coding Cave

- You are in a Coding Cave, a sandboxed bwrap namespace with nix packages. You have full autonomy within the cave.
- Projects are mounted as overlays at `/projects/<name>`. Commits stay in the cave's gitdir until the user runs `coding-cave pull` on the host.
- `cav pull` = cave-to-host, `cav push` = host-to-cave. You are inside the cave. Before telling the user to sync, check what the host has: `git fetch shared && git rev-parse refs/remotes/host/<branch>`.
- There is no sudo. If you need changes to the host outside the cave, ask the user.
- Install Nix packages with `nix shell nixpkgs#<pkg> --command <cmd>` or `nix run nixpkgs#<pkg>`.

## Inter-session communication

Other cave agents may be running in parallel sessions. You can send and receive messages via `cave-send`.

### Receiving messages

Messages are delivered automatically via hooks after tool calls and before stops. They are prefixed with a sender tag:

- `project@N: ...` is from another agent session (e.g. `coding-cave@4: hello`).
- `user: ...` is from the host user relaying through cave-send.

These are NOT regular user prompts. They are messages from peer agents or the host. Respond to them via `cave-send`, not by treating them as task instructions (unless the content is a task instruction from the user).

### Sending messages

- `cave-send project@N "your message"` sends by project and session index (e.g. `cave-send crinj@1 "hello"`).
- `cave-send --list` shows active sessions with their project and index.
- `cave-send` exits 0 on success, non-zero on failure (e.g. target not found).
- Messages are automatically tagged with your identity.
- Your session ID is `$CODING_CAVE_SESSION_ID`, your index is `$CODING_CAVE_SESSION_INDEX`.

### Sharing code between sessions

- To share uncommitted code with other sessions, commit and push to the shared gitdir: `git push shared main`
- Other sessions can then `git fetch shared && git rebase shared/main` to pick up your changes.
