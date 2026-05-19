# mkCaveEnv: build a cave user environment from a home-manager config.
#
# Wraps home-manager.lib.homeManagerConfiguration with the cave-system
# and claude-code modules plus a user module from the wrapper flake.
# Output is a derivation containing:
#
#   files/      — fully materialized HM home file tree (no store symlinks)
#   home-path/  — symlink to HM's merged home.path buildEnv
#   env.json    — JSON object of HM's home.sessionVariables (cav reads
#                 it inside the guest and applies entries before
#                 exec'ing the user's shell)
#
# Real files are required because the cave's persistent home survives
# across sessions but the nix store overlay does not.

{ nixpkgs, home-manager, system ? "x86_64-linux" }:

{ claude-code, userModule }:

let
  pkgs = import nixpkgs {
    inherit system;
    config.allowUnfreePredicate = pkg:
      (nixpkgs.lib.getName pkg) == "claude-code";
  };

  hmEval = home-manager.lib.homeManagerConfiguration {
    inherit pkgs;
    modules = [
      ../modules/cave-system.nix
      ../modules/claude-code.nix
      { programs.claude-code.package =
          claude-code.packages.${system}.default; }
      userModule
    ];
  };

  sessionVarsFile = pkgs.writeText "cave-env.json"
    (builtins.toJSON
      (pkgs.lib.mapAttrs (_: v: toString v)
        hmEval.config.home.sessionVariables));

in pkgs.runCommand "cave-user-env"
  { nativeBuildInputs = [ pkgs.rsync ]; }
  ''
    mkdir -p $out/files
    rsync -rltL --chmod=u+rwX,go=rX ${hmEval.config.home-files}/ $out/files/
    ln -s ${hmEval.config.home.path} $out/home-path
    cp ${sessionVarsFile} $out/env.json
  ''
