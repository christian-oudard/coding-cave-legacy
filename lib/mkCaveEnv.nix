# mkCaveEnv: build a cave user environment from a home-manager config.
#
# Wraps home-manager.lib.homeManagerConfiguration with the cave's
# defaults (cave-system + claude-code modules) and a user module from
# the generated wrapper flake. Output is a derivation containing:
#
#   files/      — fully materialized HM home file tree (no store symlinks)
#   home-path/  — symlink to HM's merged home.path buildEnv
#   env.sh      — exports for HM's home.sessionVariables
#
# The sandbox entry script copies files/ into $HOME and adds
# home-path/bin to PATH. Real files are required because the cave's
# persistent home survives across sessions but the nix store overlay
# does not.

{ nixpkgs, home-manager }:

{ claude-code, userModule }:

let
  systems = [ "x86_64-linux" "aarch64-linux" ];

  mkFor = system:
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

      sessionVarsFile = pkgs.writeText "cave-env.sh"
        (pkgs.lib.concatStringsSep "\n"
          (pkgs.lib.mapAttrsToList
            (k: v: ''export ${k}="${toString v}"'')
            hmEval.config.home.sessionVariables));

    in
    pkgs.runCommand "cave-user-env" { nativeBuildInputs = [ pkgs.rsync ]; } ''
      mkdir -p $out/files
      rsync -rltL --chmod=u+rwX,go=rX ${hmEval.config.home-files}/ $out/files/
      ln -s ${hmEval.config.home.path} $out/home-path
      cp ${sessionVarsFile} $out/env.sh
    '';

in {
  packages = nixpkgs.lib.genAttrs systems (system: {
    default = mkFor system;
  });
}
