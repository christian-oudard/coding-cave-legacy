# Single NixOS module owning every coding-cave host-side concern that
# requires CAP_SYS_ADMIN. Today that's just an idmapped read-only mount of
# /nix/store at /var/lib/coding-cave/nix-store, relabeling host-uid-0 to
# the cave-coder uid so the cave's overlayfs copy-ups land with ownership
# nix can chmod. Future host-side concerns extend `cav host setup` rather
# than fragmenting into separate services.
#
# The unit's body is `cav host setup` / `cav host teardown` so the same
# logic works on non-NixOS hosts (Ubuntu via `cav install-host`, etc.).

{ pkgs, cav, ... }:

{
  environment.systemPackages = [ cav ];

  systemd.services.coding-cave-host = {
    description = "Coding-cave host-side setup";
    wantedBy = [ "multi-user.target" ];
    after = [ "local-fs.target" ];
    unitConfig.RequiresMountsFor = "/nix/store";
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${cav}/bin/cav host setup";
      ExecStop = "${cav}/bin/cav host teardown";
    };
  };
}
