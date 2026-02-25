{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    jail-nix.url = "sourcehut:~alexdavid/jail.nix";
    claude-code = {
      url = "github:sadjow/claude-code-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, jail-nix, claude-code, ... }: let
    pkgs = import nixpkgs { system = "x86_64-linux"; };
    jail = jail-nix.lib.init pkgs;

    defaultClaudeSettings = ./claude-settings.json;
    version = self.shortRev or self.dirtyShortRev or "dev";

    jailHome = "/home/cave-coder";

    baseCombinators = with jail.combinators; [
      no-die-with-parent
      (set-hostname "coding-cave")
      (unsafe-add-raw-args "--symlink /bin/sh /bin/bash")
      (unsafe-add-raw-args "--symlink ${pkgs.zsh}/bin/zsh /bin/zsh")
      (set-env "SHELL" "/bin/bash")
      (unsafe-add-raw-args "--dev-bind /dev/pts /dev/pts")
      (set-env "CODING_CAVE_VERSION" version)
      (set-env "HOME" jailHome)
      # Persistent home directory (bind mount, survives across sessions).
      (add-runtime ''
        CAVE_HOME="$HOME/.local/share/coding-cave/home"
        mkdir -p "$CAVE_HOME"
        RUNTIME_ARGS+=(--bind "$CAVE_HOME" ${jailHome})
      '')
      network
      # no-new-session omitted: cave init needs its own session to survive terminal close

      # Nix CLI for autonomous package management inside the cave
      # Nix store overlay: writable /nix backed by host store as lower layer
      (try-fwd-env "CODING_CAVE_NIX_UPPER")
      (try-fwd-env "CODING_CAVE_NIX_WORK")
      (add-runtime ''
        if [ -n "''${CODING_CAVE_NIX_UPPER:-}" ]; then
          RUNTIME_ARGS+=(--overlay-src /nix/store --overlay "$CODING_CAVE_NIX_UPPER" "$CODING_CAVE_NIX_WORK" /nix/store)
          RUNTIME_ARGS+=(--tmpfs /nix/var)
        fi
      '')
      (set-env "NIX_CONFIG" "sandbox = false\nexperimental-features = nix-command flakes")

      # Dev tools
      (add-pkg-deps [
        claude-code.packages.x86_64-linux.default
        pkgs.nix
        pkgs.git
        pkgs.ripgrep
        pkgs.fd
        pkgs.jq
        pkgs.curl
        pkgs.gnused
        pkgs.gnugrep
        pkgs.findutils
        pkgs.tree
        pkgs.less
        pkgs.file
        pkgs.ncurses
        pkgs.zsh
        pkgs.eza
        pkgs.nano
        pkgs.tini
      ])
      (set-env "EDITOR" "${pkgs.nano}/bin/nano")
      (set-env "TERMINFO_DIRS" "${pkgs.ncurses}/share/terminfo")
      (set-env "LOCALE_ARCHIVE" "${pkgs.glibcLocales}/lib/locale/locale-archive")
      (set-env "LANG" "en_US.UTF-8")
      (try-fwd-env "COLORTERM")

      # Git identity (throwaway — pull re-authors commits outside the sandbox)
      (set-env "GIT_AUTHOR_NAME" "coding-cave")
      (set-env "GIT_AUTHOR_EMAIL" "coding-cave@noreply")
      (set-env "GIT_COMMITTER_NAME" "coding-cave")
      (set-env "GIT_COMMITTER_EMAIL" "coding-cave@noreply")

      # Credential forwarding
      (try-fwd-env "CLAUDE_CODE_OAUTH_TOKEN")
      (try-fwd-env "ANTHROPIC_API_KEY")
      (try-fwd-env "GITHUB_TOKEN")

      # Cave configuration forwarding
      (try-fwd-env "CODING_CAVE_CLAUDE_MD")
      (try-fwd-env "CODING_CAVE_ZDOTDIR")
      (try-fwd-env "CODING_CAVE_BASHRC")
      (try-fwd-env "CODING_CAVE_BASH_PROFILE")
      (try-fwd-env "CODING_CAVE_PROJECTS_MANIFEST")

      # Initialize Claude Code state in persistent home.
      # Mount project overlays from wrapper-generated script.
      (add-runtime ''
        CAVE_DATA="$HOME/.local/share/coding-cave"
        CAVE_HOME="$CAVE_DATA/home"
        CLAUDE_DIR="$CAVE_HOME/.claude"

        # First-run init
        if [ ! -d "$CLAUDE_DIR" ]; then
          mkdir -p "$CLAUDE_DIR"
          cp ${defaultClaudeSettings} "$CLAUDE_DIR/settings.json"
          printf '%s' '{"hasCompletedOnboarding":true,"theme":"dark","effortCalloutDismissed":true}' > "$CAVE_HOME/.claude.json"
        fi

        # Migrate from old layout (separate claude-state dir + file bind mount)
        if [ -d "$CAVE_DATA/claude-state" ] && [ ! -f "$CLAUDE_DIR/settings.json" ]; then
          cp -a "$CAVE_DATA/claude-state/." "$CLAUDE_DIR/"
        fi
        if [ -f "$CAVE_DATA/claude.json" ] && [ ! -f "$CAVE_HOME/.claude.json" ]; then
          mv "$CAVE_DATA/claude.json" "$CAVE_HOME/.claude.json"
        fi

        if [ -n "''${CODING_CAVE_CLAUDE_MD:-}" ] && [ -f "''${CODING_CAVE_CLAUDE_MD}" ]; then
          RUNTIME_ARGS+=(--ro-bind "$CODING_CAVE_CLAUDE_MD" ${jailHome}/.claude/CLAUDE.md)
        fi

        # Shell dotfiles (zsh) — overlay so .zhistory/.zcompdump are writable
        if [ -n "''${CODING_CAVE_ZDOTDIR:-}" ] && [ -d "''${CODING_CAVE_ZDOTDIR}" ]; then
          mkdir -p "$CAVE_DATA/zdotdir-upper" "$CAVE_DATA/zdotdir-work"
          RUNTIME_ARGS+=(--overlay-src "$CODING_CAVE_ZDOTDIR" --overlay "$CAVE_DATA/zdotdir-upper" "$CAVE_DATA/zdotdir-work" ${jailHome}/.config/zsh)
          RUNTIME_ARGS+=(--setenv ZDOTDIR ${jailHome}/.config/zsh)
        fi
        # Shell dotfiles (bash) — ro-bind individual files
        if [ -n "''${CODING_CAVE_BASHRC:-}" ] && [ -f "''${CODING_CAVE_BASHRC}" ]; then
          RUNTIME_ARGS+=(--ro-bind "$CODING_CAVE_BASHRC" ${jailHome}/.bashrc)
        fi
        if [ -n "''${CODING_CAVE_BASH_PROFILE:-}" ] && [ -f "''${CODING_CAVE_BASH_PROFILE}" ]; then
          RUNTIME_ARGS+=(--ro-bind "$CODING_CAVE_BASH_PROFILE" ${jailHome}/.bash_profile)
        fi

        # Mount projects from JSON manifest. The wrapper pre-mounts
        # fuse-overlayfs at merged/ (handles unmapped GIDs that kernel
        # overlayfs can't copy-up in user namespaces). Fall back to
        # kernel overlayfs if fuse-overlayfs isn't mounted.
        if [ -n "''${CODING_CAVE_PROJECTS_MANIFEST:-}" ] && [ -f "''${CODING_CAVE_PROJECTS_MANIFEST}" ]; then
          while IFS=$'\t' read -r slug src; do
            merged="$CAVE_DATA/projects/$slug/merged"
            gitdir="$CAVE_DATA/projects/$slug/gitdir"
            if mountpoint -q "$merged" 2>/dev/null; then
              RUNTIME_ARGS+=(--bind "$merged" "/projects/$slug")
            else
              upper="$CAVE_DATA/projects/$slug/upper"
              work="$CAVE_DATA/projects/$slug/work"
              mkdir -p "$upper" "$work"
              RUNTIME_ARGS+=(--overlay-src "$src" --overlay "$upper" "$work" "/projects/$slug")
            fi
            if [ -d "$gitdir" ]; then
              RUNTIME_ARGS+=(--bind "$gitdir" "/projects/$slug/.git")
            fi
          done < <(${pkgs.jq}/bin/jq -r 'to_entries[] | [.key, .value] | @tsv' "''${CODING_CAVE_PROJECTS_MANIFEST}")
        fi
      '')

      # Override jail.nix fake-passwd to use cave identity
      (add-runtime ''
        CAVE_PASSWD="$CAVE_DATA/passwd"
        CAVE_GROUP="$CAVE_DATA/group"
        NOLOGIN=${pkgs.shadow}/bin/nologin
        echo "root:x:0:0:System administrator:/root:$NOLOGIN" > "$CAVE_PASSWD"
        echo "cave-coder:x:$(id -u):$(id -g)::${jailHome}:$NOLOGIN" >> "$CAVE_PASSWD"
        echo "root:x:0:" > "$CAVE_GROUP"
        echo "cave-coder:x:$(id -g):" >> "$CAVE_GROUP"
        RUNTIME_ARGS+=(--ro-bind "$CAVE_PASSWD" /etc/passwd)
        RUNTIME_ARGS+=(--ro-bind "$CAVE_GROUP" /etc/group)
      '')

      # Cave init: dump environment for nsenter, reap zombies via tini
      (wrap-entry (entry: ''
        mkdir -p /projects
        export -p > /tmp/cave-env
        exec tini -s -- sleep infinity
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
      '')
    ];

    sandbox = jail "coding-cave" pkgs.bashInteractive baseCombinators;
    sandboxGpu = jail "coding-cave" pkgs.bashInteractive (baseCombinators ++ gpuCombinators);

    wrapperScript = pkgs.replaceVars ./wrapper.py {
      SANDBOX_BIN = "${sandbox}/bin/coding-cave";
      SANDBOX_GPU_BIN = "${sandboxGpu}/bin/coding-cave";
      VERSION = version;
    };

    wrapper = pkgs.writeShellScriptBin "coding-cave" ''
      export PATH="${pkgs.lib.makeBinPath [pkgs.fuse-overlayfs pkgs.fuse3 pkgs.util-linux]}:$PATH"
      exec ${pkgs.python3}/bin/python3 ${wrapperScript} "$@"
    '';

  in {
    packages.x86_64-linux.default = wrapper;
  };
}
