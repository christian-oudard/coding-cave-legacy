{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    jail-nix.url = "sourcehut:~alexdavid/jail.nix";
    claude-code = {
      url = "github:sadjow/claude-code-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    claude-loop-src = {
      url = "github:christian-oudard/claude-loop";
      flake = false;
    };
  };

  outputs = { self, nixpkgs, jail-nix, claude-code, claude-loop-src, ... }: let
    pkgs = import nixpkgs { system = "x86_64-linux"; };
    jail = jail-nix.lib.init pkgs;

    claude-loop = pkgs.python3Packages.buildPythonApplication {
      pname = "claude-loop";
      version = "0.1.0";
      src = claude-loop-src;
      pyproject = true;
      build-system = [ pkgs.python3Packages.hatchling ];
    };

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
        uv
        claude-loop
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

      # Extra bwrap args file from wrapper.py
      (try-fwd-env "CODING_CAVE_BWRAP_ARGS")

      # First-run init for Claude Code state
      (add-runtime ''
        CAVE_HOME="$HOME/.local/share/coding-cave/home"
        CLAUDE_DIR="$CAVE_HOME/.claude"
        if [ ! -d "$CLAUDE_DIR" ]; then
          mkdir -p "$CLAUDE_DIR"
          cp ${defaultClaudeSettings} "$CLAUDE_DIR/settings.json"
          chmod 644 "$CLAUDE_DIR/settings.json"
          printf '%s' '{"hasCompletedOnboarding":true,"theme":"dark","effortCalloutDismissed":true}' > "$CAVE_HOME/.claude.json"
        fi
      '')

      # Extra bwrap args from wrapper.py (project mounts, dotfiles, passwd)
      (add-runtime ''
        if [ -n "''${CODING_CAVE_BWRAP_ARGS:-}" ] && [ -f "''${CODING_CAVE_BWRAP_ARGS}" ]; then
          exec {EXTRA_ARGS_FD}<"''${CODING_CAVE_BWRAP_ARGS}"
          RUNTIME_ARGS+=(--args "$EXTRA_ARGS_FD")
        fi
      '')

      # Entry: set up /projects, save env for nsenter shells, then exec
      (wrap-entry (entry: ''
        mkdir -p /projects
        export TMPDIR="$HOME/.cache/tmp"
        mkdir -p "$TMPDIR"
        export PATH="$HOME/.local/bin:$PATH"
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
