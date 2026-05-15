# cave-system: cave-wide defaults imported by mkCaveEnv ahead of the
# user module. Provides identity, base packages, and the system-level
# Claude Code defaults.

{ config, lib, pkgs, ... }:

{
  home.username = "cave-coder";
  home.homeDirectory = "/home/cave-coder";
  home.stateVersion = "25.05";

  # The cave never runs `home-manager activate`, so HM's news + manual
  # outputs are pure overhead. Manpage generation also tries to chmod
  # store paths, which fails the cave-user-env build.
  manual.manpages.enable = false;
  manual.html.enable = false;
  manual.json.enable = false;
  news.display = "silent";

  # Cave-required packages. The user can add more via home.packages.
  home.packages = with pkgs; [
    coreutils bash nix git gnupg gnugrep gnused jq less
  ];

  coding-cave.claude-code = {
    enable = lib.mkDefault true;
    extraSettings = {
      model = "opus";
      reasoning_effort = "high";
      skipDangerousModePermissionPrompt = true;
      sandbox.enabled = false;
      statusLine = {
        type = "command";
        command = "~/.claude/statusline.sh";
      };
      # Register the cave marketplace (directory source) and enable the
      # cave-messages channel plugin for inter-agent messaging. The
      # marketplace files are written to ~/.cave-marketplace by sandbox.rs
      # at launch time.
      extraKnownMarketplaces.cave = {
        source = {
          source = "directory";
          name = "cave";
          path = "/home/cave-coder/.cave-marketplace";
        };
      };
      enabledPlugins."cave-messages@cave" = true;
    };
    memoryFragments = [ ../prompts/system.md ];
  };

  # Statusline script lives outside HM's normal pipeline because the
  # cave's persistent home needs a real file (not a store symlink).
  # mkCaveEnv's `cp -rL` step dereferences this when materializing files/.
  home.file.".claude/statusline.sh" = {
    source = ../defaults/statusline.sh;
    executable = true;
  };
}
