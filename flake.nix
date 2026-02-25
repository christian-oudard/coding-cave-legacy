{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    jail-nix.url = "sourcehut:~alexdavid/jail.nix";
    claude-code = {
      url = "github:sadjow/claude-code-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, jail-nix, claude-code, ... }: let
    pkgs = import nixpkgs { system = "x86_64-linux"; };
    jail = jail-nix.lib.init pkgs;

    defaultClaudeSettings = ./claude-settings.json;

    jailHome = "/home/coding-cave";

    baseCombinators = with jail.combinators; [
      (set-hostname "coding-cave")
      (unsafe-add-raw-args "--symlink /bin/sh /bin/bash")
      (set-env "SHELL" "/bin/bash")
      (set-env "HOME" jailHome)
      (try-fwd-env "CODING_CAVE_HOME_SIZE")
      # Ephemeral home (tmpfs, backed by RAM + swap). Size from config, default 32G.
      (add-runtime ''
        HOME_SIZE="''${CODING_CAVE_HOME_SIZE:-34359738368}"
        RUNTIME_ARGS+=(--size "$HOME_SIZE" --tmpfs ${jailHome})
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
        pkgs.tini
      ])
      (set-env "TERMINFO_DIRS" "${pkgs.ncurses}/share/terminfo")
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
      (try-fwd-env "CODING_CAVE_PROJECTS_MANIFEST")

      # Persist Claude Code state across sandbox runs.
      # Mount project overlays from wrapper-generated script.
      (add-runtime ''
        CAVE_DATA="$HOME/.local/share/coding-cave"
        CLAUDE_STATE="$CAVE_DATA/claude-state"
        CLAUDE_JSON="$CAVE_DATA/claude.json"
        if [ ! -d "$CLAUDE_STATE" ]; then
          mkdir -p "$CLAUDE_STATE"
          cp ${defaultClaudeSettings} "$CLAUDE_STATE/settings.json"
          printf '%s' '{"hasCompletedOnboarding":true,"theme":"dark","effortCalloutDismissed":true}' > "$CLAUDE_JSON"
        fi
        RUNTIME_ARGS+=(--bind "$CLAUDE_STATE" ${jailHome}/.claude)
        RUNTIME_ARGS+=(--bind "$CLAUDE_JSON" ${jailHome}/.claude.json)

        if [ -n "''${CODING_CAVE_CLAUDE_MD:-}" ] && [ -f "''${CODING_CAVE_CLAUDE_MD}" ]; then
          RUNTIME_ARGS+=(--ro-bind "$CODING_CAVE_CLAUDE_MD" ${jailHome}/CLAUDE.md)
        fi

        # Mount project overlays from tab-delimited manifest
        if [ -n "''${CODING_CAVE_PROJECTS_MANIFEST:-}" ] && [ -f "''${CODING_CAVE_PROJECTS_MANIFEST}" ]; then
          while IFS=$'\t' read -r src upper work slug; do
            RUNTIME_ARGS+=(--overlay-src "$src" --overlay "$upper" "$work" "/projects/$slug")
          done < "''${CODING_CAVE_PROJECTS_MANIFEST}"
        fi
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
    };

    wrapper = pkgs.writeShellScriptBin "coding-cave" ''
      export PATH="${pkgs.lib.makeBinPath [pkgs.fuse-overlayfs pkgs.fuse3 pkgs.util-linux]}:$PATH"
      exec ${pkgs.python3}/bin/python3 ${wrapperScript} "$@"
    '';

  in {
    packages.x86_64-linux.default = wrapper;
  };
}
