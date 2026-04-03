{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    jail-nix.url = "sourcehut:~alexdavid/jail.nix";
    crinj = {
      url = "github:christian-oudard/crinj";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    claude-code-nix = {
      url = "github:sadjow/claude-code-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    nix-claude = {
      url = "github:christian-oudard/nix-claude";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, jail-nix, crinj, claude-code-nix, nix-claude }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      eachSystem = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs {
        inherit system;
        config.allowUnfreePredicate = pkg: (nixpkgs.lib.getName pkg) == "claude-code";
      }));
    in {
      packages = eachSystem (pkgs:
        let
          jail = jail-nix.lib.init pkgs;
          python = pkgs.python3.withPackages (ps: [ ps.click ]);
          claude-code = claude-code-nix.packages.${pkgs.stdenv.hostPlatform.system}.default;
          mkClaudeConfig = nix-claude.lib.mkClaudeConfig;
          defaultStatusline = ./defaults/statusline.sh;
          claudeConfig = mkClaudeConfig {
            inherit pkgs;
            skipOnboarding = true;
            settings = {
              model = "opus";
              reasoning_effort = "high";
              skipDangerousModePermissionPrompt = true;
              sandbox.enabled = false;
              statusLine = {
                type = "command";
                command = "~/.claude/statusline.sh";
              };
            };
            memory.fragments = [ ./defaults/system-claude.md ];
          };

          seccompArch = if pkgs.stdenv.hostPlatform.isx86_64 then "x86_64" else "aarch64";
          seccompFilter = pkgs.runCommand "cave-seccomp-filter" {
            nativeBuildInputs = [ python ];
          } ''
            python3 ${./cav/seccomp_gen.py} --arch=${seccompArch} > $out
          '';

          sandbox = jail "coding-cave" pkgs.bashInteractive (with jail.combinators; [
            no-die-with-parent
            (set-hostname "coding-cave")
            (unsafe-add-raw-args "--uid 1000 --gid 1000")
            (unsafe-add-raw-args "--dir /bin --ro-bind ${pkgs.bashInteractive}/bin/bash /bin/bash --ro-bind ${pkgs.coreutils}/bin/env /usr/bin/env")
            network
            (add-seccomp seccompFilter)

            # Packages available inside the cave
            (add-pkg-deps ([
              claude-code
            ] ++ (with pkgs; [
              nix git gnupg jq
              coreutils bash gnugrep gnused gawk findutils
              curl wget
            ])))

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
            (set-env "NIX_CONFIG" "sandbox = false\nexperimental-features = nix-command flakes")
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

            # Subcave support (opt-in via --recursive):
            # /dev/fuse for nix store overlay, /dev/net/tun for pasta networking
            (try-fwd-env "CODING_CAVE_RECURSIVE")
            (add-runtime ''
              if [ "''${CODING_CAVE_RECURSIVE:-}" = "1" ]; then
                [ -e /dev/fuse ] && RUNTIME_ARGS+=(--dev-bind /dev/fuse /dev/fuse)
                [ -e /dev/net/tun ] && RUNTIME_ARGS+=(--dev-bind /dev/net/tun /dev/net/tun)
              fi
            '')

            # Persistent home
            (set-env "HOME" "/home/cave-coder")
            (add-runtime ''
              RUNTIME_ARGS+=(--bind "$CODING_CAVE_HOME" /home/cave-coder)
            '')

            # Claude Code config seeding (runs on host before bwrap)
            (add-runtime ''
              CLAUDE_DIR="$CODING_CAVE_HOME/.claude"
              mkdir -p "$CLAUDE_DIR"
              # Merge settings: system defaults < user-env overrides
              SETTINGS_ARGS=(${claudeConfig}/settings.json)
              if [ -n "''${CODING_CAVE_USER_ENV:-}" ] && [ -f "''${CODING_CAVE_USER_ENV}/claude/settings.json" ]; then
                SETTINGS_ARGS+=("''${CODING_CAVE_USER_ENV}/claude/settings.json")
              fi
              ${pkgs.jq}/bin/jq -s 'reduce .[] as $item ({}; . * $item)' \
                "''${SETTINGS_ARGS[@]}" > "$CLAUDE_DIR/settings.json"
              chmod 644 "$CLAUDE_DIR/settings.json"
              # Always update statusline script to match current version
              chmod u+w "$CLAUDE_DIR/statusline.sh" 2>/dev/null || true
              cp ${defaultStatusline} "$CLAUDE_DIR/statusline.sh"
              chmod +x "$CLAUDE_DIR/statusline.sh"
              # CLAUDE.md: system + user-env + user global
              {
                cat ${claudeConfig}/CLAUDE.md
                if [ -n "''${CODING_CAVE_USER_ENV:-}" ] && [ -f "''${CODING_CAVE_USER_ENV}/claude/CLAUDE.md" ]; then
                  printf '\n'
                  cat "''${CODING_CAVE_USER_ENV}/claude/CLAUDE.md"
                fi
                if [ -n "''${CODING_CAVE_USER_CLAUDE_MD:-}" ] && [ -f "''${CODING_CAVE_USER_CLAUDE_MD}" ]; then
                  printf '\n'
                  cat "''${CODING_CAVE_USER_CLAUDE_MD}"
                fi
              } > "$CLAUDE_DIR/CLAUDE.md"
              chmod 644 "$CLAUDE_DIR/CLAUDE.md"
              # Skills and commands from user-env
              if [ -n "''${CODING_CAVE_USER_ENV:-}" ]; then
                if [ -d "''${CODING_CAVE_USER_ENV}/claude/skills" ]; then
                  chmod -R u+w "$CLAUDE_DIR/skills" 2>/dev/null || true
                  rm -rf "$CLAUDE_DIR/skills"
                  mkdir -p "$CLAUDE_DIR/skills"
                  cp -r --no-preserve=mode "''${CODING_CAVE_USER_ENV}/claude/skills/." "$CLAUDE_DIR/skills/"
                fi
                if [ -d "''${CODING_CAVE_USER_ENV}/claude/commands" ]; then
                  chmod -R u+w "$CLAUDE_DIR/commands" 2>/dev/null || true
                  rm -rf "$CLAUDE_DIR/commands"
                  mkdir -p "$CLAUDE_DIR/commands"
                  cp -r --no-preserve=mode "''${CODING_CAVE_USER_ENV}/claude/commands/." "$CLAUDE_DIR/commands/"
                fi
              fi
              # dot-claude.json (onboarding skip) — only if no .claude.json yet
              if [ ! -f "$CODING_CAVE_HOME/.claude.json" ]; then
                if [ -n "''${CODING_CAVE_USER_ENV:-}" ] && [ -f "''${CODING_CAVE_USER_ENV}/claude/dot-claude.json" ]; then
                  cp "''${CODING_CAVE_USER_ENV}/claude/dot-claude.json" "$CODING_CAVE_HOME/.claude.json"
                elif [ -f ${claudeConfig}/dot-claude.json ]; then
                  cp ${claudeConfig}/dot-claude.json "$CODING_CAVE_HOME/.claude.json"
                fi
                chmod 644 "$CODING_CAVE_HOME/.claude.json" 2>/dev/null || true
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
            (add-runtime ''
              IFS=: read -ra _repos <<< "''${CODING_CAVE_SHARED_REPOS:-}"
              IFS=: read -ra _slugs <<< "''${CODING_CAVE_PROJECT_SLUGS:-}"
              for i in "''${!_repos[@]}"; do
                RUNTIME_ARGS+=(--ro-bind "''${_repos[$i]}" "/projects/''${_slugs[$i]}/.coding-cave/cave-shared.git")
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

            # DNS: override resolv.conf when inside a pasta network namespace.
            # The host's resolv.conf may point to a loopback resolver (e.g.
            # systemd-resolved at 127.0.0.53) which is unreachable in the
            # namespace's isolated loopback. Point to the mapped host address
            # instead; pasta's --dns-forward handles the actual forwarding.
            (add-runtime ''
              if [ -n "''${CODING_CAVE_DNS_SERVER:-}" ]; then
                _RESOLV=$(mktemp)
                echo "nameserver ''${CODING_CAVE_DNS_SERVER}" > "$_RESOLV"
                RUNTIME_ARGS+=(--ro-bind "$_RESOLV" /etc/resolv.conf)
              fi
            '')

            # Clear env vars that were consumed by add-runtime blocks
            # above. Without this, they leak via /proc/1/environ inside
            # the cave (bwrap is PID 1, same uid, readable).
            # Use bwrap --unsetenv instead of shell unset: jail.nix
            # needs $HOME for ~ expansion in the bwrap command line.
            # HOME itself is already overridden by set-env above.
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

            # Forward identity and config into the cave
            (try-fwd-env "CODING_CAVE_DNS_SERVER")
            (try-fwd-env "CODING_CAVE_VERSION")
            (try-fwd-env "CODING_CAVE_GIT_NAME")
            (try-fwd-env "CODING_CAVE_GIT_EMAIL")
            (try-fwd-env "CODING_CAVE_GIT_SIGNINGKEY")
            (try-fwd-env "CODING_CAVE_GIT_GPGSIGN")
            (try-fwd-env "CODING_CAVE_AGENT_NAME")
            (try-fwd-env "CODING_CAVE_LINEAGE")
            (try-fwd-env "CLAUDE_CODE_OAUTH_TOKEN")
            (try-fwd-env "CODING_CAVE_PROXY_ADDR")
            (try-fwd-env "CODING_CAVE_PROJECT_SLUG")
            (try-fwd-env "CODING_CAVE_USER_ENV")
            (try-fwd-env "CODING_CAVE_BIN")

            # Entry: configure git identity, user env, proxy, cd to project, run command
            (wrap-entry (entry: ''
              # cav itself (for `cav version` etc. inside the cave)
              [ -n "''${CODING_CAVE_BIN:-}" ] && export PATH="$CODING_CAVE_BIN:$PATH"
              # User environment from cave.nix (packages, env vars, files)
              if [ -n "''${CODING_CAVE_USER_ENV:-}" ] && [ -d "''${CODING_CAVE_USER_ENV}" ]; then
                for pkg in "''${CODING_CAVE_USER_ENV}"/packages/*; do
                  [ -d "$pkg/bin" ] && export PATH="$pkg/bin:$PATH"
                done
                # shellcheck disable=SC1091
                [ -f "''${CODING_CAVE_USER_ENV}/env.sh" ] && . "''${CODING_CAVE_USER_ENV}/env.sh"
                if [ -d "''${CODING_CAVE_USER_ENV}/files" ]; then
                  cd "''${CODING_CAVE_USER_ENV}/files"
                  find . -type f | while read -r f; do
                    dest="$HOME/$f"
                    mkdir -p "$(dirname "$dest")"
                    chmod u+w "$(dirname "$dest")" 2>/dev/null || true
                    [ -f "$dest" ] && chmod u+w "$dest" 2>/dev/null || true
                    cp --no-preserve=mode "$f" "$dest"
                  done
                  cd /
                fi
              fi
              # Git wrapper (syncs agent branches to host-cave on state changes)
              [ -d "$HOME/bin" ] && export PATH="$HOME/bin:$PATH"
              # Git identity (fallback when no .gitconfig was copied)
              if [ ! -f "$HOME/.gitconfig" ]; then
                [ -n "''${CODING_CAVE_GIT_NAME:-}" ] && git config --global user.name "$CODING_CAVE_GIT_NAME"
                [ -n "''${CODING_CAVE_GIT_EMAIL:-}" ] && git config --global user.email "$CODING_CAVE_GIT_EMAIL"
                [ -n "''${CODING_CAVE_GIT_SIGNINGKEY:-}" ] && git config --global user.signingkey "$CODING_CAVE_GIT_SIGNINGKEY"
                [ "''${CODING_CAVE_GIT_GPGSIGN:-}" = "true" ] && git config --global commit.gpgsign true
              fi
              # GPG: set ultimate trust on signing key to avoid verification warnings
              _sigkey=$(git config user.signingkey 2>/dev/null || true)
              if [ -n "$_sigkey" ]; then
                mkdir -p "$HOME/.gnupg"
                chmod 700 "$HOME/.gnupg" 2>/dev/null || true
                echo "$_sigkey:6:" | gpg --import-ownertrust 2>/dev/null || true
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
                  cat /etc/ssl/certs/ca-bundle.crt /etc/crinj-ca.pem > /tmp/combined-ca.pem
                  export SSL_CERT_FILE=/tmp/combined-ca.pem
                  export REQUESTS_CA_BUNDLE=/tmp/combined-ca.pem
                  export NODE_EXTRA_CA_CERTS=/etc/crinj-ca.pem
                fi
              fi
              cd "/projects/''${CODING_CAVE_PROJECT_SLUG:-project}"
              exec "$@"
            ''))
          ]);

          cav = pkgs.stdenv.mkDerivation {
            pname = "cav";
            version = "0.1.0";

            src = pkgs.lib.cleanSource ./.;

            nativeBuildInputs = [ pkgs.makeWrapper ];
            buildInputs = [ python ];
            propagatedBuildInputs = with pkgs; [
              fuse-overlayfs git socat passt
            ];

            installPhase = ''
              mkdir -p $out/lib/cav $out/bin

              # Install Python package
              cp -r cav/* $out/lib/cav/

              # Patch hook shebang
              substituteInPlace $out/lib/cav/hooks/pre-receive \
                --replace-fail "#!/usr/bin/env python3" "#!${python}/bin/python3"

              # Embed version and sandbox binary path
              substituteInPlace $out/lib/cav/config.py \
                --replace-fail "@VERSION@" "${self.shortRev or "dev"}"
              substituteInPlace $out/lib/cav/cli.py \
                --replace-fail "@VERSION_DATE@" "${toString (self.lastModified or 0)}"
              substituteInPlace $out/lib/cav/sandbox.py \
                --replace-fail "@SANDBOX_BIN@" "${sandbox}/bin/coding-cave" \
                --replace-fail "@CAV_BIN@" "$out/bin"
              substituteInPlace $out/lib/cav/user_env.py \
                --replace-fail "@NIX_CLAUDE_URL@" "github:christian-oudard/nix-claude/${nix-claude.rev}"

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
                  pkgs.fuse-overlayfs pkgs.fuse3.bin
                  pkgs.git pkgs.gnupg pkgs.socat pkgs.passt
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
          python3 -m unittest -v tests.test_hook tests.test_cli tests.test_alternates tests.test_nixdb tests.test_lint tests.test_cp_security
          touch $out
        '';
      });
    };
}
