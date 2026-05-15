{
  description = "coding-cave: a secure space for agents to write software";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    microvm = {
      url = "github:microvm-nix/microvm.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, microvm }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };

      kernel = import ./lib/kernel.nix { inherit pkgs; };

      nixos = import ./lib/nixos.nix {
        inherit nixpkgs microvm system kernel;
      };

      # microvm.nix's cloud-hypervisor runner hardcodes
      # "earlyprintk=ttyS0 console=ttyS0" in lib/runners/cloud-hypervisor.nix
      # (kernelConsoleDefault), which bypasses loglevel and prints
      # ~10 lines of Linux-version / BIOS-e820 / KASLR before the
      # normal console + loglevel filter come up. We can't drop just
      # earlyprintk via NixOS options without forking microvm, so
      # post-process its generated runner script to strip the
      # substring out of the --cmdline argument.
      patchedRunner = pkgs.runCommand "cav-runner-no-earlyprintk"
        { nativeBuildInputs = [ pkgs.gnused ]; } ''
        mkdir -p $out/bin
        sed 's/earlyprintk=ttyS0 //g' \
          ${nixos.config.microvm.declaredRunner}/bin/microvm-run \
          > $out/bin/microvm-run
        chmod +x $out/bin/microvm-run
      '';

      cav = pkgs.buildGoModule {
        pname = "cav";
        version = "0.0.1";
        src = pkgs.lib.fileset.toSource {
          root = ./.;
          fileset = pkgs.lib.fileset.unions [
            ./go.mod
            ./cmd
          ];
        };
        vendorHash = null;
        env.CGO_ENABLED = "0";
        meta.mainProgram = "cav";
      };

      cavWrapped = pkgs.runCommand "cav-wrapped"
        {
          nativeBuildInputs = [ pkgs.makeWrapper ];
          meta.mainProgram = "cav";
        } ''
        mkdir -p $out/bin
        makeWrapper ${cav}/bin/cav $out/bin/cav \
          --set-default CAV_VIRTIOFSD ${pkgs.virtiofsd}/bin/virtiofsd \
          --set-default CAV_NIXOS_RUNNER ${patchedRunner}/bin/microvm-run \
          --set-default CAV_SCRIPT ${pkgs.util-linux}/bin/script \
          --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.e2fsprogs ]}
      '';
    in {
      packages.${system} = {
        default = cavWrapped;
        cav = cavWrapped;
        cav-unwrapped = cav;
        kernel = kernel;
        nixos-runner = nixos.config.microvm.declaredRunner;
      };

      devShells.${system}.default = pkgs.mkShell {
        packages = [
          pkgs.go
          pkgs.gopls
          pkgs.cloud-hypervisor
          pkgs.virtiofsd
        ];
        shellHook = ''
          export CAV_VIRTIOFSD=${pkgs.virtiofsd}/bin/virtiofsd
          export CAV_NIXOS_RUNNER=${nixos.config.microvm.declaredRunner}/bin/microvm-run
        '';
      };
    };
}
