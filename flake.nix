{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    crinj = {
      url = "github:christian-oudard/crinj";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    claude-code = {
      url = "github:sadjow/claude-code-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    crane.url = "github:ipetkov/crane";
  };

  outputs = { self, nixpkgs, crinj, claude-code, home-manager, crane }:
    let
      systems = [ "x86_64-linux" ];
      eachSystem = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs {
        inherit system;
        config.allowUnfreePredicate = pkg: (nixpkgs.lib.getName pkg) == "claude-code";
      }));

      perSystem = eachSystem (pkgs:
        let
          craneLib = crane.mkLib pkgs;
          root = ./.;

          # Cargo-only source for deps (stable when only .rs/.sql change)
          depsSrc = craneLib.cleanCargoSource (craneLib.path root);

          # Full source including non-Rust files needed at build time
          src = pkgs.lib.fileset.toSource {
            inherit root;
            fileset = pkgs.lib.fileset.unions [
              (craneLib.fileset.commonCargoSources root)
              (pkgs.lib.fileset.fileFilter (file: file.hasExt "sql") root)
            ];
          };

          commonArgs = {
            pname = "cav";
            version = "0.1.0";
            inherit src;
            doCheck = false;
            nativeBuildInputs = [ pkgs.makeWrapper ];
          };

          # Single shared deps derivation: cached when Cargo.lock is unchanged
          cargoArtifacts = craneLib.buildDepsOnly (commonArgs // { src = depsSrc; });

          # Build cav-seccomp-gen alone so sandbox.nix can use it without
          # creating a circular dependency (cav -> sandbox -> cav).
          cavSeccompGen = craneLib.buildPackage {
            pname = "cav-seccomp-gen";
            version = "0.1.0";
            inherit src cargoArtifacts;
            cargoBuildFlags = [ "--bin" "cav-seccomp-gen" ];
            doCheck = false;
          };

          sandbox = import ./lib/sandbox.nix { inherit pkgs nixpkgs cavSeccompGen; };

          buildConfig = builtins.toJSON {
            version = self.shortRev or "dev";
            versionDate = toString (self.lastModified or 0);
            cavBin = "${builtins.placeholder "out"}/bin";
            sandboxConfig = "${sandbox}";
            nixpkgsUrl = "path:${nixpkgs}";
            claudeCodeNixUrl = "path:${claude-code}";
            homeManagerUrl = "path:${home-manager}";
            codingCaveUrl = "path:${self}";
          };

          # unshare --map-user/--map-group requires util-linux >= 2.39.
          cav = assert pkgs.lib.versionAtLeast pkgs.util-linux.version "2.39";
            craneLib.buildPackage (commonArgs // {
            inherit cargoArtifacts;

            postInstall = ''
              mkdir -p $out/share/coding-cave
              cat > $out/share/coding-cave/build.json <<'JSON'
              ${buildConfig}
              JSON

              wrapProgram $out/bin/cav \
                --set-default CODING_CAVE_BUILD_CONFIG \
                  "$out/share/coding-cave/build.json" \
                --prefix PATH : ${pkgs.lib.makeBinPath [
                  pkgs.git pkgs.gnupg pkgs.socat pkgs.slirp4netns pkgs.iptables
                  pkgs.util-linux
                  crinj.packages.${pkgs.stdenv.hostPlatform.system}.default
                ]}
            '';

            meta.mainProgram = "cav";
          });

          tests = craneLib.cargoTest {
            pname = "cav-tests";
            version = "0.1.0";
            inherit src cargoArtifacts;
            nativeBuildInputs = [ pkgs.git pkgs.jq ];
            cargoTestFlags = [
              "--bin" "cav"
              "--bin" "cav-pre-receive"
              "--test" "hook"
              "--test" "cli"
              "--test" "statusline"
            ];
          };

        in {
          packages = { default = cav; inherit sandbox; };
          checks = { build = cav; inherit tests; };
        }
      );

    in {
      lib.mkCaveEnv = import ./lib/mkCaveEnv.nix {
        inherit nixpkgs home-manager;
      };

      homeModules.claudeCode = ./modules/claude-code.nix;
      homeModules.default = self.homeModules.claudeCode;

      packages = nixpkgs.lib.mapAttrs (_: s: s.packages) perSystem;
      checks   = nixpkgs.lib.mapAttrs (_: s: s.checks)   perSystem;

      devShells = eachSystem (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            cargo rustc rustfmt clippy
            git jq socat iptables util-linux slirp4netns
          ];
        };
      });
    };
}
