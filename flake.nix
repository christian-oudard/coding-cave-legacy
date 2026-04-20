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
  };

  outputs = { self, nixpkgs, crinj, claude-code, home-manager }:
    let
      systems = [ "x86_64-linux" ];
      eachSystem = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs {
        inherit system;
        config.allowUnfreePredicate = pkg: (nixpkgs.lib.getName pkg) == "claude-code";
      }));
    in {
      lib.mkCaveEnv = import ./lib/mkCaveEnv.nix {
        inherit nixpkgs home-manager;
      };

      # Home-manager module: deep-merge layer over programs.claude-code.
      # Import this from any HM config that wants to use
      # `coding-cave.claude-code.{bundles,hooks,memoryFragments,extraSettings}`.
      homeModules.claudeCode = ./modules/claude-code.nix;
      homeModules.default = self.homeModules.claudeCode;

      packages = eachSystem (pkgs:
        let
          sandbox = import ./lib/sandbox.nix { inherit pkgs nixpkgs; };
          python = pkgs.python3.withPackages (ps: [ ps.click ]);

          # Build-time config consumed by the Python runtime via
          # $CODING_CAVE_BUILD_CONFIG (set by the cav wrapper). Placeholder
          # "out" resolves to the cav derivation's $out at build time.
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
            pkgs.stdenv.mkDerivation {
            pname = "cav";
            version = "0.1.0";

            src = pkgs.lib.cleanSource ./.;

            nativeBuildInputs = [ pkgs.makeWrapper ];
            buildInputs = [ python ];
            propagatedBuildInputs = with pkgs; [
              git socat slirp4netns iptables
              util-linux  # unshare + nsenter (>= 2.39 for --map-user)
            ];

            installPhase = ''
              mkdir -p $out/lib/cav $out/bin $out/share/coding-cave

              # Install Python package as an opaque blob.
              cp -r cav/* $out/lib/cav/

              # Pin the python interpreter in hook shebangs.
              substituteInPlace $out/lib/cav/scripts/hooks/pre-receive \
                --replace-fail "#!/usr/bin/env python3" "#!${python}/bin/python3"

              # Emit build-time config at a public path. constants.py
              # reads it via CODING_CAVE_BUILD_CONFIG at runtime.
              cat > $out/share/coding-cave/build.json <<'JSON'
              ${buildConfig}
              JSON

              # Console script entry point.
              cat > $out/bin/cav <<WRAPPER
              #!${python}/bin/python3
              import sys
              sys.path.insert(0, "$out/lib")
              from cav.cli import main
              main()
              WRAPPER
              chmod +x $out/bin/cav $out/lib/cav/scripts/hooks/pre-receive

              # Wrap with runtime deps on PATH + build-config pointer.
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
          };

        in {
          default = cav;
          inherit sandbox;
        }
      );

      devShells = eachSystem (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.click ps.vulture ]))
            pkgs.git
            pkgs.socat
            pkgs.iptables
            pkgs.util-linux
            pkgs.slirp4netns
          ];
        };
      });

      checks = eachSystem (pkgs: {
        unit = pkgs.runCommand "cav-unit-tests" {
          nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.click ps.vulture ])) pkgs.git ];
        } ''
          cp -r ${pkgs.lib.cleanSource ./.}/* .
          export HOME=$TMPDIR
          git config --global init.defaultBranch main
          git config --global user.email "test@test.com"
          git config --global user.name "Test"
          python3 -m unittest -v tests.test_hook tests.test_cli tests.test_alternates tests.test_nixdb tests.test_lint tests.test_cp_security tests.test_user_env tests.test_supervisor
          touch $out
        '';
      });
    };
}
