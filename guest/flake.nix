{
  description = "cave-guest-dev: minimal NixOS guest for the bootstrap vm-root-agent";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    claude-code = {
      url = "github:sadjow/claude-code-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # Not used by the guest system itself. It is here so that the user
    # environment an agent gets is built against the same nixpkgs the guest is
    # running: cave-env's generated wrapper flake pins these three by store
    # path out of this closure (see caveEnvInputs below), which is what keeps a
    # second glibc, and a second claude, out of the VM.
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, claude-code, home-manager }:
    let
      # The guest is built for the host it will run on: x86_64 under Cloud
      # Hypervisor, aarch64 under vfkit on Apple silicon. Both are Linux; the
      # darwin host builds nothing itself and needs an aarch64-linux builder.
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = nixpkgs.lib.genAttrs systems;

      guestFor = system: nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = {
          claudeCode = claude-code.packages.${system}.default;
          # The three flake sources the user environment is built from, as
          # store paths. Passed in rather than re-fetched, so `cave-env build`
          # needs no network at all: everything it locks is already in the
          # guest store because the system that installed it depends on it.
          #
          # claude-code is the flake here, not the package above: mkCaveEnv
          # asks it for packages.<system>.default itself, and pinning the flake
          # rather than the derivation is what lets a cave.nix override the
          # version without the wrapper flake having to know how.
          caveEnvInputs = {
            nixpkgs = nixpkgs.outPath;
            home-manager = home-manager.outPath;
            claude-code = claude-code.outPath;
          };
        };
        modules = [ ./configuration.nix ];
      };
    in
    {
      nixosConfigurations = forAll guestFor // {
        # Kept so existing callers that name the configuration directly still
        # resolve; it is the x86_64 guest, as it always was.
        cave-guest-dev = guestFor "x86_64-linux";
      };

      # The three artifacts the VMM boots with: --kernel, --initramfs, and the
      # root --disk. Direct kernel boot, so the kernel and initrd come from the
      # host at every start and only the disk persists.
      packages = forAll (system:
        let cfg = (guestFor system).config;
        in {
          kernel = cfg.system.build.kernel;
          initrd = cfg.system.build.initialRamdisk;
          disk = cfg.system.build.chvDisk;
          toplevel = cfg.system.build.toplevel;
        });
    };
}
