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
      (add-pkg-deps (with pkgs; [
        claude-code.packages.x86_64-linux.default
        nix
        git
        github-cli
        ripgrep
        fd
        jq
        python3
        curl
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
        perl
        tree
        less
        file
        ncurses
        which
        zsh
        eza
        nano
      ]))
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

        # Shell dotfiles (zsh) — ro-bind config files, writable dir for history/compdump.
        # Files matching *_private* are excluded (may contain secrets).
        if [ -n "''${CODING_CAVE_ZDOTDIR:-}" ] && [ -d "''${CODING_CAVE_ZDOTDIR}" ]; then
          zsh_cave="${jailHome}/.config/zsh"
          mkdir -p "$CAVE_DATA/zdotdir"
          RUNTIME_ARGS+=(--bind "$CAVE_DATA/zdotdir" "$zsh_cave")
          for f in "$CODING_CAVE_ZDOTDIR"/.z*; do
            [ -f "$f" ] || continue
            name="$(basename "$f")"
            case "$name" in *_private*|*secret*) continue ;; esac
            case "$name" in
              .zhistory|.zcompdump*)
                # Writable: copy to persistent dir if not present, then bind
                [ -f "$CAVE_DATA/zdotdir/$name" ] || cp "$f" "$CAVE_DATA/zdotdir/$name"
                ;;
              *)
                # Read-only config
                RUNTIME_ARGS+=(--ro-bind "$f" "$zsh_cave/$name")
                ;;
            esac
          done
          RUNTIME_ARGS+=(--setenv ZDOTDIR "$zsh_cave")
        fi
        # Shell dotfiles (bash) — ro-bind individual files
        if [ -n "''${CODING_CAVE_BASHRC:-}" ] && [ -f "''${CODING_CAVE_BASHRC}" ]; then
          RUNTIME_ARGS+=(--ro-bind "$CODING_CAVE_BASHRC" ${jailHome}/.bashrc)
        fi
        if [ -n "''${CODING_CAVE_BASH_PROFILE:-}" ] && [ -f "''${CODING_CAVE_BASH_PROFILE}" ]; then
          RUNTIME_ARGS+=(--ro-bind "$CODING_CAVE_BASH_PROFILE" ${jailHome}/.bash_profile)
        fi

        # Mount projects from JSON manifest. The wrapper pre-mounts
        # fuse-overlayfs at per-session merged dirs. Manifest format:
        # {slug: {source: path, merged: path}}
        if [ -n "''${CODING_CAVE_PROJECTS_MANIFEST:-}" ] && [ -f "''${CODING_CAVE_PROJECTS_MANIFEST}" ]; then
          while IFS=$'\t' read -r slug src merged; do
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
            else
              echo "FATAL: no gitdir for project '$slug' at $gitdir" >&2
              exit 1
            fi
          done < <(${pkgs.jq}/bin/jq -r 'to_entries[] | [.key, .value.source, .value.merged] | @tsv' "''${CODING_CAVE_PROJECTS_MANIFEST}")
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

      # Entry: set up /projects, save env for nsenter shells, then exec
      (wrap-entry (entry: ''
        mkdir -p /projects
        export -p > /tmp/cave-env
        chmod 444 /tmp/cave-env
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
      '')
    ];

    sandbox = jail "coding-cave" pkgs.bashInteractive baseCombinators;
    sandboxGpu = jail "coding-cave" pkgs.bashInteractive (baseCombinators ++ gpuCombinators);

    wrapperScript = pkgs.replaceVars ./wrapper.py {
      SANDBOX_BIN = "${sandbox}/bin/coding-cave";
      SANDBOX_GPU_BIN = "${sandboxGpu}/bin/coding-cave";
      VERSION = version;
    };

    baseScript = pkgs.writeText "coding-cave-script" ''
      export PATH="${pkgs.lib.makeBinPath [pkgs.fuse-overlayfs pkgs.fuse3 pkgs.util-linux]}:$PATH"
      exec ${pkgs.python3}/bin/python3 ${wrapperScript} "$@"
    '';

    wrapper = pkgs.runCommand "coding-cave-with-alias" {} ''
      mkdir -p $out/bin
      cat ${baseScript} > $out/bin/coding-cave
      chmod +x $out/bin/coding-cave
      ln -s coding-cave $out/bin/cav
    '';

  in {
    packages.x86_64-linux.default = wrapper;
  };
}
