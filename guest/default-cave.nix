# The cave environment for this guest.
#
# `cave-env edit` created this from a template; `cave-env build` applies it.
# It is one file for the whole guest: every agent gets what it declares, which
# is the design's "It is what agents get and what `cav shell` gets, because
# those are the same environment".
#
# `config` is an ordinary home-manager module. Anything home-manager can do
# here, from a package list to a whole dotfiles framework, and the cave's own
# defaults (model, reasoning effort, statusline) sit underneath at the lowest
# priority so setting one here simply wins.
#
# Three things to know that a home-manager manual will not tell you:
#
#   - Do not set `home.homeDirectory` or interpolate it. This is built once
#     and copied into every agent's home, so it names a directory none of them
#     has. Write `~`, which is expanded where it is used.
#   - Packages that every agent should always have belong in the guest's
#     `environment.systemPackages`, not here. This tier is for the things a
#     person wants and the cave does not require.
#   - `inputs` below becomes flake inputs of the generated wrapper flake, and
#     each one is passed to `config` by name. There is no host filesystem in
#     the guest, so a `path:` input pointing at somewhere on the host does not
#     resolve; use a git URL, which reaches the proxy.
#   - A script you install here and something else executes -- a statusline, a
#     hook -- needs a shebang that exists. `#!/bin/bash` does not: this guest
#     has /bin/sh and /usr/bin/env in /bin and nothing else, so execing such a
#     script fails with "bad interpreter" and exit 127. Rewrite it on the way
#     in, the way the guest already does for cave-git.sh and cave-gpg.py:
#
#       home.file.".claude/statusline.sh".source =
#         pkgs.runCommand "statusline.sh" { } ''
#           substitute ${src}/statusline.sh $out \
#             --replace '#!/bin/bash' '#!${pkgs.bash}/bin/bash'
#           chmod +x $out
#         '';
#
#     Worth knowing because of how it hides: running the script by hand as
#     `bash statusline.sh` bypasses the shebang and works, so the thing that
#     execs it looks broken instead. A statusline that fails this way shows
#     nothing at all and reports nothing anywhere.

{
  # inputs = {
  #   dotfiles.url = "github:you/dotfiles";
  # };

  config = { pkgs, ... }: {
    home.packages = with pkgs; [
      # Whatever you want on every agent's PATH, ahead of the system's.
    ];

    # Claude Code settings. Merged over the cave's defaults, so naming one key
    # here leaves the rest alone.
    coding-cave.claude-code.extraSettings = {
      # model = "opus";
      # reasoning_effort = "high";
    };

    home.sessionVariables = {
      # EDITOR = "vim";
    };
  };
}
