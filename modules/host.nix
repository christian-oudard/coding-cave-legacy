# Single NixOS module owning every coding-cave host-side concern that
# requires CAP_SYS_ADMIN. Today that's an idmapped read-only mount of
# /nix/store at /var/lib/coding-cave/nix-store (relabeling host-uid-0 to
# the cave-coder uid so the cave's overlayfs copy-ups land with ownership
# nix can chmod) and a watcher that auto-promotes packages from agent
# overlays into the host /nix/store. Future host-side concerns extend
# `cav-store` rather than fragmenting into separate services.
#
# The unit body is `cav-store <verb>` so the same logic works on
# non-NixOS hosts (Ubuntu via `cav install-host`, etc.).

{ pkgs, cav, ... }:

{
  environment.systemPackages = [ cav ];

  systemd.services.coding-cave-host = {
    description = "Coding-cave host-side mount + auto-promote watcher";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];
    unitConfig.RequiresMountsFor = "/nix/store";
    # cav-store is unwrapped (only the cav binary gets a wrapped PATH), so its
    # mount/umount/nix-store shell-outs resolve from this unit path.
    path = [ pkgs.nix pkgs.util-linux ];
    serviceConfig = {
      Type = "simple";
      ExecStartPre = "${cav}/bin/cav-store setup";
      ExecStart = "${cav}/bin/cav-store watch";
      ExecStopPost = "${cav}/bin/cav-store teardown";
      Restart = "on-failure";
      RestartSec = "5s";
    };
  };
}
