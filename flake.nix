{
  description = "Coding Cave (vm-cave): dev shell for the bootstrap VM tooling";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in
    {
      # Tools vmcav uses at runtime, so it need not provision them from nix
      # ad hoc. `use flake` in .envrc loads this; then vmcav finds qemu, ssh,
      # setsid, socat, etc. on PATH. The guest image is its own flake in guest/.
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          # qemu is intentionally absent: the guest VM build bakes it and the
          # runner calls it by absolute path, so pulling the full qemu closure
          # into the shell would just be a ~1 GB download for nothing.
          packages = with pkgs; [
            openssh
            util-linux   # setsid
            socat        # QMP control socket
            git
            python3      # config parsing (tomllib)
            jq
            go           # cave-vm module (host + guestd binaries)
          ];
        };
      });
    };
}
