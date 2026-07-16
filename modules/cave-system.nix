# cave-system: cave-wide defaults imported by mkCaveEnv ahead of the
# user module. Provides identity, base packages, and the system-level
# Claude Code defaults.

{ config, lib, pkgs, ... }:

{
  # Must match caveUser/caveHome in lib/sandbox.nix.
  home.username = "coder";
  home.homeDirectory = "/home/coder";
  home.stateVersion = "25.05";

  # The cave never runs `home-manager activate`, so HM's news + manual
  # outputs are pure overhead. Manpage generation also tries to chmod
  # store paths, which fails the cave-user-env build.
  manual.manpages.enable = false;
  manual.html.enable = false;
  manual.json.enable = false;
  news.display = "silent";

  # Packages guaranteed in every cave, regardless of the user's cave.nix.
  # A customized cave.nix replaces the user package list, so anything an
  # agent treats as "always there" must live here, not in the default
  # cave.nix. Two groups: cave infrastructure that cav itself relies on,
  # and the standard Unix toolbox agents assume exists.
  home.packages = with pkgs; [
    # cave infrastructure
    coreutils bash nix git gnupg gnugrep gnused jq less
    # standard toolbox agents rely on
    gawk findutils diffutils gnutar gzip xz bzip2 unzip curl ripgrep which
    procps patch file tree
  ];

  coding-cave.claude-code = {
    enable = lib.mkDefault true;
    extraSettings = {
      # Preference defaults: mkOptionDefault (the lowest priority) so any
      # user cave.nix value wins instead of colliding. mkDefault is not
      # enough: a user value at the same mkDefault priority ties rather
      # than overriding, which fails the build with a conflicting-
      # definition error. Dotfiles frameworks routinely wrap values in
      # mkDefault, so the system default must sit strictly below it.
      model = lib.mkOptionDefault "opus";
      reasoning_effort = lib.mkOptionDefault "high";
      statusLine = lib.mkOptionDefault {
        type = "command";
        command = "~/.claude/statusline.sh";
      };
      # Cave requirements stay at normal priority: overriding them
      # should take a deliberate mkForce.
      skipDangerousModePermissionPrompt = true;
      sandbox.enabled = false;
      # Register the cave marketplace (directory source) and enable the
      # cave-messages channel plugin for inter-agent messaging. The
      # marketplace files are written to ~/.cave-marketplace by sandbox.rs
      # at launch time.
      extraKnownMarketplaces.cave = {
        source = {
          source = "directory";
          name = "cave";
          path = "${config.home.homeDirectory}/.cave-marketplace";
        };
      };
      enabledPlugins."cave-messages@cave" = true;
    };
  };

  # Statusline script lives outside HM's normal pipeline because the
  # cave's persistent home needs a real file (not a store symlink).
  # mkCaveEnv's `cp -rL` step dereferences this when materializing files/.
  # mkDefault so a user cave.nix can override the script in place (e.g. to
  # compose extra segments) by setting its own source for this path.
  home.file.".claude/statusline.sh" = {
    source = lib.mkDefault ../defaults/statusline.sh;
    executable = true;
  };
}
