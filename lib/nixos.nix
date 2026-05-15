# Cave-vm NixOS guest. Boots under cloud-hypervisor via microvm.nix,
# on top of the slim cave-kernel, with the host's /nix/store and
# /home/cave-coder bind-mounted into the guest over virtio-fs.

{ nixpkgs, microvm, system ? "x86_64-linux", kernel }:

nixpkgs.lib.nixosSystem {
  inherit system;

  modules = [
    microvm.nixosModules.microvm

    ({ pkgs, lib, ... }: {
      networking.hostName = "cave-vm";
      system.stateVersion = nixpkgs.lib.trivial.release;

      # Boot on the slim cave-kernel rather than nixpkgs's stock kernel.
      boot.kernelPackages = pkgs.linuxPackagesFor kernel;

      # The cave-kernel has every feature it needs built in (=y), so
      # there are no .ko files to ship. Tell NixOS's modulesClosure step
      # not to error on the empty modules tree. Modprobe at runtime may
      # log "module not found" — harmless, the kernel has the feature.
      boot.initrd.allowMissingModules = true;
      boot.kernelModules = [];
      boot.initrd.kernelModules = [];
      boot.initrd.availableKernelModules = [];

      # Quiet the boot. The early-kernel chatter (Linux version,
      # BIOS-e820, KASLR) is hardcoded in microvm.nix's
      # cloud-hypervisor runner template via `earlyprintk=ttyS0`; we
      # can't drop just earlyprintk via the kernelParams option
      # without forking microvm. Live with those lines for now; what
      # we *can* silence here is loglevel + systemd unit chatter +
      # NixOS getty welcome banner.
      boot.consoleLogLevel = 3;
      boot.kernelParams = [ "quiet" "systemd.show_status=no" ];
      services.getty.greetingLine = lib.mkForce "";
      services.getty.helpLine = lib.mkForce "";

      # Terminal capability defaults. The serial console doesn't carry
      # TERM/COLORTERM across the VM boundary, so the user's prompt
      # theme falls back to "limited terminal" mode (renders the
      # horizontal rule as bold ANSI instead of a 24-bit color) unless
      # we set sensible values inside the guest. Override the serial-
      # getty's hardcoded TERM=linux directly.
      systemd.services."serial-getty@ttyS0".environment.TERM = lib.mkForce "xterm-256color";
      environment.sessionVariables.COLORTERM = "truecolor";

      # uid 1000 matches the host cave-coder, so virtio-fs files
      # appear with their host-side ownership intact (no remapping).
      users.users.cave-coder = {
        isNormalUser = true;
        uid = 1000;
        # No password: serial-only access, gated by the VM boundary.
        hashedPassword = "";
        extraGroups = [ "wheel" ];
        shell = pkgs.zsh;
      };
      users.users.root.hashedPassword = "";
      programs.zsh.enable = true;
      security.sudo.wheelNeedsPassword = false;

      # No agetty-driven login on the serial console — its autologin
      # mode hardcodes a `USER (automatic login)\n` print surrounded
      # by several blank lines from /etc/issue and the (now empty)
      # motd path. We replace serial-getty@ttyS0 below with a unit
      # that runs `runuser -l cave-coder` directly on /dev/ttyS0, so
      # the user lands on their zsh prompt with zero banner.
      users.motd = lib.mkForce "";

      boot.kernel.sysctl."kernel.sysrq" = 1;
      systemd.services."serial-getty@ttyS0".enable = false;
      systemd.services.cave-shell = {
        description = "cave-vm interactive shell on ttyS0";
        wantedBy = [ "multi-user.target" ];
        after = [ "systemd-user-sessions.service" "multi-user.target" ];
        serviceConfig = {
          Type = "idle";
          ExecStart = "${pkgs.util-linux}/bin/runuser -l cave-coder";
          StandardInput = "tty-force";
          StandardOutput = "tty";
          StandardError = "tty";
          TTYPath = "/dev/ttyS0";
          TTYReset = "yes";
          TTYVHangup = "yes";
          KillMode = "process";
          IgnoreSIGPIPE = "no";
          Restart = "no";
          UtmpIdentifier = "ttyS0";
          UtmpMode = "user";
          Environment = "TERM=xterm-256color";
          # `exit` from the user's shell terminates this unit; ExecStopPost
          # does a kernel-level poweroff (sysrq 'o') so we bypass the
          # systemd shutdown teardown — nothing in the cave is
          # persistence-critical (host /nix/store RO, overlay tmpfs).
          # Drop the console loglevel to 0 first so the kernel's own
          # `pr_emerg "Power down"` / `pr_info "Power Off"` lines don't
          # leak onto the user's terminal during shutdown.
          ExecStopPost = pkgs.writeShellScript "cave-vm-poweroff" ''
            ${pkgs.coreutils}/bin/echo 0 > /proc/sys/kernel/printk
            ${pkgs.coreutils}/bin/echo o > /proc/sysrq-trigger
          '';
        };
      };

      microvm = {
        hypervisor = "cloud-hypervisor";
        mem = 1024;
        vcpu = 1;

        # Enables systemd-notify-over-vsock from the guest, and is the
        # transport the eventual `cav shell` attach-to-running-VM flow
        # will use. CID 3 is the conventional first-guest value (0/1/2
        # are reserved for hypervisor/local/host).
        vsock.cid = 3;

        shares = [
          {
            tag = "ro-store";
            proto = "virtiofs";
            source = "/nix/store";
            mountPoint = "/nix/.ro-store";
          }
          {
            tag = "cave-home";
            proto = "virtiofs";
            source = "/home/cave-coder";
            mountPoint = "/home/cave-coder";
          }
        ];
        writableStoreOverlay = "/nix/.rw-store";
        volumes = [
          {
            image = "nix-store-overlay.img";
            mountPoint = "/nix/.rw-store";
            size = 2048;
          }
        ];
        interfaces = [];
      };
    })
  ];
}
