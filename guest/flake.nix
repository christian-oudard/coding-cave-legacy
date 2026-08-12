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
      # The guest is built for the host it will run on: x86_64 under Cloud
      # Hypervisor, aarch64 under vfkit on Apple silicon. Both are Linux; the
      # darwin host builds nothing itself and needs an aarch64-linux builder.
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = nixpkgs.lib.genAttrs systems;

      guestFor = system: nixpkgs.lib.nixosSystem {
        inherit system;
        specialArgs = {
          claudeCode = claude-code.packages.${system}.default;
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
