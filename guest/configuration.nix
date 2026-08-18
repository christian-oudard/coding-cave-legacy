{ config, pkgs, lib, modulesPath, claudeCode, ... }:

let
  # Where session sockets live, one per agent, named after it. /run, so a
  # reboot cannot leave a stale one behind.
  sessionDir = "/run/cave/sessions";

  # cave-guestd: the process that owns one agent's pty, and the client that
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

  # Guest-side leg of the service plane, one shape for all four outbound
  # bridges: a localhost port the rest of the guest talks to, carried over
  # vsock to the host service behind it. CID 2 is the host (VMADDR_CID_HOST).
  # Re-exposing each service on localhost is what lets the proxy env, the nix
  # substituter and the agent's git remotes stay ignorant of vsock entirely.
  #
  # Socket-activated rather than a long-lived socat, because the alternative
  # was a race: systemd calls a `socat TCP-LISTEN` unit active the instant the
  # process execs, not when it has bound the port, so a unit ordered after it
  # can still arrive to find nothing listening. `clone-repo` failing at boot
  # and taking the agent down with it is the observed shape of that
  # (docs/vm-cave-todo.md). systemd binds these itself at sockets.target,
  # before anything that depends on them runs, so the ordering is a guarantee.
  #
  # It also deletes machinery: no Restart=always papering over a host bridge
  # that was not listening yet, and no requirement that the host end be up
  # before the guest can queue a connection, because the kernel holds it.
  bridgeSocket = port: description: {
    inherit description;
    wantedBy = [ "sockets.target" ];
    # Ordered ahead of everything that dials one of these on startup, which is
    # what a plain service could claim but not deliver.
    before = [ "vm-root-agent.service" "clone-repo.service" ];
    socketConfig = {
      ListenStream = "127.0.0.1:${toString port}";
      # Accept, so systemd hands each connection to its own socat on stdin,
      # which is what socat's own `fork` did anyway.
      Accept = true;
    };
  };

  bridgeService = vsock: {
    description = "Carry one connection to host vsock ${toString vsock}";
    # One instance per connection means transient failures accumulate: a peer
    # that resets mid-transfer leaves socat exiting non-zero, and without this
    # the instance sits in `failed` forever. That is not merely untidy. A unit
    # left failed makes every later switch-to-configuration report the whole
    # deploy as failed, which is the same trap logrotate sprang below, and it
    # buries a real failure in a list of dead connections.
    unitConfig.CollectMode = "inactive-or-failed";
    serviceConfig = {
      ExecStart = "${pkgs.socat}/bin/socat STDIO VSOCK-CONNECT:2:${toString vsock}";
      StandardInput = "socket";
    };
  };

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
      --replace '#!/usr/bin/env python3' '#!${pkgs.python3}/bin/python3' \
      --replace '@REAL_GPG@' '${pkgs.gnupg}/bin/gpg'
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

  # --- VM shape (direct kernel boot) ---
  # Headless: on x86_64 the boot log goes to the 8250 serial port, which CHV
  # exposes with `--serial file=<log>`. Deliberately NOT the virtio console
  # (hvc0): virtio_console is a module, so hvc0 is silent until the initrd
  # loads it and early boot failures would leave no trace. Memory and cpu
  # count are host-side VMM flags, not guest config.
  #
  # Apple's Virtualization.framework exposes no legacy UART, so on aarch64
  # the virtio console is the only one there is and hvc0 is forced. The early
  # boot blind spot above is unavoidable there; vfkit's --device
  # virtio-serial,logFilePath is where that log lands.
  boot.kernelParams =
    if pkgs.stdenv.hostPlatform.isAarch64
    then [ "console=hvc0" ]
    else [ "console=ttyS0" ];

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
  # `nofail` plus a short timeout keep a boot with no seed disk attached from
  # hanging on a device that is never going to appear.
  #
  # Delivered as a small read-only ext4 disk rather than a filesystem share:
  # CHV has no 9p, and its virtio-fs would drag in a virtiofsd the launcher
  # has to supervise (vm-cave-design.md notes the stock one is unusable
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

  # The inbound leg: terminal attach is the host connecting in, so something
  # must accept AF_VSOCK on 22 and hand it to sshd. systemd-ssh-generator does
  # this by itself under Cloud Hypervisor, but not under vfkit, where it binds
  # AF_UNIX only and leaves nothing answering, so attach silently has no
  # listener. This is the fallback for that case, not the normal path.
  systemd.services.vsock-ssh = {
    description = "Accept host ssh on vsock 22 if nothing else does";
    wantedBy = [ "multi-user.target" ];
    after = [ "sshd.service" ];
    serviceConfig = {
      ExecStart = "${pkgs.socat}/bin/socat "
        + "VSOCK-LISTEN:22,fork,reuseaddr TCP:127.0.0.1:22";
      # Where the generator did bind the port, socat cannot, and exits 1
      # immediately. That is the expected outcome, not a failure: the port is
      # answered either way. Saying so matters beyond tidiness, because a unit
      # left in failed state makes every later switch-to-configuration report
      # the deploy as failed (the same trap as logrotate below), and with
      # Restart it also burns the start limit on a race it cannot win.
      SuccessExitStatus = 1;
      Restart = "on-failure";
      RestartSec = 1;
    };
  };

  # logrotate's config check rejected the store-owned config, which fails on
  # boot and, worse, makes switch-to-configuration return non-zero. The
  # agent's redeploy loop reads that as a failed deploy and would roll back a
  # good one. logrotate isn't needed here (journald self-rotates), so disable
  # it. The original diagnosis was ownership seen through a 9p-mounted
  # /nix/store, which cannot be it any more: the store is on the ext4 root
  # now. Left disabled because nothing here wants logrotate either way, so
  # whether the check would still fail is untested.
  services.logrotate.enable = false;

  # The service plane's guest end. CHV's vsock is the unix-socket hybrid: a
  # guest connection to port N surfaces on the host as the unix socket
  # <vsock.sock>_N, which cave-vm answers. See bridgeSocket in the let block
  # above for the shape and for why these are socket units rather than
  # long-lived socats.
  systemd.sockets = {
    vsock-crinj = bridgeSocket 10255 "Bridge 127.0.0.1:10255 to host crinj over vsock 7200";
    vsock-gpg = bridgeSocket 7400 "Bridge 127.0.0.1:7400 to the host gpg signing broker over vsock 7400";
    vsock-nix = bridgeSocket 7100 "Bridge 127.0.0.1:7100 to the host nix binary cache over vsock 7100";
    vsock-git = bridgeSocket 9418 "Bridge 127.0.0.1:9418 to the host git daemon over vsock 7300";

    # The host's command channel, JSON over HTTP. The same argument as the
    # bridges, one layer down: systemd binds AF_VSOCK itself, so the port is
    # listening before the daemon behind it has started and a caller cannot
    # race its startup. Not a bridge, so it is spelled out: the guest is the
    # server here, and cave-guestd accepts on the socket rather than socat
    # handing one connection to something else.
    cave-control = {
      description = "Host control plane (vsock 7001)";
      wantedBy = [ "sockets.target" ];
      socketConfig = {
        # An empty CID means "this guest", whichever one the VMM assigned.
        ListenStream = "vsock::7001";
        # One daemon serving the listener, not one process per connection.
        Accept = false;
      };
    };
  };

  systemd.services."vsock-crinj@" = bridgeService 7200;
  systemd.services."vsock-gpg@" = bridgeService 7400;
  systemd.services."vsock-nix@" = bridgeService 7100;
  systemd.services."vsock-git@" = bridgeService 7300;

  # Signing. The gpg-agent socket is deliberately NOT forwarded: a unix socket
  # cannot cross the VM boundary, and re-exposing the agent on a port would
  # hand signing to every process that can reach it, which is strictly weaker
  # than the 0600 socket a container cave gets. Instead the host runs a broker
  # that accepts a payload, validates the arguments, pins the key, and signs.

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

  # What the agent knows that a checkout does not tell it: that it is root in a
  # VM, which channels exist, that nothing it commits is visible until it
  # pushes, and that the approval gate is the point rather than an obstacle.
  # `vm-root-agent.md` calls for exactly this and nothing implemented it, so
  # every agent so far started with the repo's CLAUDE.md, which is written for
  # someone working on the host.
  #
  # A user-level CLAUDE.md rather than a system prompt: it composes with the
  # repo's own instead of replacing them, and the agent can read the file it is
  # working from.
  systemd.services.install-agent-instructions = {
    description = "Install the root agent's standing instructions";
    wantedBy = [ "multi-user.target" ];
    before = [ "vm-root-agent.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.writeShellScript "install-agent-instructions" ''
        set -eu
        install -d -m 700 /root/.claude
        install -m 644 ${./CLAUDE.md} /root/.claude/CLAUDE.md
      ''}";
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
  # keeps a boot with no seed able to commit rather than failing at the last
  # step of the loop; it is deliberately obviously-not-a-person.
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

  # The host cache's address and signing key arrive on the seed disk, because
  # the key is generated on whichever machine this VM is running on and so
  # cannot be committed here or baked into the image. `!include` rather than
  # `include`: a boot with no seed attached must still have a working nix, and
  # the leading bang is what makes a missing file not an error.
  #
  # require-sigs stays at its default of true. That is what the fragment's
  # trusted-public-keys line is for, and it is why the host signs at all: the
  # vsock link cannot be reached from outside this machine, but "unreachable"
  # is not the same claim as "verified", and only the second one survives
  # someone later deciding to reach the cache another way.
  nix.extraOptions = ''
    !include /etc/cave-host/nix.conf
  '';

  # --- Guest toolchain: the minimal floor, nothing more ---
  # This list is also the agent's PATH: its unit resolves tools through
  # /run/current-system/sw/bin rather than through store paths frozen when the
  # daemon started, so anything the agent needs has to be here to be reachable
  # after a deploy. bash and nix are named explicitly for that reason, rather
  # than relied on as somebody else's default.
  environment.systemPackages = with pkgs; [
    git
    caveGuestd
    caveAgent
    claudeCode
    bash
    nix
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
  # loudly). CAVE_REPO_URL defaults to the host git daemon, reached on
  # localhost where the vsock-git bridge re-exposes it.
  systemd.services.clone-repo = {
    description = "Clone the coding-cave repo for the agent";
    # The git bridge, not network-online.target: there is no NIC, so
    # networkd-wait-online would only ever time out.
    # The socket, not a service: systemd has bound 9418 by the time this
    # runs, which is the whole point of the change. Waiting on a socat
    # service only ever meant waiting for a process to exist.
    after = [ "vsock-git.socket" ];
    requires = [ "vsock-git.socket" ];
    before = [ "vm-root-agent.service" ];
    wantedBy = [ "multi-user.target" ];
    path = [ pkgs.git ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.writeShellScript "clone-repo" ''
        set -eu
        repo=/root/coding-cave
        # The project's shared bare repo, which is the only thing the host
        # daemon serves. Never the human's working checkout: an agent must not
        # be able to write a branch into the tree someone is standing in.
        url="''${CAVE_REPO_URL:-git://127.0.0.1/cave-shared.git}"
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
  # claude as root with bypass permissions, its pty held by a cave-guestd of
  # its own so it survives the ssh connection going away. Deliberately not a
  # multiplexer: nothing here wants windows, panes, a status bar or a prefix
  # key, and a multiplexer's scrollback is worse than the terminal's own.
  # Defined here so the agent redeploys its own unit via nixos-rebuild.
  #
  # A template, one instance per agent, because the alternative is one daemon
  # holding every pty, and then its crashes, its OOM kills and its own upgrade
  # all end every session at once. systemd is the only thing parenting an
  # agent: `cave-guestd attach` starts an instance it cannot find rather than
  # forking one, so nothing an agent is doing belongs to the ssh connection
  # that went looking for it.
  systemd.services."cave-session@" = {
    description = "Agent session %i (claude as root, its pty held by cave-guestd)";
    after = [ "vsock-crinj.socket" "seed-claude-config.service" "clone-repo.service" ];
    wants = [ "vsock-crinj.socket" ];
    requires = [ "seed-claude-config.service" "clone-repo.service" ];
    # Still false, for the one case the split does not cover: this unit's own
    # definition changing. A session is exactly what may not be restarted
    # under a working agent, so a new binary reaches sessions started after
    # the deploy, and the live ones keep the one they have.
    restartIfChanged = false;
    # Deliberately not a list of store paths. This unit is not restarted by a
    # switch, by design, so anything resolved when it started is frozen for as
    # long as the daemon lives: an agent that rebuilt its own system would
    # still be running the git, nix and claude of the generation it started
    # under. /run/current-system/sw/bin follows the generation instead, so a
    # session begun after a deploy gets the new tools while live sessions are
    # left alone.
    path = [ "/run/current-system/sw" ];
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
      # agent starts and immediately exits. It is set here rather than by
      # `vmcav agent`, which only attaches to the session this unit already
      # started; a claude launched by hand from `vmcav shell` needs its own.
      IS_SANDBOX = "1";
      # systemd sets HOME only for units with User=, so a root unit gets none
      # and every tool that resolves config through $HOME misbehaves under the
      # agent. git is the one that bites: it never reads /root/.gitconfig, so
      # the seeded identity is invisible and `git commit` fails with "Author
      # identity unknown" even though the seed installed it correctly. claude
      # itself is unaffected (node's os.homedir() falls back to the passwd
      # entry), which is why this survived bootstrap unnoticed.
      HOME = "/root";
      # A unit has no terminal, so TERM is unset and anything curses-ish
      # falls back to dumb rendering: no colour, no cursor addressing, and
      # output that reads like a terminal with no scrollback. The daemon
      # passes its own environment to the agent, so it has to be stated here.
      # Only the type: the size comes from whatever terminal attaches.
      TERM = "xterm-256color";
    };
    serviceConfig = {
      # The session process stays in the foreground and exits with its agent,
      # so systemd tracks the real process and the unit's state is the
      # session's state rather than a guess about a pid after a double fork.
      Type = "simple";
      # The session process's own path is pinned, because nothing can swap a
      # process that is holding a pty. The agent's is not: naming it through
      # the current system means a session started after a deploy picks up
      # whatever that deploy installed. Baking the store path here was a
      # self-update bug -- the agent could rebuild its own system, activate
      # it, and still launch the previous claude forever.
      ExecStart = "${caveGuestd}/bin/cave-guestd session -id %i -dir ${sessionDir} "
        + "-- /run/current-system/sw/bin/cave-agent";
      # An agent that exits stays exited. Coming back is someone asking for it
      # again, which starts this unit afresh.
      Restart = "no";
    };
  };

  # --- The control plane ---
  # The host's command channel, JSON over HTTP on vsock 7001. Everything
  # routine `cav` asks the guest comes through here, so that ssh is a human's
  # way in and nothing else depends on it. Its socket is declared with the
  # bridges above; this is the daemon behind it.
  systemd.services.cave-control = {
    description = "Host control plane";
    # No wantedBy: the socket starts this, and a guest nobody talks to should
    # not run it. It answers questions about the session units rather than
    # owning them, so it can be restarted under a live agent, which is exactly
    # what a deploy from inside the VM will do to it.
    requires = [ "cave-control.socket" ];
    after = [ "cave-control.socket" ];
    # The token is host-provided and arrives on the seed disk. Without it there
    # is nothing to authenticate against and the daemon refuses to start, which
    # is the right failure: an unauthenticated control plane is worse than none.
    unitConfig.ConditionPathExists = "/etc/cave-host/control-token";
    serviceConfig = {
      ExecStart = "${caveGuestd}/bin/cave-guestd serve -dir ${sessionDir}";
      Restart = "on-failure";
      RestartSec = 1;
    };
  };

  # The root agent is session 1, and is the one instance that starts by itself:
  # it is doing work whether or not anyone attaches. Every other instance is
  # created by an attach. A drop-in rather than a unit, so the template above
  # stays the single definition of what a session is.
  systemd.units."cave-session@1.service" = {
    overrideStrategy = "asDropin";
    wantedBy = [ "multi-user.target" ];
  };
}
