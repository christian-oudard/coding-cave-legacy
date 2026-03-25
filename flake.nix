{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    jail-nix.url = "sourcehut:~alexdavid/jail.nix";
    claude-code = {
      url = "github:sadjow/claude-code-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    crinj = {
      url = "github:christian-oudard/crinj";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, jail-nix, claude-code, crinj, ... }: let
    pkgs = import nixpkgs { system = "x86_64-linux"; };
    jail = jail-nix.lib.init pkgs;
    crinj-bin = crinj.packages.x86_64-linux.default;

    defaultClaudeSettings = ./defaults/claude-settings.json;
    defaultStatusline = ./defaults/statusline.sh;
    systemClaudeMd = ./system-claude.md;
    ipcSetup = ./ipc_setup.sh;
    version = self.shortRev or self.dirtyShortRev or "dev";

    # IPC: message sender (available inside cave)
    caveSend = pkgs.writeShellScriptBin "cave-send" ''
      exec ${pkgs.python3}/bin/python3 ${./cave_send.py} "$@"
    '';

    jailHome = "/home/cave-coder";

    baseCombinators = with jail.combinators; [
      no-die-with-parent
      (set-hostname "coding-cave")
      # pasta wraps bwrap in a user namespace where the host user appears as root.
      # Force bwrap to map whatever external UID it has to cave-coder (1000) inside.
      (unsafe-add-raw-args "--uid 1000 --gid 1000")
      # /dev/fuse for fuse-overlayfs (needed for nested caves)
      (add-runtime ''
        [ -e /dev/fuse ] && RUNTIME_ARGS+=(--dev-bind /dev/fuse /dev/fuse)
      '')
      (unsafe-add-raw-args "--symlink /bin/sh /bin/bash")
      # zsh symlink added dynamically when user env includes zsh
      (set-env "SHELL" "/bin/bash")
      (set-env "CODING_CAVE_VERSION" version)
      (set-env "HOME" jailHome)
      # Persistent home directory (bind mount, survives across sessions).
      (add-runtime ''
        CAVE_HOME="$HOME/.local/share/coding-cave/home"
        mkdir -p "$CAVE_HOME"
        RUNTIME_ARGS+=(--bind "$CAVE_HOME" ${jailHome})
      '')
      # `network` prevents bwrap from doing --unshare-net, so it inherits
      # the surrounding network namespace (pasta's isolated one on the host,
      # or the outer cave's namespace when running nested).
      network

      # no-new-session omitted: cave init needs its own session to survive terminal close

      # Nix CLI for autonomous package management inside the cave
      # Writable /nix/store overlay (fuse-overlayfs, mounted by wrapper.py).
      # Host nix DB is seeded at startup so nix doesn't try to re-register
      # (chmod) existing lower-layer paths, which fails on overlayfs.
      # NIX_MERGED and NIX_DB_DIR are read from the process env by
      # add-runtime (host-side); no need to forward into bwrap.
      (add-runtime ''
        if [ -n "''${CODING_CAVE_NIX_MERGED:-}" ]; then
          RUNTIME_ARGS+=(--bind "$CODING_CAVE_NIX_MERGED" /nix/store)
          RUNTIME_ARGS+=(--tmpfs /nix/var --dir /nix/var/nix)
          if [ -n "''${CODING_CAVE_NIX_DB_DIR:-}" ] && [ -d "''${CODING_CAVE_NIX_DB_DIR}" ]; then
            RUNTIME_ARGS+=(--bind "''${CODING_CAVE_NIX_DB_DIR}" /nix/var/nix/db)
          fi
        fi
      '')
      (set-env "NIX_CONFIG" "sandbox = false\nexperimental-features = nix-command flakes")
      (set-env "NIX_PATH" "nixpkgs=${nixpkgs}")

      # Base packages (infrastructure — user packages come from cave.nix)
      (add-pkg-deps (with pkgs; [
        claude-code.packages.x86_64-linux.default
        nix
        git
        ripgrep
        fd
        jq
        curl
        coreutils
        gawk
        gnused
        gnugrep
        findutils
        diffutils
        patch
        gnutar
        gzip
        unzip
        wget
        bash
        less
        file
        ncurses
        which
        glibc.bin
        util-linux
        fuse-overlayfs
        fuse3
        gnupg
        wl-clipboard
      ] ++ [ pkgs."poppler-utils" pkgs.stdenv.cc.cc.lib caveSend ]))
      (set-env "LD_LIBRARY_PATH" "${pkgs.stdenv.cc.cc.lib}/lib")
      (set-env "TERMINFO_DIRS" "${pkgs.ncurses}/share/terminfo")
      (set-env "LOCALE_ARCHIVE" "${pkgs.glibcLocales}/lib/locale/locale-archive")
      (set-env "LANG" "en_US.UTF-8")
      (try-fwd-env "COLORTERM")

      # Git identity: forwarded from host when available (eliminates
      # re-authoring on pull).  Falls back to throwaway identity.
      # These are consumed once in wrap-entry (init) then unset.
      (try-fwd-env "CODING_CAVE_GIT_NAME")
      (try-fwd-env "CODING_CAVE_GIT_EMAIL")
      (try-fwd-env "CODING_CAVE_GPG_AGENT_SOCK")
      # GNUPGHOME is only used by lifecycle.py (host-side bind-mount).

      # Credential forwarding
      (try-fwd-env "CLAUDE_CODE_OAUTH_TOKEN")
      (try-fwd-env "ANTHROPIC_API_KEY")
      # Proxy address (crinj) — consumed in wrap-entry
      (try-fwd-env "CODING_CAVE_PROXY_ADDR")

      # Session identity (runtime — visible to statusline and scripts)
      (try-fwd-env "CODING_CAVE_SESSION_ID")
      (try-fwd-env "CODING_CAVE_SESSION_INDEX")
      (try-fwd-env "CODING_CAVE_PROJECT_PATH")
      # Setup-only: consumed in wrap-entry then unset
      (try-fwd-env "CODING_CAVE_EXTRA_PATH")
      # Host-only vars (add-runtime / lifecycle.py): BWRAP_ARGS,
      # USER_ENV, USER_CLAUDE_MD, GITCONFIG, SESSION_ID, PROJECT
      # are read from the process env before bwrap starts.
      # Claude Code state init
      (add-runtime ''
        CAVE_HOME="$HOME/.local/share/coding-cave/home"
        CLAUDE_DIR="$CAVE_HOME/.claude"
        mkdir -p "$CLAUDE_DIR"
        # Merge default settings (base) with user's existing settings (overlay)
        if [ -f "$CLAUDE_DIR/settings.json" ]; then
          ${pkgs.jq}/bin/jq -s '.[0] * .[1]' ${defaultClaudeSettings} "$CLAUDE_DIR/settings.json" > "$CLAUDE_DIR/settings.json.tmp"
          mv "$CLAUDE_DIR/settings.json.tmp" "$CLAUDE_DIR/settings.json"
        else
          cp ${defaultClaudeSettings} "$CLAUDE_DIR/settings.json"
        fi
        chmod 644 "$CLAUDE_DIR/settings.json"
        if [ ! -f "$CAVE_HOME/.claude.json" ]; then
          printf '%s' '{"hasCompletedOnboarding":true,"theme":"dark","effortCalloutDismissed":true}' > "$CAVE_HOME/.claude.json"
        fi
        # Always update statusline script to match current version
        cp ${defaultStatusline} "$CLAUDE_DIR/statusline.sh"
        chmod +x "$CLAUDE_DIR/statusline.sh"
        # Concatenate system + user CLAUDE.md
        {
          cat ${systemClaudeMd}
          if [ -n "''${CODING_CAVE_USER_CLAUDE_MD:-}" ] && [ -f "''${CODING_CAVE_USER_CLAUDE_MD}" ]; then
            printf '\n'
            cat "''${CODING_CAVE_USER_CLAUDE_MD}"
          fi
        } > "$CLAUDE_DIR/CLAUDE.md.tmp"
        mv "$CLAUDE_DIR/CLAUDE.md.tmp" "$CLAUDE_DIR/CLAUDE.md"
        chmod 644 "$CLAUDE_DIR/CLAUDE.md"
        # Skills directory (seeded by lifecycle.py from user-env)
        mkdir -p "$CLAUDE_DIR/skills"
      '')

      # Extra bwrap args from wrapper.py (project mounts, dotfiles, passwd).
      (add-runtime ''
        if [ -n "''${CODING_CAVE_BWRAP_ARGS:-}" ] && [ -f "''${CODING_CAVE_BWRAP_ARGS}" ]; then
          while IFS= read -r -d "" arg; do
            RUNTIME_ARGS+=("$arg")
          done < "''${CODING_CAVE_BWRAP_ARGS}"
        fi
      '')

      # Entry: set up /projects, configure GPG signing, save env, then exec
      (wrap-entry (entry: ''
        mkdir -p /projects
        export TMPDIR="$HOME/.cache/tmp"
        mkdir -p "$TMPDIR"
        # User packages from cave.nix
        if [ -n "''${CODING_CAVE_EXTRA_PATH:-}" ]; then
          export PATH="''${CODING_CAVE_EXTRA_PATH}:$PATH"
        fi
        # ~/.local/bin after EXTRA_PATH so mirrored binaries (e.g. host
        # nvim with plugins) shadow the bare cave equivalents.
        export PATH="$HOME/.local/bin:$PATH"
        # GPG commit signing via forwarded agent socket.
        # Create .gnupg dir inside the cave (must be here, not add-runtime,
        # because add-runtime runs on host where /home/cave-coder doesn't exist).
        if [ -n "''${CODING_CAVE_GPG_AGENT_SOCK:-}" ]; then
          mkdir -p "$HOME/.gnupg"
          chmod 700 "$HOME/.gnupg" 2>/dev/null || true
          # Set ultimate trust on the signing key so verification
          # doesn't warn about uncertified signatures.
          _sigkey=$(git config user.signingkey 2>/dev/null || true)
          if [ -n "$_sigkey" ]; then
            echo "$_sigkey:6:" | gpg --import-ownertrust 2>/dev/null || true
          fi
        fi
        # Git identity from forwarded host vars (or fallback).
        if [ -n "''${CODING_CAVE_GIT_NAME:-}" ]; then
          export GIT_AUTHOR_NAME="''${CODING_CAVE_GIT_NAME}"
          export GIT_COMMITTER_NAME="''${CODING_CAVE_GIT_NAME}"
        else
          export GIT_AUTHOR_NAME="coding-cave"
          export GIT_COMMITTER_NAME="coding-cave"
        fi
        if [ -n "''${CODING_CAVE_GIT_EMAIL:-}" ]; then
          export GIT_AUTHOR_EMAIL="''${CODING_CAVE_GIT_EMAIL}"
          export GIT_COMMITTER_EMAIL="''${CODING_CAVE_GIT_EMAIL}"
        else
          export GIT_AUTHOR_EMAIL="coding-cave@noreply"
          export GIT_COMMITTER_EMAIL="coding-cave@noreply"
        fi
        # HTTP proxy for credential injection (crinj)
        if [ -n "''${CODING_CAVE_PROXY_ADDR:-}" ]; then
          export HTTP_PROXY="http://''${CODING_CAVE_PROXY_ADDR}"
          export HTTPS_PROXY="http://''${CODING_CAVE_PROXY_ADDR}"
          export http_proxy="$HTTP_PROXY"
          export https_proxy="$HTTPS_PROXY"
          export NO_PROXY="localhost,127.0.0.1,::1"
          export no_proxy="$NO_PROXY"
          # Trust crinj's MITM CA certificate
          if [ -f /etc/crinj-ca.pem ]; then
            # Combined bundle for tools that use SSL_CERT_FILE (curl, Python, etc.)
            cat /etc/ssl/certs/ca-bundle.crt /etc/crinj-ca.pem > /tmp/combined-ca.pem
            export SSL_CERT_FILE=/tmp/combined-ca.pem
            # Python requests uses certifi by default, ignoring SSL_CERT_FILE
            export REQUESTS_CA_BUNDLE=/tmp/combined-ca.pem
            # NODE_EXTRA_CA_CERTS is additive, only needs the extra cert
            export NODE_EXTRA_CA_CERTS=/etc/crinj-ca.pem
          fi
        fi
        # Clean up setup-only vars before saving the env snapshot.
        unset CODING_CAVE_EXTRA_PATH CODING_CAVE_GIT_NAME \
              CODING_CAVE_GIT_EMAIL CODING_CAVE_GPG_AGENT_SOCK \
              CODING_CAVE_PROXY_ADDR
        export -p > /tmp/cave-env
        chmod 444 /tmp/cave-env
        # Ensure shells started via nsenter get the cave environment.
        # Without this, nsenter inherits the host's env and source
        # cave-env from bash -c is unreliable.
        printf '%s\n' '. /tmp/cave-env' > /etc/zshenv
        printf '%s\n' '. /tmp/cave-env' > /etc/profile
        exec "$@"
      ''))
    ];

    gpuCombinators = with jail.combinators; [
      gpu
      (add-runtime ''
        for dev in /dev/nvidia{0,1,ctl,-uvm,-uvm-tools,-modeset}; do
          [ -e "$dev" ] && RUNTIME_ARGS+=(--dev-bind "$dev" "$dev")
        done
        NVIDIA_SMI="$(command -v nvidia-smi 2>/dev/null || true)"
        if [ -n "$NVIDIA_SMI" ]; then
          NVIDIA_BIN="$(dirname "$(realpath "$NVIDIA_SMI")")"
          RUNTIME_ARGS+=(--ro-bind "$NVIDIA_BIN" "$NVIDIA_BIN")
          RUNTIME_ARGS+=(--symlink "$NVIDIA_BIN/nvidia-smi" /bin/nvidia-smi)
        fi
        RUNTIME_ARGS+=(--setenv LD_LIBRARY_PATH "/run/opengl-driver/lib:${pkgs.stdenv.cc.cc.lib}/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}")
      '')
    ];

    sandbox = jail "coding-cave" pkgs.bashInteractive baseCombinators;
    sandboxGpu = jail "coding-cave" pkgs.bashInteractive (baseCombinators ++ gpuCombinators);

    # Reference individual files to avoid copying a directory to the Nix
    # store (chmod on dirs fails on fuse-overlayfs inside the cave).
    codingCaveState = pkgs.replaceVars ./coding_cave/_state.py {
      SANDBOX_BIN = "${sandbox}/bin/coding-cave";
      SANDBOX_GPU_BIN = "${sandboxGpu}/bin/coding-cave";
      GATEWAY_LOCAL_BIN = "${crinj-bin}/bin/crinj";
      IPC_SETUP_SH = ipcSetup;
      VERSION = version;
    };
    codingCavePkg = pkgs.runCommand "coding-cave-pkg" {} ''
      mkdir -p $out/coding_cave
      cp ${./coding_cave/__init__.py}  $out/coding_cave/__init__.py
      cp ${./coding_cave/__main__.py}  $out/coding_cave/__main__.py
      cp ${./coding_cave/cli.py}       $out/coding_cave/cli.py
      cp ${./coding_cave/commands.py}  $out/coding_cave/commands.py
      cp ${./coding_cave/config.py}    $out/coding_cave/config.py
      cp ${./coding_cave/git.py}       $out/coding_cave/git.py
      cp ${./coding_cave/gitdir.py}    $out/coding_cave/gitdir.py
      cp ${./coding_cave/lifecycle.py} $out/coding_cave/lifecycle.py
      cp ${./coding_cave/overlay.py}   $out/coding_cave/overlay.py
      cp ${./coding_cave/projects.py}  $out/coding_cave/projects.py
      cp ${./coding_cave/sessions.py}  $out/coding_cave/sessions.py
      cp ${./coding_cave/sync.py}      $out/coding_cave/sync.py
      cp ${codingCaveState}            $out/coding_cave/_state.py
    '';

    # -c with explicit sys.path avoids CWD shadowing the built package
    # (python3 -m puts CWD at sys.path[0], which finds unsubstituted
    # source _state.py when run from the project directory).
    baseScript = pkgs.writeText "coding-cave-script" ''
      export PATH="${pkgs.lib.makeBinPath [pkgs.util-linux pkgs.passt pkgs.fuse-overlayfs]}:$PATH"
      exec ${pkgs.python3}/bin/python3 -c '
import sys
sys.path.insert(0, "${codingCavePkg}")
from coding_cave.cli import main
main()
' "$@"
    '';

    wrapper = pkgs.runCommand "coding-cave-with-alias" {} ''
      mkdir -p $out/bin
      cat ${baseScript} > $out/bin/coding-cave
      chmod +x $out/bin/coding-cave
      ln -s coding-cave $out/bin/cav
    '';

  in {
    packages.x86_64-linux = {
      default = wrapper;
      inherit crinj-bin;
    };
  };
}
