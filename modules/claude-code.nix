# coding-cave.claude-code: deep-merge layer over home-manager's
# programs.claude-code.
#
# Adds the bundle, hook list, and CLAUDE.md fragment composition that
# the upstream HM module does not support directly.

{ config, lib, pkgs, ... }:

let
  cfg = config.coding-cave.claude-code;

  jsonFormat = pkgs.formats.json { };

  resolveBundle = import ../lib/resolveBundle.nix { inherit lib; };
  resolvedBundles = map resolveBundle cfg.bundles;

  # Bundle settings merge in declaration order; user extraSettings on top.
  # Strip permissions.allow/deny: caves run --dangerously-skip-permissions,
  # so the host user's deny list would only block legitimate cave commands.
  mergedSettings = let
    raw = lib.foldl' lib.recursiveUpdate { }
      ((map (b: b.settings or { }) resolvedBundles) ++ [ cfg.extraSettings ]);
    perms = raw.permissions or { };
  in raw // lib.optionalAttrs (raw ? permissions) {
    permissions = removeAttrs perms [ "allow" "deny" ];
  };

  # Skills/commands attrsets are merged across bundles. Later bundles win
  # on key collisions.
  mergedSkills = lib.foldl' (acc: b: acc // (b.skills or { })) { } resolvedBundles;
  mergedCommands = lib.foldl' (acc: b: acc // (b.commands or { })) { } resolvedBundles;

  bundlePackages = builtins.concatLists
    (map (b: lib.optional (b ? package && b.package != null) b.package)
      resolvedBundles);

  fragmentText = frag:
    if builtins.isPath frag then builtins.readFile frag
    else frag;
  # The nixpkgs module system reverses listOf merge order (later modules
  # come first). Reverse here so the joined text reads in declaration
  # order: cave-system's fragment first, user fragments after.
  memoryText = lib.concatStringsSep "\n\n"
    (map fragmentText (lib.reverseList cfg.memoryFragments));

in {
  options.coding-cave.claude-code = {
    enable = lib.mkEnableOption "coding-cave Claude Code deep-merge layer";

    bundles = lib.mkOption {
      type = lib.types.listOf lib.types.attrs;
      default = [ ];
      description = ''
        Plugin bundles. Each bundle is an attrset with any of:
        - src:      path to a directory containing skills/ and/or commands/
        - settings: attrset deep-merged into programs.claude-code.settings
        - skills:   attrset of skill entries (overrides anything from src)
        - commands: attrset of command entries (overrides anything from src)
        - package:  derivation added to home.packages

        Bundles merge in declaration order, so later bundles win.
      '';
    };

    plugins = lib.mkOption {
      type = with lib.types; listOf (either package path);
      default = [ ];
      description = ''
        Canonical Claude Code plugin directories, passed through to
        programs.claude-code.plugins. Each entry is either a path to a
        plugin directory or a derivation producing one. The plugin's
        skills, hooks, bin/, and settings are discovered by Claude Code
        itself from the plugin directory layout.
      '';
    };

    hooks = lib.mkOption {
      type = lib.types.attrsOf (lib.types.listOf lib.types.attrs);
      default = { };
      description = ''
        Claude Code hooks per event, concatenated across modules.
        Each event maps to a list of matcher entries, e.g.
          hooks.PreToolUse = [{
            matcher = "Bash";
            hooks = [{ type = "command"; command = "..."; }];
          }];

        Because the type is `listOf`, lists from multiple modules
        concatenate automatically rather than overriding.
      '';
    };

    memoryFragments = lib.mkOption {
      type = lib.types.listOf (lib.types.either lib.types.str lib.types.path);
      default = [ ];
      description = ''
        Fragments concatenated into CLAUDE.md in declaration order.
        Strings are used verbatim; paths are read at evaluation time.
      '';
    };

    extraSettings = lib.mkOption {
      inherit (jsonFormat) type;
      default = { };
      description = ''
        Settings deep-merged on top of bundle settings, before being
        passed to programs.claude-code.settings. The JSON format type
        deep-merges across modules: nested attrsets compose key-by-key
        rather than being replaced wholesale.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    programs.claude-code = {
      enable = true;
      settings = mergedSettings
        // lib.optionalAttrs (cfg.hooks != { }) { hooks = cfg.hooks; };
      skills = mergedSkills;
      commands = mergedCommands;
      plugins = cfg.plugins;
      memory.text = lib.mkIf (cfg.memoryFragments != [ ]) memoryText;
    };

    home.packages = bundlePackages;
  };
}
