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

  # Auto-derive enabledPlugins from every registered plugin. Once any
  # enabledPlugins entry exists in settings.json, Claude Code treats the key
  # as an explicit allow-list and silently disables every plugin not listed,
  # including --plugin-dir entries. Reading each plugin's
  # .claude-plugin/plugin.json and adding its name keeps them active when
  # bundles or extraSettings contribute their own entries (e.g. marketplace
  # plugins like cave-messages@cave). Source the final merged plugin list, not
  # just cfg.plugins, so plugins another module registers (e.g. via an imported
  # home-manager module) are enabled too. Acyclic: plugin definitions never
  # depend on settings/enabledPlugins.
  readPluginName = p:
    let manifest = p + "/.claude-plugin/plugin.json";
    in if builtins.pathExists manifest
       then [ (lib.importJSON manifest).name ]
       else [ ];
  autoEnabledPlugins = lib.listToAttrs
    (map (n: lib.nameValuePair n true)
      (lib.concatMap readPluginName config.programs.claude-code.plugins));

  # Bundle settings merge in declaration order; user extraSettings on top.
  # Strip permissions.allow/deny: caves run --dangerously-skip-permissions,
  # so the host user's deny list would only block legitimate cave commands.
  # Force CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="" so the privacy level
  # is "default", which is the only level that lets the channel feature
  # gate refresh. Telemetry is blocked at the proxy (crinj) layer.
  mergedSettings = let
    raw = lib.foldl' lib.recursiveUpdate { }
      ((map (b: b.settings or { }) resolvedBundles) ++ [ cfg.extraSettings ]);
    perms = raw.permissions or { };
    env = raw.env or { };
    combinedEnabled = autoEnabledPlugins // (raw.enabledPlugins or { });
  in raw // lib.optionalAttrs (raw ? permissions) {
    permissions = removeAttrs perms [ "allow" "deny" ];
  } // {
    env = env // { CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = ""; };
  } // lib.optionalAttrs (combinedEnabled != { }) {
    enabledPlugins = combinedEnabled;
  };

  # Skills/commands attrsets are merged across bundles. Later bundles win
  # on key collisions.
  mergedSkills = lib.foldl' (acc: b: acc // (b.skills or { })) { } resolvedBundles;
  mergedCommands = lib.foldl' (acc: b: acc // (b.commands or { })) { } resolvedBundles;

  bundlePackages = builtins.concatLists
    (map (b: lib.optional (b ? package && b.package != null) b.package)
      resolvedBundles);

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
    };

    # Plugins passed as built derivations (not path strings) get their bin/ on
    # PATH, so a plugin that ships a CLI (e.g. persist) is callable by name from
    # a terminal or a custom hook without a separate home.packages entry.
    home.packages = bundlePackages
      ++ builtins.filter lib.isDerivation cfg.plugins;
  };
}
