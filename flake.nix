{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    jail-nix.url = "sourcehut:~alexdavid/jail.nix";
  };

  outputs = { nixpkgs, jail-nix, ... }: let
    pkgs = import nixpkgs { system = "x86_64-linux"; };
    jail = jail-nix.lib.init pkgs;

  in {
    packages.x86_64-linux.default = jail "codingcave" pkgs.bashInteractive (with jail.combinators; [
      (set-hostname "coding-cave")
      network
      no-new-session
    ]);
  };
}
