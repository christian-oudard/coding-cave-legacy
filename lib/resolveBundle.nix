# resolveBundle: normalize a bundle attrset for the coding-cave.claude-code
# module.
#
# A bundle may have a `src` directory containing `skills/` and/or
# `commands/` subdirectories. This helper extracts those into HM-shaped
# attrsets that programs.claude-code.{skills,commands} accepts:
#
#   skills.<name>   = src/skills/<name>;     (path to a directory)
#   commands.<name> = src/commands/<name>;   (path to a .md file, name has
#                                             the .md suffix stripped)
#
# Explicit `bundle.skills` and `bundle.commands` override anything found
# under `src`. Other bundle keys (settings, package) pass through unchanged.

{ lib }:

bundle:

let
  src = bundle.src or null;

  readSubdir = subdir:
    let
      path = src + "/${subdir}";
    in
    if src != null && builtins.pathExists path
    then builtins.readDir path
    else { };

  skillsFromSrc =
    let
      entries = readSubdir "skills";
      dirs = lib.filterAttrs (_: type: type == "directory") entries;
    in
    lib.mapAttrs (name: _: src + "/skills/${name}") dirs;

  commandsFromSrc =
    let
      entries = readSubdir "commands";
      mdFiles = lib.filterAttrs
        (name: type: type == "regular" && lib.hasSuffix ".md" name)
        entries;
      stripExt = name: lib.removeSuffix ".md" name;
    in
    lib.mapAttrs' (name: _:
      lib.nameValuePair (stripExt name) (src + "/commands/${name}")
    ) mdFiles;

in
bundle // {
  skills = skillsFromSrc // (bundle.skills or { });
  commands = commandsFromSrc // (bundle.commands or { });
}
