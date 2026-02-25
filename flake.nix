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

    sandbox = jail "codingcave" pkgs.bashInteractive (with jail.combinators; [
      (set-hostname "coding-cave")
      network
      no-new-session

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
      ])

      # Credential forwarding
      (try-fwd-env "CLAUDE_CODE_OAUTH_TOKEN")
      (try-fwd-env "ANTHROPIC_API_KEY")
      (try-fwd-env "GITHUB_TOKEN")
      (try-fwd-env "GIT_AUTHOR_NAME")
      (try-fwd-env "GIT_AUTHOR_EMAIL")
      (try-fwd-env "GIT_COMMITTER_NAME")
      (try-fwd-env "GIT_COMMITTER_EMAIL")

      # Workspace and task forwarding
      (try-fwd-env "CODING_CAVE_WORKSPACE")
      (try-fwd-env "CODING_CAVE_TASK")

      # Persist Claude Code state across sandbox runs.
      # Bind workspace directory if set.
      (add-runtime ''
        CAVE_DATA="$HOME/.local/share/coding-cave"
        CLAUDE_STATE="$CAVE_DATA/claude-state"
        CLAUDE_JSON="$CAVE_DATA/claude.json"
        if [ ! -d "$CLAUDE_STATE" ]; then
          mkdir -p "$CLAUDE_STATE"
          cp ${defaultClaudeSettings} "$CLAUDE_STATE/settings.json"
          printf '%s' '{"hasCompletedOnboarding":true,"theme":"dark","effortCalloutDismissed":true}' > "$CLAUDE_JSON"
        fi
        RUNTIME_ARGS+=(--bind "$CLAUDE_STATE" "$HOME/.claude")
        RUNTIME_ARGS+=(--bind "$CLAUDE_JSON" "$HOME/.claude.json")

        if [ -n "''${CODING_CAVE_WORKSPACE:-}" ] && [ -d "''${CODING_CAVE_WORKSPACE:-}" ]; then
          RUNTIME_ARGS+=(--bind "$CODING_CAVE_WORKSPACE" "$CODING_CAVE_WORKSPACE")
        fi
      '')

      # Forward CODING_CAVE_SHELL to control entry mode
      (try-fwd-env "CODING_CAVE_SHELL")

      # Inside the jail: skip onboarding, cd to workspace, launch claude or shell
      (wrap-entry (entry: ''
        if [ -n "''${CODING_CAVE_WORKSPACE:-}" ]; then
          cd "$CODING_CAVE_WORKSPACE"
        fi
        if [ -n "''${CODING_CAVE_SHELL:-}" ]; then
          ${entry}
        elif [ -n "''${CODING_CAVE_TASK:-}" ]; then
          exec claude --dangerously-skip-permissions -p "$CODING_CAVE_TASK"
        else
          exec claude --dangerously-skip-permissions
        fi
      ''))
    ]);

    wrapperScript = pkgs.replaceVars ./wrapper.py {
      SANDBOX_BIN = "${sandbox}/bin/codingcave";
    };

    wrapper = pkgs.writeShellScriptBin "codingcave" ''
      exec ${pkgs.python3}/bin/python3 ${wrapperScript} "$@"
    '';

  in {
    packages.x86_64-linux.default = wrapper;
  };
}
