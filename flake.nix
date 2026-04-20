{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    jail-nix.url = "sourcehut:~alexdavid/jail.nix";
    crinj = {
      url = "github:christian-oudard/crinj";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    claude-code = {
      url = "github:sadjow/claude-code-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, jail-nix, crinj, claude-code, home-manager }:
    let
      systems = [ "x86_64-linux" ];
      eachSystem = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs {
        inherit system;
        config.allowUnfreePredicate = pkg: (nixpkgs.lib.getName pkg) == "claude-code";
      }));
    in {
      lib.mkCaveEnv = import ./lib/mkCaveEnv.nix {
        inherit nixpkgs home-manager;
      };

      # Home-manager module: deep-merge layer over programs.claude-code.
      # Import this from any HM config that wants to use
      # `coding-cave.claude-code.{bundles,hooks,memoryFragments,extraSettings}`.
      homeModules.claudeCode = ./modules/claude-code.nix;
      homeModules.default = self.homeModules.claudeCode;

      packages = eachSystem (pkgs:
        let
          jail = jail-nix.lib.init pkgs;
          python = pkgs.python3.withPackages (ps: [ ps.click ]);

          # Default .claude.json content used to skip onboarding for caves
          # without a user .claude.json yet. Mutable file, not managed by HM.
          dotClaudeJsonDefault = pkgs.writeText "dot-claude.json" (builtins.toJSON {
            hasCompletedOnboarding = true;
            effortCalloutDismissed = true;
          });

          seccompArch = if pkgs.stdenv.hostPlatform.isx86_64 then "x86_64" else "aarch64";
          seccompFilter = pkgs.runCommand "cave-seccomp-filter" {
            nativeBuildInputs = [ python ];
          } ''
            python3 ${./cav/seccomp_gen.py} --arch=${seccompArch} > $out
          '';

          # FHS dynamic linker compatibility, so pre-built binaries
          # (e.g. Playwright's Chromium, pip wheels) can execute.
          fhsLdLinux = if pkgs.stdenv.hostPlatform.isx86_64
            then { loader = "${pkgs.glibc}/lib/ld-linux-x86-64.so.2"; path = "/lib64/ld-linux-x86-64.so.2"; }
            else { loader = "${pkgs.glibc}/lib/ld-linux-aarch64.so.1"; path = "/lib/ld-linux-aarch64.so.1"; };
          gccLib = "${pkgs.stdenv.cc.cc.lib}/lib";

          # Empty regular file used to mask host-fingerprinting /proc
          # entries. A regular file (not /dev/null) so reads see an
          # empty file instead of EACCES on a char device.
          procMask = pkgs.writeText "cave-proc-mask" "";

          # Empty directory used to mask /proc subtrees (e.g. /proc/asound,
          # which exposes the exact laptop model via /proc/asound/cards).
          procMaskDir = pkgs.runCommand "cave-proc-mask-dir" { } "mkdir $out";

          # Zero-valued /proc/meminfo. An empty file causes glibc's
          # sysconf(_SC_PHYS_PAGES) parser to return uninitialized stack
          # memory because it loops looking for "MemTotal:" and returns
          # an unset local on no-match. Giving it a well-formed file with
          # zero values makes the parser return a clean 0.
          procMaskMeminfo = pkgs.writeText "cave-proc-meminfo" ''
            MemTotal:              0 kB
            MemFree:               0 kB
            MemAvailable:          0 kB
            Buffers:               0 kB
            Cached:                0 kB
            SwapTotal:             0 kB
            SwapFree:              0 kB
          '';

          # Static /etc/passwd and /etc/group. jail.nix's base-permissions
          # include a `fake-passwd` combinator that generates these from
          # the host caller's `id -un`, `id -u`, `id -g`, and $HOME. That
          # leaks the host username (e.g. "christian") and host home path
          # (e.g. "/home/christian") into /etc/passwd inside the cave.
          # Bind these later so they win over fake-passwd's ro-bind.
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

          # Static /etc/hosts. NixOS auto-adds `127.0.0.2 <host-hostname>`
          # to /etc/hosts for apps that resolve their own hostname. Inside
          # the cave that leaks the host machine's name, so replace with
          # just localhost plus the cave's own uts hostname.
          caveHosts = pkgs.writeText "cave-hosts" ''
            127.0.0.1 localhost coding-cave
            ::1       localhost coding-cave
          '';

          # UTC localtime. The host's /etc/localtime symlinks into the
          # tzdata zoneinfo tree and reveals the host's geographic region.
          # Use UTC inside the cave; if users want a specific timezone,
          # set TZ=<name> on the host and it's forwarded via try-fwd-env.
          caveLocaltime = "${pkgs.tzdata}/share/zoneinfo/UTC";

          sandbox = jail "coding-cave" pkgs.bashInteractive (with jail.combinators; [
            no-die-with-parent
            (set-hostname "coding-cave")
            (unsafe-add-raw-args "--uid 1000 --gid 1000")
            # --as-pid-1 makes the target process PID 1 directly rather than
            # bwrap installing a reaper. This hides bwrap's argv (host paths
            # in --bind flags) and bwrap's own environ (host HOME, consumed
            # CODING_CAVE_* vars) from /proc/1 inside the cave.
            (unsafe-add-raw-args "--as-pid-1")
            (unsafe-add-raw-args "--dir /bin --ro-bind ${pkgs.bashInteractive}/bin/bash /bin/bash --ro-bind ${pkgs.coreutils}/bin/env /usr/bin/env")
            (unsafe-add-raw-args "--ro-bind ${fhsLdLinux.loader} ${fhsLdLinux.path}")
            # Reimplement jail.nix's `network` combinator without the
            # two sub-combinators that leak host state:
            #   * time-zone symlinks the host's zoneinfo into the cave
            #   * runtime-deep-ro-bind /etc/hosts binds the host's
            #     /etc/hosts (with NixOS's "127.0.0.2 <host-hostname>"
            #     entry) as a symlink to a nix store path, and that
            #     symlink blocks us from binding our own /etc/hosts
            #     later (bwrap follows it and fails with "No such file
            #     or directory" on the read-only store target).
            # Pre-register the `network` include-once key with a no-op
            # so anything that adds `network` later becomes a no-op,
            # then pull in the non-leaky pieces manually.
            (include-once "network" (x: x))
            (share-ns "net")
            (runtime-deep-ro-bind "/etc/nsswitch.conf")
            (runtime-deep-ro-bind "/etc/resolv.conf")
            (runtime-deep-ro-bind "/etc/ssl")
            (try-readonly "/run/systemd/resolve")
            (write-text "/etc/hostname" "coding-cave\n")
            (unsafe-add-raw-args "--hostname coding-cave")
            (add-seccomp seccompFilter)

            # FHS library path for pre-built native extensions (libstdc++ etc.)
            (set-env "LD_LIBRARY_PATH" gccLib)

            # Standard POSIX environment
            (set-env "SHELL" "${pkgs.bashInteractive}/bin/bash")
            (set-env "USER" "cave-coder")
            (set-env "LOGNAME" "cave-coder")
            (set-env "TERMINFO_DIRS" "${pkgs.ncurses}/share/terminfo")
            (set-env "LOCALE_ARCHIVE" "${pkgs.glibcLocales}/lib/locale/locale-archive")
            (set-env "LANG" "en_US.UTF-8")

            # Forward host terminal settings (these describe the terminal,
            # not binaries, so they're safe to forward unconditionally)
            (try-fwd-env "TERM")
            (try-fwd-env "COLORTERM")
            (try-fwd-env "TZ")

            # Nix inside the cave
            (set-env "NIX_CONFIG" "sandbox = false\nexperimental-features = nix-command flakes\nbuild-users-group =")
            (set-env "NIX_PATH" "nixpkgs=${nixpkgs}")

            # Nix store overlay (writable for autonomous package management)
            (add-runtime ''
              if [ -n "''${CODING_CAVE_NIX_MERGED:-}" ]; then
                RUNTIME_ARGS+=(--bind "$CODING_CAVE_NIX_MERGED" /nix/store)
                RUNTIME_ARGS+=(--tmpfs /nix/var --dir /nix/var/nix)
                if [ -n "''${CODING_CAVE_NIX_DB_DIR:-}" ] && [ -d "''${CODING_CAVE_NIX_DB_DIR}" ]; then
                  RUNTIME_ARGS+=(--bind "''${CODING_CAVE_NIX_DB_DIR}" /nix/var/nix/db)
                fi
              fi
            '')

            # Subcave support: /dev/net/tun for slirp4netns
            (add-runtime ''
              [ -e /dev/net/tun ] && RUNTIME_ARGS+=(--dev-bind /dev/net/tun /dev/net/tun)
            '')

            # Persistent home
            (set-env "HOME" "/home/cave-coder")
            (add-runtime ''
              RUNTIME_ARGS+=(--bind "$CODING_CAVE_HOME" /home/cave-coder)
            '')

            # Seed mutable .claude.json (onboarding skip) only if absent.
            # Everything else under ~/.claude/ comes from the user-env's
            # baked files/ tree, copied in by the inside-bwrap entry script.
            (add-runtime ''
              if [ ! -f "$CODING_CAVE_HOME/.claude.json" ]; then
                cp --no-preserve=mode ${dotClaudeJsonDefault} \
                  "$CODING_CAVE_HOME/.claude.json"
              fi
              # Bind the host's per-session CLAUDE.md addendum into the
              # namespace at a stable path so the entry script can append
              # it to the freshly-copied ~/.claude/CLAUDE.md.
              if [ -n "''${CODING_CAVE_USER_CLAUDE_MD:-}" ] && \
                 [ -f "''${CODING_CAVE_USER_CLAUDE_MD}" ]; then
                RUNTIME_ARGS+=(--ro-bind "$CODING_CAVE_USER_CLAUDE_MD" /tmp/.cave-user-claude.md)
              fi
            '')

            # Agent workspaces at /projects/<slug> (colon-separated lists)
            (add-runtime ''
              RUNTIME_ARGS+=(--dir /projects)
              IFS=: read -ra _ws <<< "''${CODING_CAVE_WORKSPACES:-}"
              IFS=: read -ra _slugs <<< "''${CODING_CAVE_PROJECT_SLUGS:-}"
              for i in "''${!_ws[@]}"; do
                RUNTIME_ARGS+=(--bind "''${_ws[$i]}" "/projects/''${_slugs[$i]}")
              done
            '')

            # Shared repos (read-only for alternates object access).
            # Mount under /projects/<slug>/.coding-cave/ instead of at
            # the host-absolute path, to avoid leaking the host home dir.
            # The path must match constants.HOST_SHARED_REPO_DIR.
            (add-runtime ''
              IFS=: read -ra _repos <<< "''${CODING_CAVE_SHARED_REPOS:-}"
              IFS=: read -ra _slugs <<< "''${CODING_CAVE_PROJECT_SLUGS:-}"
              for i in "''${!_repos[@]}"; do
                RUNTIME_ARGS+=(--ro-bind "''${_repos[$i]}" "/projects/''${_slugs[$i]}/.coding-cave/host-cave-shared.git")
              done
            '')

            # Git config (copy host .gitconfig into persistent home)
            (add-runtime ''
              if [ -n "''${CODING_CAVE_GITCONFIG:-}" ] && [ -f "''${CODING_CAVE_GITCONFIG}" ]; then
                cp --no-preserve=mode "''${CODING_CAVE_GITCONFIG}" "$CODING_CAVE_HOME/.gitconfig"
              fi
            '')

            # GPG: socket + public keyring for commit signing
            # Bind the host agent socket to BOTH the homedir path and the
            # runtime dir path. gpg prefers /run/user/<uid>/gnupg/ and will
            # auto-start a local (keyless) agent there if it finds nothing.
            # Binding at the bwrap level prevents anything from clobbering it.
            (add-runtime ''
              if [ -n "''${CODING_CAVE_GPG_SOCKET:-}" ] && [ -e "''${CODING_CAVE_GPG_SOCKET}" ]; then
                RUNTIME_ARGS+=(--bind "$CODING_CAVE_GPG_SOCKET" /home/cave-coder/.gnupg/S.gpg-agent)
                RUNTIME_ARGS+=(--dir /run/user/1000/gnupg)
                RUNTIME_ARGS+=(--bind "$CODING_CAVE_GPG_SOCKET" /run/user/1000/gnupg/S.gpg-agent)
              fi
              if [ -n "''${CODING_CAVE_GPG_PUBRING:-}" ] && [ -f "''${CODING_CAVE_GPG_PUBRING}" ]; then
                RUNTIME_ARGS+=(--ro-bind "$CODING_CAVE_GPG_PUBRING" "/home/cave-coder/.gnupg/$(basename "$CODING_CAVE_GPG_PUBRING")")
              fi
            '')

            # Proxy CA certificate (crinj)
            (add-runtime ''
              if [ -n "''${CODING_CAVE_PROXY_CA:-}" ] && [ -f "''${CODING_CAVE_PROXY_CA}" ]; then
                RUNTIME_ARGS+=(--ro-bind "$CODING_CAVE_PROXY_CA" /etc/crinj-ca.pem)
              fi
            '')

            # DNS: override resolv.conf inside the network namespace.
            # The host's resolv.conf may point to a loopback resolver (e.g.
            # systemd-resolved at 127.0.0.53) which is unreachable in the
            # namespace. Point to slirp4netns's built-in DNS forwarder
            # (10.0.2.3) which forwards queries to the host's resolver.
            (add-runtime ''
              if [ -n "''${CODING_CAVE_DNS_SERVER:-}" ]; then
                _RESOLV=$(mktemp)
                echo "nameserver ''${CODING_CAVE_DNS_SERVER}" > "$_RESOLV"
                RUNTIME_ARGS+=(--ro-bind "$_RESOLV" /etc/resolv.conf)
              fi
            '')

            # Remove PWD from the child env. bwrap's --clearenv preserves
            # PWD by design, so without --unsetenv it leaks the host cwd
            # via /proc/1/environ (now the target process, thanks to
            # --as-pid-1). The CODING_CAVE_* entries are belt-and-braces:
            # they aren't try-fwd-env'd so --clearenv already drops them,
            # but the list guards against future additions accidentally
            # re-exposing host paths.
            (add-runtime ''
              for _v in \
                PWD SHLVL \
                CODING_CAVE_NIX_MERGED CODING_CAVE_HOME \
                CODING_CAVE_WORKSPACES CODING_CAVE_SHARED_REPOS \
                CODING_CAVE_HOST_HOME CODING_CAVE_NIX_DB_DIR \
                CODING_CAVE_GPG_SOCKET CODING_CAVE_GPG_PUBRING \
                CODING_CAVE_GITCONFIG CODING_CAVE_USER_CLAUDE_MD \
                CODING_CAVE_PROXY_CA CODING_CAVE_PROJECT_SLUGS \
              ; do
                RUNTIME_ARGS+=(--unsetenv "$_v")
              done
            '')

            # Static /etc that overrides host-derived content. jail.nix's
            # fake-passwd bakes in the host's username and $HOME; `network`
            # pulls in runtime-deep-ro-bind /etc/hosts which reveals the
            # host's hostname (NixOS appends `127.0.0.2 <host-hostname>`).
            # The time-zone combinator (also via network) is suppressed
            # by the include-once above. Emit these via add-runtime so
            # they land at the END of RUNTIME_ARGS, after the network
            # runtime binds, and win by bwrap last-wins ordering.
            (add-runtime ''
              RUNTIME_ARGS+=(
                --ro-bind ${cavePasswd} /etc/passwd
                --ro-bind ${caveGroup} /etc/group
                --ro-bind ${caveHosts} /etc/hosts
                --ro-bind ${caveLocaltime} /etc/localtime
              )
            '')

            # Shadow procfs for mount_too_revealing(). The kernel blocks
            # a new procfs mount in a user_ns when every existing procfs
            # has locked submounts hiding non-empty paths. The /proc
            # masks below would trip that check and break nested subcave
            # launches. A second `--proc` mount (fresh procfs, not a
            # bind of the host's /proc) has no submounts and satisfies
            # the kernel's fully-visible check. Using `--proc` (not
            # `--bind /proc`) is critical: `--bind /proc` would bind the
            # HOST's procfs, exposing the host's PID namespace and every
            # host process's environ/cmdline/root. `--proc` creates a
            # fresh procfs in the cave's own pid_ns, so /shadow-proc/1
            # is the cave's init, not the host's.
            #
            # To hide this unmasked procfs from the cave user (who
            # otherwise could read /shadow-proc/cmdline and other
            # kernel-global paths), we overmount a tmpfs at the parent
            # dir. The shadow-proc mount remains in the mount namespace
            # (so mnt_already_visible() finds it when scanning ns->list)
            # but is unreachable by path. The cave user lacks
            # CAP_SYS_ADMIN in the bwrap-created user_ns, so they cannot
            # unmount the tmpfs or bind-mount the shadowed procfs
            # elsewhere. In a child user_ns they'd create, inherited
            # mounts carry MNT_LOCKED, which forbids those operations.
            (unsafe-add-raw-args "--dir /run/.cave-priv --proc /run/.cave-priv/shadow-proc --perms 0700 --tmpfs /run/.cave-priv")

            # Mask host-system fingerprinting via /proc. Binding an empty
            # regular file (procMask) or empty directory (procMaskDir)
            # hides kernel build/config/symbols, loaded modules, hardware
            # interrupts/diskstats (live side channels), the cross-cave
            # boot_id, disk/partition/swap layout, PCI and audio device
            # trees (the audio tree exposes the exact laptop model), the
            # kernel osrelease/version sysctls, host RAM totals, host
            # uptime/loadavg, and the session/user keyring listing.
            # Works with the shadow mount above so nested caves still launch.
            (add-runtime ''
              for _p in \
                /proc/cmdline /proc/version /proc/config.gz \
                /proc/kallsyms /proc/kcore /proc/modules /proc/kmsg \
                /proc/iomem /proc/ioports \
                /proc/interrupts /proc/diskstats \
                /proc/partitions /proc/swaps /proc/devices \
                /proc/fb /proc/misc /proc/tty/drivers \
                /proc/consoles /proc/crypto \
                /proc/uptime /proc/loadavg /proc/keys /proc/key-users \
                /proc/buddyinfo /proc/zoneinfo /proc/pagetypeinfo \
                /proc/vmstat /proc/softirqs /proc/schedstat /proc/locks \
                /proc/execdomains \
                /proc/sys/kernel/random/boot_id \
                /proc/sys/kernel/osrelease /proc/sys/kernel/version \
              ; do
                RUNTIME_ARGS+=(--ro-bind-try ${procMask} "$_p")
              done
              for _d in \
                /proc/asound /proc/driver /proc/bus \
                /proc/acpi /proc/fs /proc/irq /proc/pressure \
              ; do
                RUNTIME_ARGS+=(--ro-bind-try ${procMaskDir} "$_d")
              done
              RUNTIME_ARGS+=(--ro-bind ${procMaskMeminfo} /proc/meminfo)
            '')

            # Synthetic /proc/cpuinfo. Node's os.cpus() parses this file
            # and crashes on empty input; tools like make -j / nproc use
            # sched_getaffinity(2) instead, so we don't need the real
            # file. Emit N generic processor blocks (N = sched CPUs via
            # nproc) with a fixed fake model to hide the host CPU brand.
            (add-runtime ''
              _cpuinfo=$(${pkgs.coreutils}/bin/mktemp)
              _n=$(${pkgs.coreutils}/bin/nproc 2>/dev/null || echo 1)
              for ((_i=0; _i<_n; _i++)); do
                printf 'processor\t: %d\nvendor_id\t: GenericCPU\ncpu family\t: 0\nmodel\t\t: 0\nmodel name\t: Generic CPU\nstepping\t: 0\ncpu MHz\t\t: 0.000\ncache size\t: 0 KB\nsiblings\t: %d\ncore id\t\t: %d\ncpu cores\t: %d\nflags\t\t:\nbogomips\t: 0.00\n\n' "$_i" "$_n" "$_i" "$_n" >> "$_cpuinfo"
              done
              RUNTIME_ARGS+=(--ro-bind "$_cpuinfo" /proc/cpuinfo)
            '')

            # Forward identity and config into the cave
            (try-fwd-env "CODING_CAVE_DNS_SERVER")
            (try-fwd-env "CODING_CAVE_VERSION")
            (try-fwd-env "CODING_CAVE_GIT_NAME")
            (try-fwd-env "CODING_CAVE_GIT_EMAIL")
            (try-fwd-env "CODING_CAVE_GIT_SIGNINGKEY")
            (try-fwd-env "CODING_CAVE_GIT_GPGSIGN")
            (try-fwd-env "CODING_CAVE_AGENT_NAME")
            (try-fwd-env "CODING_CAVE_LINEAGE")
            (try-fwd-env "CODING_CAVE_GATEWAY")
            (try-fwd-env "CODING_CAVE_PROXY_ADDR")
            (try-fwd-env "CODING_CAVE_PROJECT_SLUG")
            (try-fwd-env "CODING_CAVE_USER_ENV")
            (try-fwd-env "CODING_CAVE_BIN")
            # Subcave overlay: binary paths inherited from parent
            (try-fwd-env "CODING_CAVE_NS_MOUNTPOINT")
            # Dummy token so Claude Code considers itself logged in.
            # Crinj replaces the Authorization header with real credentials.
            (try-fwd-env "CLAUDE_CODE_OAUTH_TOKEN")

            # Entry: configure git identity, user env, proxy, cd to project, run command
            (wrap-entry (entry: ''
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
                  ${pkgs.coreutils}/bin/cat /etc/ssl/certs/ca-bundle.crt /etc/crinj-ca.pem > /tmp/combined-ca.pem
                  export SSL_CERT_FILE=/tmp/combined-ca.pem
                  export REQUESTS_CA_BUNDLE=/tmp/combined-ca.pem
                  export NODE_EXTRA_CA_CERTS=/etc/crinj-ca.pem
                fi
              fi
              cd "/projects/''${CODING_CAVE_PROJECT_SLUG:-project}"
              exec "$@"
            ''))
          ]);

          # unshare --map-user/--map-group requires util-linux >= 2.39.
          cav = assert pkgs.lib.versionAtLeast pkgs.util-linux.version "2.39";
            pkgs.stdenv.mkDerivation {
            pname = "cav";
            version = "0.1.0";

            src = pkgs.lib.cleanSource ./.;

            nativeBuildInputs = [ pkgs.makeWrapper ];
            buildInputs = [ python ];
            propagatedBuildInputs = with pkgs; [
              git socat slirp4netns iptables
              util-linux  # unshare + nsenter (>= 2.39 for --map-user)
            ];

            installPhase = ''
              mkdir -p $out/lib/cav $out/bin

              # Install Python package
              cp -r cav/* $out/lib/cav/

              # Patch hook shebang
              substituteInPlace $out/lib/cav/hooks/pre-receive \
                --replace-fail "#!/usr/bin/env python3" "#!${python}/bin/python3"

              # Embed version and sandbox binary path
              substituteInPlace $out/lib/cav/constants.py \
                --replace-fail "@VERSION@" "${self.shortRev or "dev"}"
              substituteInPlace $out/lib/cav/cli.py \
                --replace-fail "@VERSION_DATE@" "${toString (self.lastModified or 0)}"
              substituteInPlace $out/lib/cav/sandbox.py \
                --replace-fail "@SANDBOX_BIN@" "${sandbox}/bin/coding-cave" \
                --replace-fail "@CAV_BIN@" "$out/bin" \
                --replace-fail "@SUPERVISOR_PY@" "$out/lib/cav/supervisor.py"
              substituteInPlace $out/lib/cav/user_env.py \
                --replace-fail "@NIXPKGS_URL@" "path:${nixpkgs}" \
                --replace-fail "@CLAUDE_CODE_NIX_URL@" "path:${claude-code}" \
                --replace-fail "@HOME_MANAGER_URL@" "path:${home-manager}" \
                --replace-fail "@CODING_CAVE_URL@" "path:${self}"

              # Write entry point
              cat > $out/bin/cav <<WRAPPER
              #!${python}/bin/python3
              import sys
              sys.path.insert(0, "$out/lib")
              from cav.cli import main
              main()
              WRAPPER
              chmod +x $out/bin/cav $out/lib/cav/hooks/pre-receive

              # Wrap with runtime dependencies on PATH
              wrapProgram $out/bin/cav \
                --prefix PATH : ${pkgs.lib.makeBinPath [
                  pkgs.git pkgs.gnupg pkgs.socat pkgs.slirp4netns pkgs.iptables
                  pkgs.util-linux
                  crinj.packages.${pkgs.stdenv.hostPlatform.system}.default
                ]}
            '';

            meta.mainProgram = "cav";
          };

        in {
          default = cav;
          inherit sandbox;
        }
      );

      devShells = eachSystem (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.click ps.vulture ]))
            pkgs.git
            pkgs.socat
            pkgs.iptables
            pkgs.util-linux
            pkgs.slirp4netns
          ];
        };
      });

      checks = eachSystem (pkgs: {
        unit = pkgs.runCommand "cav-unit-tests" {
          nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.click ps.vulture ])) pkgs.git ];
        } ''
          cp -r ${pkgs.lib.cleanSource ./.}/* .
          export HOME=$TMPDIR
          git config --global init.defaultBranch main
          git config --global user.email "test@test.com"
          git config --global user.name "Test"
          python3 -m unittest -v tests.test_hook tests.test_cli tests.test_alternates tests.test_nixdb tests.test_lint tests.test_cp_security tests.test_user_env tests.test_supervisor
          touch $out
        '';
      });
    };
}
