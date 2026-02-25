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

    sandbox = jail "coding-cave" pkgs.bashInteractive (with jail.combinators; [
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
      no-new-session
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

      # Dev tools
      (add-pkg-deps [
        claude-code.packages.x86_64-linux.default
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

      # Task forwarding (used by wrap-entry inside the jail)
      (try-fwd-env "CODING_CAVE_TASK")
      # CODING_CAVE_WORKSPACE, OVERLAY_UPPER, OVERLAY_WORK are read by
      # add-runtime on the host side only — not forwarded into the jail.

      # Persist Claude Code state across sandbox runs.
      # Mount workspace as overlay (project is read-only lower layer).
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

        if [ -n "''${CODING_CAVE_OVERLAY_UPPER:-}" ]; then
          RUNTIME_ARGS+=(--overlay-src "$CODING_CAVE_WORKSPACE" --overlay "$CODING_CAVE_OVERLAY_UPPER" "$CODING_CAVE_OVERLAY_WORK" /workspace)
        elif [ -n "''${CODING_CAVE_WORKSPACE:-}" ] && [ -d "''${CODING_CAVE_WORKSPACE:-}" ]; then
          RUNTIME_ARGS+=(--bind "$CODING_CAVE_WORKSPACE" /workspace)
        fi
      '')

      # Inside the jail: cd to workspace, launch claude
      (wrap-entry (entry: ''
        if [ -d /workspace ]; then
          cd /workspace
        fi
        if [ -n "''${CODING_CAVE_TASK:-}" ]; then
          exec claude --dangerously-skip-permissions -p "$CODING_CAVE_TASK"
        else
          exec claude --dangerously-skip-permissions
        fi
      ''))
    ]);

    wrapperScript = pkgs.replaceVars ./wrapper.py {
      SANDBOX_BIN = "${sandbox}/bin/coding-cave";
    };

    wrapper = pkgs.writeShellScriptBin "coding-cave" ''
      export PATH="${pkgs.lib.makeBinPath [pkgs.fuse-overlayfs pkgs.fuse3]}:$PATH"
      exec ${pkgs.python3}/bin/python3 ${wrapperScript} "$@"
    '';

  in {
    packages.x86_64-linux.default = wrapper;
  };
}
