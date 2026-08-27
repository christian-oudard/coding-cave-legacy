# cave-system: cave-wide defaults imported by mkCaveEnv ahead of the user
# module. Provides identity and the Claude Code defaults every agent gets.
#
# Ported from `main`'s modules/cave-system.nix, minus the two things the guest
# already owns. Both deletions are the design's own instruction:
# "environment.systemPackages is the simpler default for anything that is not
# per-user dotfiles" (docs/vm-cave-design.md).
#
#   - **No toolbox.** `main` lists coreutils, git, jq, ripgrep and the rest
#     here because cav runs on whatever distro the host is and cannot
#     configure a system it does not own, so the user env was the only tier
#     there was. The guest owns its system, so that floor lives in
#     environment.systemPackages, where every agent gets it whether or not the
#     user env has ever been built. Adding a package here instead would give
#     an agent a second coreutils on PATH ahead of the system's.
#   - **No timeZone option.** `main` needs it because the sandbox pins
#     /etc/localtime to UTC and $TZ is the only lever left. The guest has
#     time.timeZone, which is the real setting.

{ config, lib, pkgs, ... }:

{
  config = {
    # A home no agent has, and that is deliberate: this env is built once and
    # materialized into every agent's home (see mkCaveEnv.nix). home-manager
    # requires the option to be set, so it is set to something that will look
    # obviously wrong if it ever leaks into a rendered file -- which is the
    # failure mode to catch, since /home/cave exists nowhere in the guest.
    home.username = "cave";
    home.homeDirectory = "/home/cave";
    home.stateVersion = "25.05";

    # The cave never runs `home-manager activate`, so HM's news + manual
    # outputs are pure overhead. Manpage generation also tries to chmod store
    # paths, which fails the cave-user-env build.
    manual.manpages.enable = false;
    manual.html.enable = false;
    manual.json.enable = false;
    news.display = "silent";

    coding-cave.claude-code = {
      enable = lib.mkDefault true;
      extraSettings = {
        # Preference defaults: mkOptionDefault (the lowest priority) so any
        # user cave.nix value wins instead of colliding. mkDefault is not
        # enough: a user value at the same mkDefault priority ties rather than
        # overriding, which fails the build with a conflicting-definition
        # error. Dotfiles frameworks routinely wrap values in mkDefault, so the
        # system default must sit strictly below it.
        model = lib.mkOptionDefault "opus";
        reasoning_effort = lib.mkOptionDefault "high";
        statusLine = lib.mkOptionDefault {
          type = "command";
          command = "~/.claude/statusline.sh";
        };

        # Cave requirements stay at normal priority: overriding one should take
        # a deliberate mkForce.
        #
        # sandbox.enabled = false because the VM is the sandbox. claude's own
        # sandboxing would be a second, weaker one nested inside it, and its
        # failure modes (a seccomp filter it cannot install, a bind it cannot
        # make) read as tool bugs rather than as policy.
        sandbox.enabled = false;
        # Also asserted as policy in environment.etc."claude-code/
        # managed-settings.json", which is the tier an agent cannot rewrite.
        # Stated in both places on purpose: the managed file is what holds when
        # an agent edits its own settings.json, and this is what holds when the
        # user env is the only thing that has been built.
        skipDangerousModePermissionPrompt = true;
      };
    };

    # Statusline script. A real file rather than a store symlink for the same
    # reason everything else here is (mkCaveEnv.nix): the tree is materialized
    # writable. mkDefault so a user cave.nix can override the script in place
    # by setting its own source for this path.
    home.file.".claude/statusline.sh" = {
      source = lib.mkDefault ../defaults/statusline.sh;
      executable = true;
    };
  };
}
