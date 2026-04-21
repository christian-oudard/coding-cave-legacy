# Coding cave sandbox config: paths and static bwrap args.
#
# Emits a JSON file consumed by cav/bwrap.py at runtime to assemble the
# full bwrap argv. Replaces the bash wrapper that used to live here.
#
# Hardening defaults (all on):
#   - all namespaces unshared (user, pid, ipc, uts, cgroup; net shared with host)
#   - --as-pid-1 so bwrap's argv and environ don't leak via /proc/1
#   - fake /etc/{passwd,group,hosts,hostname,localtime} hide host identity
#   - /proc mask hides kernel build, CPU/memory sysctls, keyring, uptime, etc.
#   - synthetic /proc/cpuinfo hides host CPU brand (Node-compatible)
#   - shadow-proc trick keeps nested caves launchable
#   - seccomp denylist blocks sysinfo(2), keyring, bpf, io_uring, etc.

{ pkgs, nixpkgs }:

let
  # ---- Static files bound into the cave ----

  # Static /etc/passwd and /etc/group. Without these the cave inherits the
  # host's username (e.g. "christian") and host home path
  # (e.g. "/home/christian") via /etc/passwd.
  nologin = "${pkgs.shadow}/bin/nologin";
  cavePasswd = pkgs.writeText "cave-passwd" ''
    root:x:0:0:System administrator:/root:${nologin}
    cave-coder:x:1000:1000::/home/cave-coder:${nologin}
    nobody:x:65534:65534:Unmapped user:/:${nologin}
  '';
  caveGroup = pkgs.writeText "cave-group" ''
    root:x:0:
    cave-coder:x:1000:
    nogroup:x:65534:
  '';

  # Static /etc/hosts. NixOS auto-adds `127.0.0.2 <host-hostname>` which
  # would leak the host machine name.
  caveHosts = pkgs.writeText "cave-hosts" ''
    127.0.0.1 localhost coding-cave
    ::1       localhost coding-cave
  '';

  # UTC localtime. The host's /etc/localtime symlinks into zoneinfo and
  # reveals geographic region. Users who want a specific TZ set it via $TZ
  # on the host; it's forwarded into the cave.
  caveLocaltime = "${pkgs.tzdata}/share/zoneinfo/UTC";

  # /etc/hostname so tools that read it (vs uname) see the cave name.
  caveHostname = pkgs.writeText "cave-hostname" "coding-cave\n";

  # Default .claude.json to skip onboarding for caves without one yet.
  dotClaudeJsonDefault = pkgs.writeText "dot-claude.json" (builtins.toJSON {
    hasCompletedOnboarding = true;
    effortCalloutDismissed = true;
  });

  # Empty regular file used to mask /proc entries. A regular file (not
  # /dev/null) so reads see an empty file instead of EACCES on a char device.
  procMask = pkgs.writeText "cave-proc-mask" "";

  # Empty directory used to mask /proc subtrees (e.g. /proc/asound, which
  # exposes the exact laptop model via /proc/asound/cards).
  procMaskDir = pkgs.runCommand "cave-proc-mask-dir" { } "mkdir $out";

  # Zero-valued /proc/meminfo. An empty file causes glibc's
  # sysconf(_SC_PHYS_PAGES) parser to return uninitialized stack memory
  # because it loops for "MemTotal:" and returns an unset local on no-match.
  # A well-formed file with zero values parses cleanly.
  procMaskMeminfo = pkgs.writeText "cave-proc-meminfo" ''
    MemTotal:              0 kB
    MemFree:               0 kB
    MemAvailable:          0 kB
    Buffers:               0 kB
    Cached:                0 kB
    SwapTotal:             0 kB
    SwapFree:              0 kB
  '';

  # Seccomp BPF denylist. Generated from cav/sandbox/seccomp_gen.py which
  # emits raw cBPF bytecode (struct sock_filter[]).
  seccompArch = if pkgs.stdenv.hostPlatform.isx86_64 then "x86_64" else "aarch64";
  seccompFilter = pkgs.runCommand "cave-seccomp-filter" { } ''
    ${pkgs.python3}/bin/python3 ${../cav/sandbox/seccomp_gen.py} --arch=${seccompArch} > $out
  '';

  # FHS dynamic linker compatibility, so pre-built binaries
  # (e.g. Playwright's Chromium, pip wheels) can execute.
  fhsLdLinux =
    if pkgs.stdenv.hostPlatform.isx86_64 then {
      loader = "${pkgs.glibc}/lib/ld-linux-x86-64.so.2";
      path = "/lib64/ld-linux-x86-64.so.2";
    } else {
      loader = "${pkgs.glibc}/lib/ld-linux-aarch64.so.1";
      path = "/lib/ld-linux-aarch64.so.1";
    };
  gccLib = "${pkgs.stdenv.cc.cc.lib}/lib";

  # ---- Entry script: runs inside the cave as the target process ----

  entry = pkgs.writeShellScript "coding-cave-entry" ''
    # cav itself (for `cav version` etc. inside the cave)
    [ -n "''${CODING_CAVE_BIN:-}" ] && export PATH="$CODING_CAVE_BIN:$PATH"
    # User environment from cave.nix (HM-built packages, env vars, files)
    if [ -n "''${CODING_CAVE_USER_ENV:-}" ] && [ -d "''${CODING_CAVE_USER_ENV}" ]; then
      [ -d "''${CODING_CAVE_USER_ENV}/home-path/bin" ] && \
        export PATH="''${CODING_CAVE_USER_ENV}/home-path/bin:$PATH"
      # shellcheck disable=SC1091
      [ -f "''${CODING_CAVE_USER_ENV}/env.sh" ] && . "''${CODING_CAVE_USER_ENV}/env.sh"
      if [ -d "''${CODING_CAVE_USER_ENV}/files" ]; then
        cd "''${CODING_CAVE_USER_ENV}/files"
        ${pkgs.findutils}/bin/find . -type f | while read -r f; do
          dest="$HOME/$f"
          ${pkgs.coreutils}/bin/mkdir -p "$(${pkgs.coreutils}/bin/dirname "$dest")"
          ${pkgs.coreutils}/bin/chmod u+w "$(${pkgs.coreutils}/bin/dirname "$dest")" 2>/dev/null || true
          [ -f "$dest" ] && ${pkgs.coreutils}/bin/chmod u+w "$dest" 2>/dev/null || true
          ${pkgs.coreutils}/bin/cp --no-preserve=mode "$f" "$dest"
        done
        cd /
      fi
      # Append per-session user CLAUDE.md (bind-mounted by pre-bwrap)
      if [ -f /tmp/.cave-user-claude.md ] && [ -f "$HOME/.claude/CLAUDE.md" ]; then
        ${pkgs.coreutils}/bin/chmod u+w "$HOME/.claude/CLAUDE.md" 2>/dev/null || true
        printf '\n' >> "$HOME/.claude/CLAUDE.md"
        ${pkgs.coreutils}/bin/cat /tmp/.cave-user-claude.md >> "$HOME/.claude/CLAUDE.md"
      fi
    fi
    # Git wrapper (syncs agent branches to host-cave on state changes)
    [ -d "$HOME/bin" ] && export PATH="$HOME/bin:$PATH"
    # Git identity (fallback when no .gitconfig was copied)
    if [ ! -f "$HOME/.gitconfig" ]; then
      [ -n "''${CODING_CAVE_GIT_NAME:-}" ] && ${pkgs.git}/bin/git config --global user.name "$CODING_CAVE_GIT_NAME"
      [ -n "''${CODING_CAVE_GIT_EMAIL:-}" ] && ${pkgs.git}/bin/git config --global user.email "$CODING_CAVE_GIT_EMAIL"
      [ -n "''${CODING_CAVE_GIT_SIGNINGKEY:-}" ] && ${pkgs.git}/bin/git config --global user.signingkey "$CODING_CAVE_GIT_SIGNINGKEY"
      [ "''${CODING_CAVE_GIT_GPGSIGN:-}" = "true" ] && ${pkgs.git}/bin/git config --global commit.gpgsign true
    fi
    # GPG: set ultimate trust on signing key to avoid verification warnings
    _sigkey=$(${pkgs.git}/bin/git config user.signingkey 2>/dev/null || true)
    if [ -n "$_sigkey" ]; then
      ${pkgs.coreutils}/bin/mkdir -p "$HOME/.gnupg"
      ${pkgs.coreutils}/bin/chmod 700 "$HOME/.gnupg" 2>/dev/null || true
      echo "$_sigkey:6:" | ${pkgs.gnupg}/bin/gpg --import-ownertrust 2>/dev/null || true
    fi
    # HTTP proxy (crinj credential injection)
    if [ -n "''${CODING_CAVE_PROXY_ADDR:-}" ]; then
      export HTTP_PROXY="http://''${CODING_CAVE_PROXY_ADDR}"
      export HTTPS_PROXY="http://''${CODING_CAVE_PROXY_ADDR}"
      export http_proxy="$HTTP_PROXY"
      export https_proxy="$HTTPS_PROXY"
      export NO_PROXY="localhost,127.0.0.1,::1"
      export no_proxy="$NO_PROXY"
      if [ -f /etc/crinj-ca.pem ]; then
        ${pkgs.coreutils}/bin/cat ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt /etc/crinj-ca.pem > /tmp/combined-ca.pem
        export SSL_CERT_FILE=/tmp/combined-ca.pem
        export REQUESTS_CA_BUNDLE=/tmp/combined-ca.pem
        export NODE_EXTRA_CA_CERTS=/etc/crinj-ca.pem
      fi
    fi
    cd "/projects/''${CODING_CAVE_PROJECT_SLUG:-project}"
    exec "$@"
  '';

  # ---- Static bwrap args (build-time constant) ----

  staticArgs = [
    # Namespaces. Net is shared with the host; if an isolated netns is
    # needed, slirp4netns is set up from outside before exec.
    "--unshare-user" "--unshare-ipc" "--unshare-pid"
    "--unshare-uts" "--unshare-cgroup"
    "--clearenv" "--new-session"
    "--hostname" "coding-cave"
    "--uid" "1000" "--gid" "1000"
    # --as-pid-1 makes the target process PID 1 directly, hiding bwrap's
    # argv (host paths in --bind flags) and environ (host HOME, consumed
    # CODING_CAVE_* vars) from /proc/1 inside the cave.
    "--as-pid-1"
    # Filesystem base
    "--proc" "/proc"
    "--dev" "/dev"
    "--tmpfs" "/tmp"
    "--tmpfs" "/run"
    "--ro-bind" "/nix/store" "/nix/store"
    "--dir" "/bin"
    "--ro-bind" "${pkgs.bashInteractive}/bin/bash" "/bin/bash"
    "--symlink" "bash" "/bin/sh"
    "--ro-bind" "${pkgs.coreutils}/bin/env" "/usr/bin/env"
    "--ro-bind" fhsLdLinux.loader fhsLdLinux.path

    # Environment: static set
    "--setenv" "HOME" "/home/cave-coder"
    "--setenv" "USER" "cave-coder"
    "--setenv" "LOGNAME" "cave-coder"
    "--setenv" "SHELL" "${pkgs.bashInteractive}/bin/bash"
    "--setenv" "LANG" "en_US.UTF-8"
    "--setenv" "LOCALE_ARCHIVE" "${pkgs.glibcLocales}/lib/locale/locale-archive"
    "--setenv" "TERMINFO_DIRS" "${pkgs.ncurses}/share/terminfo"
    # FHS library path for pre-built native extensions (libstdc++ etc.)
    "--setenv" "LD_LIBRARY_PATH" gccLib
    # Nix inside the cave
    "--setenv" "NIX_CONFIG" "sandbox = false\nexperimental-features = nix-command flakes\nbuild-users-group ="
    "--setenv" "NIX_PATH" "nixpkgs=${nixpkgs}"

    # Environment: belt-and-braces unsets. --clearenv preserves PWD by
    # design (leaking host cwd via /proc/1/environ). The CODING_CAVE_*
    # entries guard against future additions leaking host paths even if
    # --clearenv wouldn't already drop them.
    "--unsetenv" "PWD"
    "--unsetenv" "SHLVL"
    "--unsetenv" "CODING_CAVE_NIX_MERGED"
    "--unsetenv" "CODING_CAVE_HOME"
    "--unsetenv" "CODING_CAVE_WORKSPACES"
    "--unsetenv" "CODING_CAVE_SHARED_REPOS"
    "--unsetenv" "CODING_CAVE_HOST_HOME"
    "--unsetenv" "CODING_CAVE_NIX_DB_DIR"
    "--unsetenv" "CODING_CAVE_GPG_SOCKET"
    "--unsetenv" "CODING_CAVE_GPG_PUBRING"
    "--unsetenv" "CODING_CAVE_GITCONFIG"
    "--unsetenv" "CODING_CAVE_USER_CLAUDE_MD"
    "--unsetenv" "CODING_CAVE_PROXY_CA"
    "--unsetenv" "CODING_CAVE_PROJECT_SLUGS"

    # Host network config. --ro-bind-try silently skips if the host
    # doesn't have the path (e.g. no systemd-resolved).
    "--ro-bind-try" "/etc/nsswitch.conf" "/etc/nsswitch.conf"
    "--ro-bind-try" "/etc/resolv.conf" "/etc/resolv.conf"
    "--ro-bind-try" "/etc/ssl" "/etc/ssl"
    "--ro-bind-try" "/run/systemd/resolve" "/run/systemd/resolve"

    # Cave-local /etc (MUST come after network binds above so they win
    # by bwrap last-wins ordering). Hides host username, hostname,
    # timezone.
    "--ro-bind" "${cavePasswd}" "/etc/passwd"
    "--ro-bind" "${caveGroup}" "/etc/group"
    "--ro-bind" "${caveHosts}" "/etc/hosts"
    "--ro-bind" "${caveLocaltime}" "/etc/localtime"
    "--ro-bind" "${caveHostname}" "/etc/hostname"

    # Shadow procfs for mount_too_revealing(). The kernel blocks a new
    # procfs mount in a user_ns when every existing procfs has locked
    # submounts hiding non-empty paths. The /proc masks below would trip
    # that check and break nested subcave launches. A second `--proc`
    # mount (fresh procfs, not a bind of the host's /proc) has no
    # submounts and satisfies the kernel's fully-visible check. Using
    # `--proc` (not `--bind /proc`) is critical: `--bind /proc` would
    # bind the HOST's procfs, exposing the host's PID namespace and every
    # host process's environ/cmdline/root. `--proc` creates a fresh
    # procfs in the cave's own pid_ns, so /shadow-proc/1 is the cave's
    # init, not the host's.
    #
    # To hide this unmasked procfs from the cave user (who otherwise
    # could read /shadow-proc/cmdline and other kernel-global paths), we
    # overmount a tmpfs at the parent dir. The shadow-proc mount remains
    # in the mount namespace (so mnt_already_visible() finds it when
    # scanning ns->list) but is unreachable by path. The cave user lacks
    # CAP_SYS_ADMIN in the bwrap-created user_ns, so they cannot unmount
    # the tmpfs or bind-mount the shadowed procfs elsewhere. In a child
    # user_ns they'd create, inherited mounts carry MNT_LOCKED, which
    # forbids those operations.
    "--dir" "/run/.cave-priv"
    "--proc" "/run/.cave-priv/shadow-proc"
    "--perms" "0700" "--tmpfs" "/run/.cave-priv"
  ];

  # Env vars conditionally forwarded from the caller's environment.
  forwardEnv = [
    # Terminal (describe the terminal, not binaries)
    "TERM" "COLORTERM" "TZ"
    # Cave identity / config
    "CODING_CAVE_DNS_SERVER" "CODING_CAVE_VERSION"
    "CODING_CAVE_GIT_NAME" "CODING_CAVE_GIT_EMAIL"
    "CODING_CAVE_GIT_SIGNINGKEY" "CODING_CAVE_GIT_GPGSIGN"
    "CODING_CAVE_AGENT_NAME" "CODING_CAVE_LINEAGE"
    "CODING_CAVE_GATEWAY" "CODING_CAVE_PROXY_ADDR"
    "CODING_CAVE_PROJECT_SLUG" "CODING_CAVE_USER_ENV"
    "CODING_CAVE_BIN" "CODING_CAVE_NS_MOUNTPOINT"
    # Dummy token so Claude Code considers itself logged in.
    # Crinj replaces the Authorization header with real credentials.
    "CLAUDE_CODE_OAUTH_TOKEN"
  ];

  # /proc file entries masked with an empty regular file. Hides kernel
  # build/config/symbols, loaded modules, live side channels
  # (interrupts, diskstats), cross-cave boot_id, disk/partition/swap
  # layout, kernel osrelease/version sysctls, uptime/loadavg, and
  # session/user keyrings.
  procMaskFiles = [
    "/proc/cmdline" "/proc/version" "/proc/config.gz"
    "/proc/kallsyms" "/proc/kcore" "/proc/modules" "/proc/kmsg"
    "/proc/iomem" "/proc/ioports"
    "/proc/interrupts" "/proc/diskstats"
    "/proc/partitions" "/proc/swaps" "/proc/devices"
    "/proc/fb" "/proc/misc" "/proc/tty/drivers"
    "/proc/consoles" "/proc/crypto"
    "/proc/uptime" "/proc/loadavg" "/proc/keys" "/proc/key-users"
    "/proc/buddyinfo" "/proc/zoneinfo" "/proc/pagetypeinfo"
    "/proc/vmstat" "/proc/softirqs" "/proc/schedstat" "/proc/locks"
    "/proc/execdomains"
    "/proc/sys/kernel/random/boot_id"
    "/proc/sys/kernel/osrelease" "/proc/sys/kernel/version"
  ];

  # /proc subtrees masked with an empty directory. The audio tree
  # (/proc/asound) exposes the exact laptop model via /proc/asound/cards.
  procMaskDirs = [
    "/proc/asound" "/proc/driver" "/proc/bus"
    "/proc/acpi" "/proc/fs" "/proc/irq" "/proc/pressure"
  ];

  config = {
    bwrap = "${pkgs.bubblewrap}/bin/bwrap";
    bash = "${pkgs.bashInteractive}/bin/bash";
    entry = "${entry}";
    inherit staticArgs forwardEnv procMaskFiles procMaskDirs;
    procMask = "${procMask}";
    procMaskDir = "${procMaskDir}";
    procMaskMeminfo = "${procMaskMeminfo}";
    seccompFilter = "${seccompFilter}";
    dotClaudeJsonDefault = "${dotClaudeJsonDefault}";
  };

in pkgs.writeText "coding-cave-sandbox.json" (builtins.toJSON config)
