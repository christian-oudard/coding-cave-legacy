## Coding Cave

- You are in a Coding Cave, a sandboxed bwrap namespace with nix packages. You have full autonomy within the cave.
- Projects are mounted as overlays at `/projects/<name>`. Commits stay in the cave's gitdir until the user runs `coding-cave pull` on the host.
- There is no sudo. Ask the user to run commands that need root on the host.
- Install packages with `nix shell nixpkgs#<pkg> --command <cmd>` or `nix run nixpkgs#<pkg>`. Do not use `nix-env` or `nix profile` — persistent packages belong in cave.nix.
- Multiple agents may be working on the same gitdir concurrently. Only commit your own changes — if you see uncommitted changes you didn't make, leave them alone.
