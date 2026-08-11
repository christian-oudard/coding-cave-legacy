{ config, pkgs, lib, modulesPath, claudeCode, ... }:

let
  # The socket cave-guestd serves on. /run, so a reboot cannot leave a stale
  # one behind.
  agentSocket = "/run/cave-agentd.sock";

  # cave-guestd: the daemon that owns each agent's pty, and the client that
  # attaches to one. It replaced dtach, which held a pty but buffered nothing
  # and never resized a session to its attaching client, so every reattach
  # came back to a blank screen at the wrong width.
  #
  # The Go module lives outside this flake's directory, so its source arrives
  # impurely, the same way the crinj CA does: from the host build via
  # $CAVE_VM_SRC, and from an in-guest redeploy out of the agent's own
  # checkout. No silent fallback, a guest without the daemon has no agent.
  caveVmSrc =
    let
      env = builtins.getEnv "CAVE_VM_SRC";
      inGuest = "/root/coding-cave/cave-vm";
    in
    if env != "" then env
    else if builtins.pathExists inGuest then inGuest
    else throw ("cave-vm source not found: export CAVE_VM_SRC and build with "
      + "--impure (host), or build --impure in the guest where ${inGuest} exists");

  caveGuestd = pkgs.buildGoModule {
    pname = "cave-guestd";
    version = "0";
    src = builtins.path { path = /. + caveVmSrc; name = "cave-vm-src"; };
    vendorHash = null; # the module has no dependencies, by design
    subPackages = [ "cmd/cave-guestd" ];
  };

  # One definition of "run the agent", used by the boot unit and by the
  # daemon when it has to restart a dead session, so the two cannot drift
  # into launching claude differently.
  caveAgent = pkgs.writeShellScriptBin "cave-agent" ''
    cd /root/coding-cave
    exec ${claudeCode}/bin/claude --dangerously-skip-permissions "$@"
  '';

  # git's gpg.program inside the guest. Signing is forwarded to the host
  # broker over the service plane, because the secret key deliberately cannot
  # reach the guest; everything else (notably verification) runs the real gpg
  # locally against the seeded public keyring.
  caveGpg = pkgs.runCommand "cave-gpg" { } ''
    mkdir -p $out/bin
    substitute ${./cave-gpg.py} $out/bin/cave-gpg \
      --replace '#!/usr/bin/env python3' '#!${pkgs.python3}/bin/python3'
    chmod +x $out/bin/cave-gpg
  '';
