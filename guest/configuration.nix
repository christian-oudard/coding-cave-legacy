{ config, pkgs, lib, modulesPath, claudeCode, ... }:

{
  imports = [
    # Gives config.system.build.vm (QEMU runner) and provides a root
    # filesystem + bootloader stub, so the toplevel evaluates without a
    # hand-written disk layout during the bootstrap era.
    "${modulesPath}/virtualisation/qemu-vm.nix"
  ];

  system.stateVersion = "24.11";

  # --- VM shape (bootstrap era: QEMU runner) ---
  # Headless server: serial console, ssh forwarded to the host so the user
  # attaches and drives the agent's tmux. Sized to run nix builds (and
  # claude) inside. Accelerator choice (KVM vs TCG) is left to the runner /
  # bootstrap script, so this boots even where /dev/kvm is absent.
  virtualisation = {
    graphics = false;
    memorySize = 4096;
    cores = 4;
    # Max disk size (MiB). Sparse, so it only consumes what the guest writes;
    # a large cap is "elastic". cave-vm passes the configured size via
    # CODING_CAVE_DISK_MB (an impure build); pure builds (smoke test) get the
    # 100 GiB default.
    diskSize =
      let e = builtins.getEnv "CODING_CAVE_DISK_MB";
      in if e != "" then lib.toInt e else 102400;
    # No forwardPorts here: which host port maps to guest ssh is a host-side
    # concern (and hardcoding one bind-conflicts across concurrent boots), so
    # the host scripts inject `hostfwd=...` via QEMU_NET_OPTS instead. The
    # smoke test needs no forward and stays conflict-free.
  };
  boot.kernelParams = [ "console=ttyS0" ];
  # qemu-vm.nix pins boot.loader.grub.device at the virtio root disk even in
  # direct-kernel boot (useBootLoader = false), and grub.enable defaults to
  # true, so an in-guest `nixos-rebuild switch` runs grub-install against the
  # partition-less root image and aborts before activating. Boot state is
  # host-injected (-kernel/-initrd/-append); the disk is a cache with no
  # bootloader on it. Disabling GRUB lets `switch` register and activate
  # generations (the root agent's redeploy path, docs/vm-root-agent.md) and skip
  # the bootloader step entirely.
  boot.loader.grub.enable = false;

  # --- Networking: bootstrap-era user-mode NIC, all egress to crinj ---
  # The guest DHCPs on its single NIC (QEMU user-net / passt). crinj on the
  # host is the only sanctioned egress; point every proxy-aware tool at it.
  # The gateway 10.0.2.2 is the host as seen from QEMU user networking.
  networking.hostName = "coding-cave";
  networking.useDHCP = true;
  networking.firewall.enable = false;

  # crinj listens on the host; QEMU user-net maps the host to 10.0.2.2.
  # Port 10255 matches the Rust implementation's CRINJ_PORT and vmcav's
  # default.
  environment.sessionVariables =
    let proxy = "http://10.0.2.2:10255";
    in {
      HTTP_PROXY = proxy;
      HTTPS_PROXY = proxy;
      http_proxy = proxy;
      https_proxy = proxy;
      NO_PROXY = "localhost,127.0.0.1,::1,10.0.2.2";
      no_proxy = "localhost,127.0.0.1,::1,10.0.2.2";
      # NixOS bakes the system CA path into curl/go/nix, but node and
      # claude's runtime do not read it on their own; point them at the
      # system bundle, which includes the crinj CA (security.pki below).
      SSL_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt";
      NODE_EXTRA_CA_CERTS = "/etc/ssl/certs/ca-certificates.crt";
    };

  # --- TLS trust: crinj's intercepting CA, baked into the system store ---
  # crinj MITMs TLS, so the whole guest must trust its CA. Baking it into
  # security.pki lands it in /etc/ssl/certs/ca-certificates.crt, the one
  # path OpenSSL-based tools, go, and nix already consult, so trust is
  # system-wide instead of per-service env plumbing. The CA is
  # host-specific and must not be committed, so it reaches the eval
  # impurely (every caller already builds with --impure):
  #   - host build (vmcav): $CODING_CAVE_CRINJ_CA carries the PEM
  #   - in-guest redeploy: read from the mounted seed share
  # No silent fallback: a build without the CA yields a guest whose agent
  # cannot reach the API, so fail the eval loudly instead.
  security.pki.certificates =
    let
      env = builtins.getEnv "CODING_CAVE_CRINJ_CA";
      share = "/etc/cave-host/ca.pem";
    in [
      (if env != "" then env
       else if builtins.pathExists share then builtins.readFile share
       else throw ("crinj CA not found: export CODING_CAVE_CRINJ_CA and "
         + "build with --impure (host), or build --impure in the guest "
         + "where ${share} is mounted"))
    ];

  # Host-provided runtime seed share. Carries the files that are
  # host-specific and must not be committed in this flake: crinj's
  # intercepting CA (ca.pem, also read at eval time by in-guest redeploys,
  # see security.pki below), the host's claude login state (claude.json),
  # and the host's ssh public key. Delivered read-only over 9p; vmcav
  # stages the files and adds the matching -virtfs, keeping host paths out
  # of this committed flake. `nofail` + a short timeout keep a boot without
  # the share from hanging.
  # qemu-vm.nix rebuilds the VM's top-level fileSystems from
  # virtualisation.fileSystems (it replaces fileSystems with
  # mkVMOverride virtualisation.fileSystems), so a VM mount must be declared
  # here, not at top level, or it is silently dropped.
  virtualisation.fileSystems."/etc/cave-host" = {
    device = "cavehost";
    fsType = "9p";
    options = [ "trans=virtio" "ro" "nofail" "x-systemd.device-timeout=2" ];
  };

  # --- Access: host attaches over ssh, drives the agent's tmux ---
  services.openssh = {
    enable = true;
    settings = {
      PermitRootLogin = "prohibit-password";
      PasswordAuthentication = false;
    };
  };
  services.getty.autologinUser = "root";

  # logrotate's config check rejects the store-owned config (through the
  # 9p-mounted /nix/store the file's owner isn't uid 0), which fails on boot
  # and, worse, makes switch-to-configuration return non-zero. The agent's
  # redeploy loop reads that as a failed deploy and would roll back a good
  # one. logrotate isn't needed here (journald self-rotates), so disable it.
  services.logrotate.enable = false;

  # Install the host's ssh public key for root login. The key is host-specific
  # (generated by the host script), delivered over the host-seed 9p share.
  # Copied into /root/.ssh as root:root 600 rather than pointing sshd at the
  # 9p file, whose mount ownership/perms would trip sshd StrictModes.
  systemd.services.install-ssh-key = {
    description = "Install host ssh key for root login";
    wantedBy = [ "multi-user.target" ];
    before = [ "sshd.service" ];
    unitConfig.ConditionPathExists = "/etc/cave-host/authorized_keys";
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.writeShellScript "install-ssh-key" ''
        set -eu
        install -d -m 700 /root/.ssh
        install -m 600 /etc/cave-host/authorized_keys /root/.ssh/authorized_keys
      ''}";
    };
  };

  # --- Nix: flakes + daemon, so the agent redeploys from inside ---
  nix.settings.experimental-features = [ "nix-command" "flakes" ];
  nix.settings.trusted-users = [ "root" ];
  nixpkgs.config.allowUnfree = true;

  # --- Guest toolchain: the minimal floor, nothing more ---
  environment.systemPackages = with pkgs; [
    git
    tmux
    claudeCode
    vim
  ];

  # --- Claude config seeding ---
  # claude prompts interactively without accepted trust/onboarding flags for
  # its project dir, and without login state. Seed /root/.claude.json before
  # the agent starts: preserve any existing guest state, else start from the
  # host's staged claude.json (login state; crinj injects the real token at
  # the network layer), then force the trust/onboarding flags for the repo.
  systemd.services.seed-claude-config = {
    description = "Seed /root/.claude.json (trust + onboarding + login state)";
    wantedBy = [ "multi-user.target" ];
    before = [ "vm-root-agent.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.writeShellScript "seed-claude-config" ''
        set -eu
        seed=/root/.claude.json
        host=/etc/cave-host/claude.json
        if [ -f "$seed" ]; then base=$(cat "$seed")
        elif [ -f "$host" ]; then base=$(cat "$host")
        else base='{}'; fi
        printf '%s' "$base" | ${pkgs.jq}/bin/jq \
          '.projects["/root/coding-cave"].hasTrustDialogAccepted = true
           | .projects["/root/coding-cave"].hasCompletedProjectOnboarding = true' \
          > "$seed.tmp"
        mv "$seed.tmp" "$seed"
        chmod 600 "$seed"
      ''}";
    };
  };

  # --- Repo clone ---
  # Clone the coding-cave repo the agent works in. Its own oneshot (not
  # buried in the agent launcher) so success/failure is visible in the
  # journal and the agent hard-depends on it: no repo, no agent (fail
  # loudly). CAVE_REPO_URL defaults to the host git daemon over user-net.
  systemd.services.clone-repo = {
    description = "Clone the coding-cave repo for the agent";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    before = [ "vm-root-agent.service" ];
    wantedBy = [ "multi-user.target" ];
    path = [ pkgs.git ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.writeShellScript "clone-repo" ''
        set -eu
        repo=/root/coding-cave
        url="''${CAVE_REPO_URL:-git://10.0.2.2/coding-cave}"
        if [ -d "$repo/.git" ]; then
          echo "clone-repo: repo already present at $repo"
        else
          echo "clone-repo: cloning $url"
          git clone "$url" "$repo"
          echo "clone-repo: cloned OK"
        fi
      ''}";
    };
  };

  # --- The agent runtime ---
  # A tmux session running claude as root with bypass permissions. Defined
  # here so the agent redeploys its own unit via nixos-rebuild.
  # RestartIfChanged=false so a switch doesn't kill a live session.
  systemd.services.vm-root-agent = {
    description = "Bootstrap vm-root-agent (claude as root, in tmux)";
    after = [ "network-online.target" "seed-claude-config.service" "clone-repo.service" ];
    wants = [ "network-online.target" ];
    requires = [ "seed-claude-config.service" "clone-repo.service" ];
    wantedBy = [ "multi-user.target" ];
    restartIfChanged = false;
    path = [ pkgs.git pkgs.tmux claudeCode pkgs.nix pkgs.bash ];
    # systemd services do not read environment.sessionVariables, so the
    # agent's egress + TLS trust are set here explicitly. The crinj CA is
    # already in the system bundle (security.pki above); the two cert vars
    # are still needed because claude's runtime does not consult the system
    # bundle path on its own (verified: /login gets a cert-chain error
    # without SSL_CERT_FILE, and node subprocesses need NODE_EXTRA_CA_CERTS).
    environment = {
      HTTP_PROXY = "http://10.0.2.2:10255";
      HTTPS_PROXY = "http://10.0.2.2:10255";
      NO_PROXY = "localhost,127.0.0.1,::1,10.0.2.2";
      SSL_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt";
      NODE_EXTRA_CA_CERTS = "/etc/ssl/certs/ca-certificates.crt";
      # claude refuses --dangerously-skip-permissions as root ("cannot be
      # used with root/sudo privileges") unless it sees an already-sandboxed
      # environment. The VM is that sandbox, so declare it; without this the
      # agent starts and immediately exits. `vmcav agent` sets the same when
      # it launches claude interactively; a plain `vmcav shell` does not.
      IS_SANDBOX = "1";
    };
    serviceConfig = {
      Type = "forking";
      ExecStart = "${pkgs.writeShellScript "vm-root-agent-start" ''
        set -eu
        exec ${pkgs.tmux}/bin/tmux new-session -d -s agent \
          "cd /root/coding-cave && exec claude --dangerously-skip-permissions"
      ''}";
      Restart = "no";
    };
  };
}
