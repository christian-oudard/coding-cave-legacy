# mkCaveEnv: build a cave user environment from a home-manager config.
#
# Wraps home-manager.lib.homeManagerConfiguration with the cave's defaults
# (cave-system + claude-code modules) and a user module from the generated
# wrapper flake. Output is a derivation containing:
#
#   files/      -- fully materialized HM home file tree (no store symlinks)
#   home-path/  -- symlink to HM's merged home.path buildEnv
#   env.json    -- JSON object of HM's home.sessionVariables
#
# Ported from `main`'s lib/mkCaveEnv.nix, which builds the same three outputs
# on the host for a bwrap cave. Two things differ, and both follow from the
# guest being a machine rather than a mount namespace.
#
# **One environment, many homes.** `main` builds a cave-user-env per cave and
# materializes it into that cave's home. Here there is one spec for the whole
# guest (docs/vm-cave-design.md: "It is what agents get and what `cav shell`
# gets, because those are the same environment"), built once and materialized
# into every agent's home by seed-agent-home@. So the eval has to name a home
# directory that no agent actually has. Nothing in a cave.nix may interpolate
# `config.home.homeDirectory` and expect it to mean the agent's home: it will
# read /home/cave, which exists nowhere. Use `~`, which claude and every shell
# expand at the point of use, or a path relative to the home file's own
# location.
#
# **files/ stays real, and not for the reason `main` gives.** On `main` a
# cave's home persists while its nix store overlay does not, so a store
# symlink in the home would dangle. Here the store is the same persistent
# ext4 as the home and that reason is gone -- but a second one is not: claude
# writes to its own settings.json (`/model`, `/config`), and a read-only store
# symlink turns that into an error. Real, writable files keep the tool able to
# edit its own configuration for the length of a session; the next build is
# what puts the declared value back.

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

      # home.sessionVariables as a flat JSON object. cave-agent applies these
      # before exec'ing claude, because a systemd unit reads neither HM's
      # hm-session-vars.sh nor environment.sessionVariables.
      sessionVarsFile = pkgs.writeText "cave-env.json"
        (builtins.toJSON
          (pkgs.lib.mapAttrs (_: v: toString v)
            hmEval.config.home.sessionVariables));

    in
    pkgs.runCommand "cave-user-env" { nativeBuildInputs = [ pkgs.rsync ]; } ''
      mkdir -p $out/files
      rsync -rltL --chmod=u+rwX,go=rX ${hmEval.config.home-files}/ $out/files/
      ln -s ${hmEval.config.home.path} $out/home-path
      cp ${sessionVarsFile} $out/env.json
    '';

in {
  packages = nixpkgs.lib.genAttrs systems (system: {
    default = mkFor system;
  });
}
