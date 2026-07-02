# Single NixOS module owning the coding-cave host-side store manager. It
# maintains a root-owned shared nix store at /var/lib/coding-cave/store and
# periodically converges every cave store onto it: each package ends up as one
# inode, canonically the host store's when the host has the path, else the
# shared store's (the root-owned inode is what stops a cave rewriting shared
# content). Cave-originated paths pass a signature gate before entering the
# shared store and never reach the host store. It garbage-collects the shared
# store while rooting the paths every live cave still references, and applies
# optional per-agent disk quotas. It also creates the sticky per-cave store
# base at /var/lib/coding-cave/nix-stores. Future host-side concerns extend
# `cav-store` rather than fragmenting into separate services.
#
# The unit body is `cav-store <verb>` so the same logic works on
# non-NixOS hosts (Ubuntu via `cav install-host`, etc.). Host configuration
# (the quotas) is rendered to /etc/coding-cave/host.json, which cav-store
# reads.

{ config, lib, pkgs, cav, ... }:

let
  cfg = config.services.codingCave;
  # Only emit keys that are set; an absent key means that quota is inactive.
  hostConfig = lib.filterAttrs (_: v: v != null) {
    perAgentQuota = cfg.perAgentQuota;
    sharedHomeQuota = cfg.sharedHomeQuota;
  };
in
{
  options.services.codingCave = {
    perAgentQuota = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "10G";
      description = ''
        Per-agent disk cap, applied to each agent's nix store and its
        workspace clones (e.g. "10G"). null leaves it inactive. Requires
        project quota enabled on the filesystem that holds the cave state
        (see the README); if set without that support, coding-cave-host
        refuses to start rather than run unprotected.
      '';
    };
    sharedHomeQuota = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "20G";
      description = ''
        Global cap on the shared cave home (one limit for all sibling
        agents). null leaves it inactive. Same filesystem requirement as
        perAgentQuota.
      '';
    };
  };

  config = {
    environment.systemPackages = [ cav ];

    # cav-store reads this at its default config path.
    environment.etc."coding-cave/host.json".text = builtins.toJSON hostConfig;

    systemd.services.coding-cave-host = {
      description = "Coding-cave host-side store manager";
      wantedBy = [ "multi-user.target" ];
      after = [ "local-fs.target" ];
      unitConfig.RequiresMountsFor = "/nix/store";
      # cav-store is unwrapped (only the cav binary gets a wrapped PATH), so
      # its nix/nix-store/chattr/setquota shell-outs resolve from this unit
      # path. chattr (e2fsprogs, `-p +P`) and setquota apply the optional
      # per-agent project quotas; shared paths are not marked immutable.
      path = [ pkgs.nix pkgs.e2fsprogs pkgs.quota ];
      serviceConfig = {
        Type = "simple";
        ExecStartPre = "${cav}/bin/cav-store setup";
        ExecStart = "${cav}/bin/cav-store watch";
        ExecStopPost = "${cav}/bin/cav-store teardown";
        # Fail-fast, no respawn: cav-store exits on the first serious error
        # (failed import, unreadable DB, GC failure). Restarting it would
        # re-run into the same wall every few seconds; a "failed" unit that
        # stays failed is the loud signal to fix the cause and start it again.
        Restart = "no";
      };
    };
  };
}
