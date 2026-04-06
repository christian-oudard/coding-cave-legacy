# mkCaveEnv: build a cave environment from user configuration.
#
# Called by the generated flake wrapper to produce the cave-user-env
# derivation consumed by the sandbox at runtime.

{ nixpkgs, nix-claude, systemClaudeMd, defaultStatusline }:

let
  lib = nixpkgs.lib;
  systems = [ "x86_64-linux" "aarch64-linux" ];
  mkClaudeConfig = nix-claude.lib.mkClaudeConfig;
  drvName = p: p.pname or (builtins.parseDrvName p.name).name;

in

# config: a function { pkgs } -> { packages, claude, env, files }
{
  claude-code ? null,
  config ? _: {},
}:

let
  mkFor = system:
    let
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfreePredicate = pkg: (lib.getName pkg) == "claude-code";
      };

      userConfig = config { inherit pkgs; };

      requiredPackages = with pkgs; [ coreutils bash nix git gnugrep gnused ];

      defaultPackages = with pkgs; [
        coreutils bash nix git gnupg jq
        gnugrep gnused gawk findutils
        curl wget
      ];

      packages = userConfig.packages or defaultPackages;
      claude = userConfig.claude or {};
      env = userConfig.env or {};
      files = userConfig.files or {};

      # Always include required packages; user packages take precedence
      userPkgNames = map drvName packages;
      extraRequired = builtins.filter (p:
        !builtins.elem (drvName p) userPkgNames
      ) requiredPackages;

      validatedPackages = packages ++ extraRequired;

      claude-code-pkg =
        if claude-code == null then
          throw "cave.nix must provide claude-code. Add claude-code-nix as a flake input and pass it as claude-code."
        else
          claude-code.packages.${system}.default;

      allPackages = [ claude-code-pkg ] ++ validatedPackages;

      claudePlugins = claude.plugins or [];
      claudeSettings = claude.settings or {};
      claudeMdExtra = claude.claudeMd or null;

      systemClaudeSettings = {
        model = "opus";
        reasoning_effort = "high";
        skipDangerousModePermissionPrompt = true;
        sandbox.enabled = false;
        statusLine = {
          type = "command";
          command = "~/.claude/statusline.sh";
        };
      };

      claudeConfig = mkClaudeConfig {
        inherit pkgs;
        skipOnboarding = true;
        plugins = claudePlugins;
        settings = systemClaudeSettings // claudeSettings;
        memory.fragments =
          [ systemClaudeMd ]
          ++ lib.optional (claudeMdExtra != null) claudeMdExtra;
      };

      finalPackages = allPackages ++ (claudeConfig.packages or []);

    in pkgs.runCommand "cave-user-env" {} ''
      mkdir -p $out/packages

      ${lib.concatMapStringsSep "\n" (p:
        let name = drvName p; in
        ''
          if [ -d "${p}/bin" ]; then
            ln -s ${p} $out/packages/${name}
          fi
        ''
      ) finalPackages}

      ln -s ${claudeConfig} $out/claude

      cat > $out/env.sh << 'ENVEOF'
      ${lib.concatStringsSep "\n" (lib.mapAttrsToList (k: v:
        "export ${k}=\"${v}\""
      ) env)}
      ENVEOF

      ${lib.concatStringsSep "\n" (lib.mapAttrsToList (relPath: content:
        let
          safeName = builtins.replaceStrings ["/"] ["-"] relPath;
          fileStore = pkgs.writeText "cave-file-${safeName}" content;
        in ''
          mkdir -p "$out/files/$(dirname "${relPath}")"
          cp ${fileStore} "$out/files/${relPath}"
        ''
      ) files)}
    '';

in {
  packages = lib.genAttrs systems (system: {
    default = mkFor system;
  });
}
