## Coding Cave

- You are in a Coding Cave, a sandboxed bwrap namespace with nix packages. You have full autonomy within the cave.
- Projects are mounted as overlays at `/projects/<name>`. Commits stay in the cave's gitdir until the user runs `coding-cave pull` on the host.
- There is no sudo. If you need changes to the host outside the cave, ask the user.
- Install Nix packages with `nix shell nixpkgs#<pkg> --command <cmd>` or `nix run nixpkgs#<pkg>`.
