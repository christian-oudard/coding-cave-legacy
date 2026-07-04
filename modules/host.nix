# Host-side NixOS module: installs the cav CLI. There is no host daemon;
# each cave gets its own nix store under the launching user's data dir, so
# the launcher needs no privileged setup.

{ cav, ... }:

{
  config = {
    environment.systemPackages = [ cav ];
  };
}
