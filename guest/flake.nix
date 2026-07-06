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

      # Convenience: the QEMU runner script (boots under TCG when KVM is
      # absent, e.g. inside a cave without /dev/kvm passthrough). This is a
      # bootstrap-era test vehicle; production boots via Cloud Hypervisor.
      packages.${system}.vm =
        self.nixosConfigurations.cave-guest-dev.config.system.build.vm;
    };
}
