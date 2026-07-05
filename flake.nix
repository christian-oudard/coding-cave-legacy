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
              (pkgs.lib.fileset.fileFilter (file: file.hasExt "md") (root + /prompts))
            ];
          };

          commonArgs = {
            pname = "cav";
            version = "0.1.0";
            inherit src;
            doCheck = false;
            nativeBuildInputs = [ pkgs.makeWrapper pkgs.pkg-config ];
            buildInputs = [ pkgs.sqlite ];
          };

          # Per-profile deps derivations. Cached when Cargo.lock is unchanged.
          # Splitting by profile means cav-debug doesn't have to recompile
          # every dep in dev mode just because the cache was release-built.
          mkCargoArtifacts = profile: craneLib.buildDepsOnly (commonArgs // {
            src = depsSrc;
            pname = "cav-deps-${profile}";
            CARGO_PROFILE = profile;
          });
          cargoArtifactsRelease = mkCargoArtifacts "release";
          cargoArtifactsDev = mkCargoArtifacts "dev";

          # Build cav-seccomp-gen alone so sandbox.nix can use it without
          # creating a circular dependency (cav -> sandbox -> cav).
          # Built at dev profile because it runs once at sandbox-build time
          # to emit a static BPF blob; the optimization level doesn't matter.
          cavSeccompGen = craneLib.buildPackage (commonArgs // {
            cargoArtifacts = cargoArtifactsDev;
            CARGO_PROFILE = "dev";
            cargoExtraArgs = "--bin cav-seccomp-gen";
          });

          sandbox = import ./lib/sandbox.nix { inherit pkgs nixpkgs cavSeccompGen; };

          # Hash of the cave-side files that mkCaveEnv actually consumes.
          # Used by user_env::compute_hash to decide when to rebuild the
          # cave's user-env. Replaces a `path:${self}` URL — that one
          # changed on every commit to the cav repo and forced a rebuild
          # even when no relevant nix file had moved.
          caveSourceHash = builtins.hashString "sha256" (
            builtins.hashFile "sha256" (root + "/lib/mkCaveEnv.nix")
            + builtins.hashFile "sha256" (root + "/modules/cave-system.nix")
            + builtins.hashFile "sha256" (root + "/modules/claude-code.nix")
          );

          buildConfig = builtins.toJSON {
            version = self.shortRev or "dev";
            versionDate = toString (self.lastModified or 0);
            cavBin = "${builtins.placeholder "out"}/bin";
            sandboxConfig = "${sandbox}";
            nixpkgsUrl = "path:${nixpkgs}";
            # Upstream ref so each user-env locks claude-code to the latest
            # sadjow release on its first build, bumped only on an explicit
            # `cav env update claude-code`.
            claudeCodeNixUrl = "github:sadjow/claude-code-nix";
            homeManagerUrl = "path:${home-manager}";
            codingCaveUrl = "path:${self}";
            inherit caveSourceHash;
          };

          mkCav = { profile ? "release", binName ? "cav" }:
            # unshare --map-user/--map-group requires util-linux >= 2.39.
            assert pkgs.lib.versionAtLeast pkgs.util-linux.version "2.39";
            craneLib.buildPackage (commonArgs // {
              cargoArtifacts =
                if profile == "release" then cargoArtifactsRelease
                else cargoArtifactsDev;
              CARGO_PROFILE = profile;

              postInstall = ''
                mkdir -p $out/share/coding-cave
                cat > $out/share/coding-cave/build.json <<'JSON'
                ${buildConfig}
                JSON

                ${pkgs.lib.optionalString (binName != "cav") ''
                  mv $out/bin/cav $out/bin/${binName}
                ''}

                wrapProgram $out/bin/${binName} \
                  --set-default CODING_CAVE_BUILD_CONFIG \
                    "$out/share/coding-cave/build.json" \
                  --prefix PATH : ${pkgs.lib.makeBinPath [
                    # gitMinimal, not git: cav's own git ops (worktrees, hooks,
                    # refs) need no python/perl, which full git drags in (~190MB
                    # into every cave store). An agent that wants full git gets
                    # it from its cave.nix.
                    pkgs.gitMinimal pkgs.gnupg pkgs.socat pkgs.slirp4netns pkgs.iptables
                    pkgs.util-linux
                    crinj.packages.${pkgs.stdenv.hostPlatform.system}.default
                  ]}
              '';

              meta.mainProgram = binName;
            });

          cav = mkCav { };
          # Debug build keeps debug_assertions on, which enables verbose
          # timing logs in cav, supervisor, and entry script. Installed
          # under a distinct binary name so it can sit alongside the
          # release `cav` (from the host module) in nix profile.
          cavDebug = mkCav { profile = "dev"; binName = "cav-debug"; };

          tests = craneLib.cargoTest {
            pname = "cav-tests";
            version = "0.1.0";
            inherit src;
            cargoArtifacts = cargoArtifactsDev;
            nativeBuildInputs = [ pkgs.git pkgs.jq ];
            cargoTestFlags = [
              "--bin" "cav"
              "--bin" "cav-git-pre-receive"
              "--test" "hook"
              "--test" "cli"
              "--test" "statusline"
            ];
          };

          # Instantiates the host NixOS module so a broken module or broken
          # flake wiring fails here (and `nix flake check`) instead of at a
          # user's nixos-rebuild. Forcing the installed package names forces
          # the module fixpoint without building the system or cav, so
          # `nix eval .#checks.<sys>.hostModuleEval.drvPath` is a cheap,
          # build-free, tree-accurate gate (see check_module.sh).
          hostModuleEval =
            let probe = builtins.concatStringsSep "\n" (map (p: p.name) (nixpkgs.lib.nixosSystem {
                  system = pkgs.stdenv.hostPlatform.system;
                  modules = [
                    self.nixosModules.codingCave
                    { boot.isContainer = true; system.stateVersion = "24.11"; }
                  ];
                }).config.environment.systemPackages);
            in pkgs.runCommand "host-module-eval" { inherit probe; } "printf '%s' \"$probe\" > $out";

        in {
          packages = {
            default = cav;
            cav-debug = cavDebug;
            inherit sandbox;
          };
          checks = { build = cav; inherit tests hostModuleEval; };
        }
      );

    in {
      lib.mkCaveEnv = import ./lib/mkCaveEnv.nix {
        inherit nixpkgs home-manager;
      };

      homeModules.claudeCode = ./modules/claude-code.nix;
      homeModules.default = self.homeModules.claudeCode;

      nixosModules.codingCave = { pkgs, ... }: {
        imports = [ ./modules/host.nix ];
        # Inject cav as a module arg so host.nix evaluates as a normal NixOS
        # module (config/lib/pkgs from the module system, cav from here).
        _module.args.cav = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      };
      nixosModules.default = self.nixosModules.codingCave;

      packages = nixpkgs.lib.mapAttrs (_: s: s.packages) perSystem;
      checks   = nixpkgs.lib.mapAttrs (_: s: s.checks)   perSystem;

      devShells = eachSystem (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            cargo rustc rustfmt clippy cargo-llvm-cov
            git jq socat iptables util-linux slirp4netns
            pkg-config sqlite
          ];
          # nixpkgs rustc omits llvm-tools-preview; point cargo-llvm-cov at the
          # version-matched llvm bundled with rustc.
          LLVM_COV = "${pkgs.rustc.llvm}/bin/llvm-cov";
          LLVM_PROFDATA = "${pkgs.rustc.llvm}/bin/llvm-profdata";
        };
      });
    };
}
