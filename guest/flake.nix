{
  description = "cave-guest-dev: minimal NixOS guest for the bootstrap vm-root-agent";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    claude-code = {
      url = "github:sadjow/claude-code-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, claude-code }:
    let
      system = "x86_64-linux";
    in
    {
      nixosConfigurations.cave-guest-dev = nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = {
          claudeCode = claude-code.packages.${system}.default;
        };
        modules = [ ./configuration.nix ];
      };

      # The three artifacts Cloud Hypervisor boots with: --kernel, --initramfs,
      # and the root --disk. Direct kernel boot, so the kernel and initrd come
      # from the host at every start and only the disk persists.
      packages.${system} =
        let cfg = self.nixosConfigurations.cave-guest-dev.config;
        in {
          kernel = cfg.system.build.kernel;
          initrd = cfg.system.build.initialRamdisk;
          disk = cfg.system.build.chvDisk;
          toplevel = cfg.system.build.toplevel;
        };
    };
}