in
{
  imports = [
    # virtio driver set for the initrd. Written for QEMU guests but it is the
    # same virtio-pci device family Cloud Hypervisor exposes.
    "${modulesPath}/profiles/qemu-guest.nix"
  ];

  system.stateVersion = "24.11";

  # --- VM shape (Cloud Hypervisor, direct kernel boot) ---
  # Headless: the boot log goes to the 8250 serial port, which CHV exposes
  # with `--serial file=<log>`. Deliberately NOT the virtio console (hvc0):
  # virtio_console is a module, so hvc0 is silent until the initrd loads it
  # and early boot failures would leave no trace. Memory and cpu count are
  # host-side CHV flags, not guest config.
  boot.kernelParams = [ "console=ttyS0" ];

  # Root is the whole device, no partition table (make-disk-image
  # partitionTableType = "none"), so the kernel gets root=/dev/vda directly.
  fileSystems."/" = {
    device = "/dev/vda";
    fsType = "ext4";
  };

  # Boot state is host-injected (--kernel/--initramfs/--cmdline) and the disk
  # carries no bootloader, so grub-install would abort an in-guest
  # `nixos-rebuild switch` before it activated anything. Disabling GRUB lets
  # switch register and activate generations, which is the root agent's
  # redeploy path (docs/vm-root-agent.md). The consequence to remember: a
  # generation that changes the kernel needs the host to boot the new one,
  # which is why bootloader boot is the graduation step in that doc.
  boot.loader.grub.enable = false;

  # The raw root image CHV boots. Built once on the host; vmcav copies it into
  # the state dir on first start, after which the guest owns it and
  # nixos-rebuild mutates it in place.
  system.build.chvDisk = import "${modulesPath}/../lib/make-disk-image.nix" {
    inherit pkgs lib config;
    partitionTableType = "none";
    installBootLoader = false;
    format = "raw";
    diskSize = "auto";
    additionalSpace = "3G";
    copyChannel = false;
  };

  # --- Networking: none. Every host service arrives over vsock ---
  # The guest has no NIC at all, so unproxied egress is impossible by
  # construction rather than by firewall rule (vm-cave-design.md). Host
  # services reach the guest over virtio-vsock and are re-exposed on
  # localhost by the forwarders below, so nothing in this config, in the
  # agent's git remotes, or in its proxy env has to know vsock exists.
  networking.hostName = "coding-cave";
  networking.useDHCP = false;
  networking.firewall.enable = false;

  environment.sessionVariables =
    let proxy = "http://127.0.0.1:10255";
    in {
      HTTP_PROXY = proxy;
      HTTPS_PROXY = proxy;
      http_proxy = proxy;
      https_proxy = proxy;
      NO_PROXY = "localhost,127.0.0.1,::1";
      no_proxy = "localhost,127.0.0.1,::1";
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

  # Host-provided runtime seed. Carries the files that are host-specific and
  # must not be committed in this flake: crinj's intercepting CA (ca.pem, also
  # read at eval time by in-guest redeploys, see security.pki above), the
  # host's claude login state (claude.json), this VM's own captured login
  # placeholder (credentials.json), its git identity, and its ssh public key.
  # `nofail` plus a short timeout keep a seedless boot (the smoke test) from
  # hanging.
  #
  # Delivered as a small read-only ext4 disk rather than a filesystem share:
  # CHV has no 9p, and its virtio-fs would drag in a virtiofsd the launcher
  # has to supervise (vm-cave-plan.md notes the stock one is unusable
  # unprivileged). A handful of small static files do not justify that, and
  # vmcav can build the image rootlessly with `mke2fs -d`.
  fileSystems."/etc/cave-host" = {
    device = "/dev/vdb";
    fsType = "ext4";
    options = [ "ro" "nofail" "x-systemd.device-timeout=2" ];
  };

  # --- Access: host comes in over ssh, then attaches to the agent daemon ---
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

  # Guest-side leg of the service plane. CHV's vsock is the unix-socket
  # hybrid: a guest connection to port N surfaces on the host as the unix
  # socket <vsock.sock>_N, which vmcav bridges to the real service. CID 2 is
  # the host (VMADDR_CID_HOST). Re-exposing each service on localhost is what
  # lets the rest of this config, the proxy env, and the agent's git remotes
  # stay ignorant of vsock. Restart=always because the host bridge may not be
  # listening yet when the guest boots.
  systemd.services.vsock-crinj = {
    description = "Bridge 127.0.0.1:10255 to host crinj over vsock 7200";
    wantedBy = [ "multi-user.target" ];
    before = [ "vm-root-agent.service" ];
    serviceConfig = {
      ExecStart = "${pkgs.socat}/bin/socat "
        + "TCP-LISTEN:10255,bind=127.0.0.1,fork,reuseaddr VSOCK-CONNECT:2:7200";
      Restart = "always";
      RestartSec = 1;
    };
  };

  # Signing. The gpg-agent socket is deliberately NOT forwarded: a unix socket
  # cannot cross the VM boundary, and re-exposing the agent on a port would
  # hand signing to every process that can reach it, which is strictly weaker
  # than the 0600 socket a container cave gets. Instead the host runs a broker
  # that accepts a payload, validates the arguments, pins the key, and signs.
  systemd.services.vsock-gpg = {
    description = "Bridge 127.0.0.1:7400 to the host gpg signing broker over vsock 7400";
    wantedBy = [ "multi-user.target" ];
    before = [ "vm-root-agent.service" ];
    serviceConfig = {
      ExecStart = "${pkgs.socat}/bin/socat "
        + "TCP-LISTEN:7400,bind=127.0.0.1,fork,reuseaddr VSOCK-CONNECT:2:7400";
      Restart = "always";
      RestartSec = 1;
    };
  };

  # Public keyring, so `git log --show-signature` and any verification the
  # agent does resolve locally instead of round-tripping to the host. Public
  # material only; nothing here can sign.
  systemd.services.install-pubring = {
    description = "Install the host public keyring for verification";
    wantedBy = [ "multi-user.target" ];
    before = [ "vm-root-agent.service" ];
    unitConfig.ConditionPathExists = "/etc/cave-host/pubring.kbx";
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.writeShellScript "install-pubring" ''
        set -eu
        install -d -m 700 /root/.gnupg
        install -m 600 /etc/cave-host/pubring.kbx /root/.gnupg/pubring.kbx
      ''}";
    };
  };

  systemd.services.vsock-git = {
    description = "Bridge 127.0.0.1:9418 to the host git daemon over vsock 7300";
    wantedBy = [ "multi-user.target" ];
    before = [ "clone-repo.service" ];
    serviceConfig = {
      ExecStart = "${pkgs.socat}/bin/socat "
        + "TCP-LISTEN:9418,bind=127.0.0.1,fork,reuseaddr VSOCK-CONNECT:2:7300";
      Restart = "always";
      RestartSec = 1;
    };
  };

  # Install the host's ssh public key for root login. The key is host-specific
  # (generated by the host script), delivered on the seed disk. Copied into
  # /root/.ssh as root:root 600 rather than pointing sshd at the seed file,
  # whose mount ownership and perms would trip sshd StrictModes.
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

  # --- Claude policy settings ---
  # The bypass-permissions warning is a different dialog from the trust one
  # seeded below, and accepting it never touches .claude.json: claude records
  # it as `skipDangerousModePermissionPrompt` in settings, which is why it came
  # back at every reformat while the trust dialog stayed accepted.
  #
  # Stated here as policy rather than written into /root/.claude/settings.json,
  # because that is what it is: the agent runs with
  # --dangerously-skip-permissions by construction, and a VM with no NIC is the
  # sandbox the dialog is asking about. Policy is also the tier the agent
  # cannot clobber by rewriting its own settings, and being config rather than
  # disk state it survives a reformat for free. The container cave writes the
  # same file for its own reason (the cave-messages channel allowlist).
  environment.etc."claude-code/managed-settings.json".text = builtins.toJSON {
    skipDangerousModePermissionPrompt = true;
  };

  # System-wide git config. autoSetupRemote so the agent's first `git push` on
  # a branch it just forked creates and tracks the remote branch instead of
  # failing with "no upstream"; the agent branch is created locally by
  # clone-repo below and may not exist on the host yet.
  environment.etc."gitconfig".text = ''
    [push]
    	default = current
    	autoSetupRemote = true
    [gpg]
    	program = ${caveGpg}/bin/cave-gpg
  '';

  # --- Git identity ---
  # The agent's core loop ends in "commit and push", and git refuses to commit
  # without an identity. vmcav stages the host's user.name/user.email into the
  # seed share (host-specific, so it cannot be committed here). The fallback
  # keeps a seedless boot (the smoke test) able to commit rather than failing
  # at the last step of the loop; it is deliberately obviously-not-a-person.
  systemd.services.install-gitconfig = {
    description = "Install git identity for root";
    wantedBy = [ "multi-user.target" ];
    before = [ "clone-repo.service" "vm-root-agent.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.writeShellScript "install-gitconfig" ''
        set -eu
        seed=/etc/cave-host/gitconfig
        if [ -f "$seed" ]; then
          install -m 644 "$seed" /root/.gitconfig
        elif [ ! -f /root/.gitconfig ]; then
          printf '[user]\n\tname = vm-root-agent\n\temail = vm-root-agent@coding-cave.invalid\n' \
            > /root/.gitconfig
        fi
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
    caveGuestd
    caveAgent
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
        # The base is this VM's own previous state, captured before the last
        # reformat, never the host's ~/.claude.json: those flags are keyed to
        # the host's project paths, so they never applied to /root/coding-cave
        # anyway, and the rest of that file is the host's history and MCP
        # config, which has no business in here. An empty base is fine, the
        # keys forced below are the ones that matter.
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

        # claude.json is login *state*; the token lives beside it, and without
        # it the agent starts logged out and stops at /login. What the host
        # stages is this VM's own placeholder, captured after it logged in
        # once: crinj holds the real token, and a placeholder is worthless
        # anywhere else. Never overwritten, so an in-guest login wins.
        cred=/root/.claude/.credentials.json
        host_cred=/etc/cave-host/credentials.json
        if [ ! -f "$cred" ] && [ -f "$host_cred" ]; then
          mkdir -p /root/.claude
          install -m600 "$host_cred" "$cred"
        fi
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
    # The git bridge, not network-online.target: there is no NIC, so
    # networkd-wait-online would only ever time out.
    after = [ "vsock-git.service" ];
    wants = [ "vsock-git.service" ];
    before = [ "vm-root-agent.service" ];
    wantedBy = [ "multi-user.target" ];
    path = [ pkgs.git ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.writeShellScript "clone-repo" ''
        set -eu
        repo=/root/coding-cave
        url="''${CAVE_REPO_URL:-git://127.0.0.1/coding-cave}"
        agent="''${CAVE_AGENT_ID:-1}"
        if [ -d "$repo/.git" ]; then
          echo "clone-repo: repo already present at $repo"
          exit 0
        fi
        echo "clone-repo: cloning $url"
        git clone "$url" "$repo"
        cd "$repo"
        # Land on the agent branch, the only namespace pre-receive accepts
        # (refs/heads/agent-N/<base>). vmcav also builds the guest from this
        # ref, so for the agent, pushing is deploying. Track the remote branch
        # if it already exists, else fork it from the branch just cloned.
        base=$(git symbolic-ref --short HEAD)
        branch="agent-$agent/$base"
        if git rev-parse --verify --quiet "refs/remotes/origin/$branch" >/dev/null; then
          git checkout -B "$branch" --track "origin/$branch"
        else
          git checkout -B "$branch"
        fi
        echo "clone-repo: cloned OK, on $branch (base $base)"
      ''}";
    };
  };

  # --- The agent runtime ---
  # claude as root with bypass permissions, its pty held by cave-guestd so it
  # survives the ssh connection going away. Deliberately not a multiplexer:
  # nothing here wants windows, panes, a status bar or a prefix key, and a
  # multiplexer's scrollback is worse than the terminal's own. Defined here so
  # the agent redeploys its own unit via nixos-rebuild.
  # RestartIfChanged=false so a switch doesn't kill a live session.
  systemd.services.vm-root-agent = {
    description = "Bootstrap vm-root-agent (claude as root, under cave-guestd)";
    after = [ "vsock-crinj.service" "seed-claude-config.service" "clone-repo.service" ];
    wants = [ "vsock-crinj.service" ];
    requires = [ "seed-claude-config.service" "clone-repo.service" ];
    wantedBy = [ "multi-user.target" ];
    restartIfChanged = false;
    path = [ pkgs.git claudeCode pkgs.nix pkgs.bash ];
    # systemd services do not read environment.sessionVariables, so the
    # agent's egress + TLS trust are set here explicitly. The crinj CA is
    # already in the system bundle (security.pki above); the two cert vars
    # are still needed because claude's runtime does not consult the system
    # bundle path on its own (verified: /login gets a cert-chain error
    # without SSL_CERT_FILE, and node subprocesses need NODE_EXTRA_CA_CERTS).
    environment = {
      HTTP_PROXY = "http://127.0.0.1:10255";
      HTTPS_PROXY = "http://127.0.0.1:10255";
      NO_PROXY = "localhost,127.0.0.1,::1";
      SSL_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt";
      NODE_EXTRA_CA_CERTS = "/etc/ssl/certs/ca-certificates.crt";
      # claude refuses --dangerously-skip-permissions as root ("cannot be
      # used with root/sudo privileges") unless it sees an already-sandboxed
      # environment. The VM is that sandbox, so declare it; without this the
      # agent starts and immediately exits. `vmcav agent` sets the same when
      # it launches claude interactively; a plain `vmcav shell` does not.
      IS_SANDBOX = "1";
      # A unit has no terminal, so TERM is unset and anything curses-ish
      # falls back to dumb rendering: no colour, no cursor addressing, and
      # output that reads like a terminal with no scrollback. The daemon
      # passes its own environment to the agent, so it has to be stated here.
      # Only the type: the size comes from whatever terminal attaches.
      TERM = "xterm-256color";
    };
    serviceConfig = {
      # The daemon stays in the foreground, so systemd tracks the real process
      # rather than guessing a pid after a double fork. It starts the agent
      # immediately, which is what makes the agent's own work independent of
      # anyone being attached.
      Type = "simple";
      ExecStart = "${caveGuestd}/bin/cave-guestd serve -socket ${agentSocket} "
        + "-- ${caveAgent}/bin/cave-agent";
      Restart = "no";
    };
  };
}
