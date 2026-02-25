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

    claudeSettings = pkgs.writeText "settings.json" (builtins.toJSON {
      theme = "dark";
      syntaxTheme = "Monokai Extended";
      enableSyntaxHighlighting = true;
    });

    sandbox = jail "codingcave" pkgs.bashInteractive (with jail.combinators; [
      (set-hostname "coding-cave")
      network
      no-new-session
      (try-fwd-env "CLAUDE_CODE_OAUTH_TOKEN")
      (add-pkg-deps [ claude-code.packages.x86_64-linux.default ])
      (add-runtime "mkdir -p ~/.claude")
      (write-text "/home/christian/.claude/settings.json" (builtins.readFile claudeSettings))
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
