# Home-Manager Conversion Plan

Convert coding-cave's user environment from a custom builder (`mkCaveEnv` + `nix-claude.mkClaudeConfig`) to a standard home-manager evaluation. The cave's persistent home becomes a regular HM-managed environment, with a thin coding-cave-owned module that adds the deep-merge semantics HM lacks.

## Goal

- One way to configure Claude Code: `programs.claude-code.*` from upstream HM, plus `coding-cave.claude-code.*` for compositional extensions.
- One way to configure the cave's home environment: standard HM modules (`home.packages`, `home.sessionVariables`, `home.file`, `programs.*`).
- The same `cave.nix` config can be imported on the host as an HM module — same shape, same semantics.
- Retire the `nix-claude` flake entirely.

## Why

1. **Standardization.** HM is the standard. Anyone who knows HM can read `cave.nix`. Anyone who needs git/direnv/zsh/neovim config in the cave gets it from HM modules instead of bespoke `files`/`env` attrsets.
2. **Upstream tracking.** When HM's `programs.claude-code` adds new options (agents, rules, output styles, LSP), the cave gets them automatically.
3. **Less code to maintain.** `mkClaudeConfig` (~190 lines), the nix-claude HM module (~95 lines), the option declarations (~100 lines), and the gen-cave.nix codegen all go away. Replaced by ~50-100 lines of coding-cave module + a thinner `mkCaveEnv`.
4. **Unified host/cave story.** The host already uses HM. The cave should too. Then `claude.nix` can be imported by both targets without translation.

## Performance baseline

Measured on this host (see `/tmp/cave-perf-test/` if it still exists):

- HM eval: 0.56s vs current 0.28s. Both well under a second.
- Output on disk: 84 KiB vs 48 KiB. Negligible.
- Closure: 222 MiB total, but only 12 paths and ~140 KiB are HM-specific. Everything else (glibc-locales, systemd, glib, bash, etc.) is on every NixOS host and the cave overlay sees it for free.
- Cold download (no host overlay): ~200-300 MB one-time, ~40-50s at 50 Mbps.
- HM eval works with `home.username = "cave-coder"` even when the user does not exist on the build host. No fake-user-bwrap workaround needed.
- `minimal = true` does not help: same output, same eval time, more API friction.

Conclusion: HM is performant enough for the cave. No reason to avoid it on cost grounds.

## Target architecture

```
coding-cave/
├── lib/
│   ├── mkCaveEnv.nix         # rewritten: evaluates HM, extracts outputs
│   └── resolveBundle.nix     # bundle convention helper
├── modules/
│   ├── claude-code.nix       # the coding-cave.claude-code.* module
│   └── cave-system.nix       # cave defaults (model, statusline, identity)
└── flake.nix                 # exports homeModules, lib.mkCaveEnv
```

`nix-claude` is removed from coding-cave's flake inputs and from dotfiles.

## The `coding-cave.claude-code` module

Lives at `coding-cave/modules/claude-code.nix`. Declares the deep-merge-friendly options that HM does not, then feeds the merged result into HM's native `programs.claude-code`.

Option namespace:

```
coding-cave.claude-code = {
  enable             = bool;                   # gates the whole layer
  bundles            = listOf attrs;           # plugin bundles ({ src, settings, package, skills, commands })
  hooks              = attrsOf (listOf attrs); # hook lists per event, concatenated across modules
  memoryFragments    = listOf (either str path); # CLAUDE.md fragments, concatenated in declaration order
  extraSettings      = attrs;                  # deep-merged on top of bundle settings
};
```

Why these types matter:
- `attrsOf (listOf attrs)` for hooks: the module system concatenates `listOf` values across modules automatically. System defaults and user config can both contribute to `hooks.Stop = [...]` without conflicting. This is the deep-merge feature HM lacks.
- `listOf (either str path)` for memory fragments: same story for CLAUDE.md composition.
- `bundles` is a plain `listOf attrs` because each bundle is a heterogeneous attrset, not an HM-style submodule.

What the module does internally:
1. For each bundle: if `src` is a directory with `skills/` or `commands/` subdirs, extract them into HM-shaped attrsets (`skills.<name> = src/skills/<name>;`).
2. Deep-merge bundle settings using `lib.recursiveUpdate`, with bundle order = declaration order, user `extraSettings` on top.
3. Set `programs.claude-code.settings`, `programs.claude-code.skills`, `programs.claude-code.commands`, `programs.claude-code.memory.text` from the merged result.
4. Add bundle packages to `home.packages`.

The module always sets `programs.claude-code.settings.hooks = cfg.hooks` so that hook lists from all modules are visible in the final settings.json.

## The `cave-system` module

Lives at `coding-cave/modules/cave-system.nix`. Provides cave-wide defaults that every cave gets automatically. Imported by `mkCaveEnv` ahead of the user module.

What it sets:

```nix
{
  # cave-coder identity for HM eval
  home.username      = "cave-coder";
  home.homeDirectory = "/home/cave-coder";
  home.stateVersion  = "25.05";

  # cave's claude defaults
  coding-cave.claude-code = {
    enable = true;
    extraSettings = {
      model = "opus";
      reasoning_effort = "high";
      skipDangerousModePermissionPrompt = true;
      sandbox.enabled = false;
      statusLine = { type = "command"; command = "~/.claude/statusline.sh"; };
    };
    memoryFragments = [ ../defaults/system-claude.md ];
  };

  # statusline script
  home.file.".claude/statusline.sh" = {
    source = ../defaults/statusline.sh;
    executable = true;
  };

  # base packages every cave needs
  home.packages = with pkgs; [ coreutils bash nix git gnupg gnugrep gnused jq ];

  programs.claude-code.enable = true;
  # programs.claude-code.package set later from the claude-code flake input
}
```

## `mkCaveEnv` rewrite

`lib/mkCaveEnv.nix` becomes a thin wrapper around `home-manager.lib.homeManagerConfiguration`:

```nix
{ nixpkgs, home-manager }:
{ claude-code, userModule }:
let
  systems = [ "x86_64-linux" "aarch64-linux" ];
  mkFor = system:
    let
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfreePredicate = pkg: (pkgs.lib.getName pkg) == "claude-code";
      };

      hmEval = home-manager.lib.homeManagerConfiguration {
        inherit pkgs;
        modules = [
          ../modules/cave-system.nix
          ../modules/claude-code.nix
          { programs.claude-code.package = claude-code.packages.${system}.default; }
          userModule
        ];
      };

      sessionVarsFile = pkgs.writeText "env.sh" (
        pkgs.lib.concatStringsSep "\n" (pkgs.lib.mapAttrsToList
          (k: v: ''export ${k}="${toString v}"'')
          hmEval.config.home.sessionVariables)
      );
    in
    pkgs.runCommand "cave-user-env" {} ''
      mkdir -p $out
      cp -rL ${hmEval.config.home-files}/. $out/files/
      ln -s ${hmEval.config.home.path} $out/home-path
      cp ${sessionVarsFile} $out/env.sh
    '';
in {
  packages = pkgs.lib.genAttrs systems (s: { default = mkFor s; });
}
```

The output is similar to what `mkCaveEnv` produces today: a derivation with `files/`, `home-path/`, and `env.sh`. The sandbox entry script consumes it the same way.

Two key choices:
- **`cp -rL`** dereferences HM's symlinks into real files. The cave's persistent home survives across sessions but the nix store overlay does not, so symlinks would dangle. Real copies are required.
- **`home-path` is symlinked**, not copied. It points into the merged store, where it stays valid as long as the cave session runs. PATH gets `${USER_ENV}/home-path/bin`.

## `cave.nix` format

User cave config becomes a standard HM module:

```nix
{
  inputs = {
    persist = { url = "github:christian-oudard/persist"; flake = false; };
  };

  config = { pkgs, persist, ... }: {
    home.packages = with pkgs; [
      tree eza nano direnv nix-direnv zsh neovim diff-so-fancy
      python3 python3Packages.pytest uv
      ghc cabal-install stack
      nodejs typescript
      cargo rustc rustfmt clippy
    ];

    coding-cave.claude-code = {
      bundles = [
        { src = persist; }
      ];
      hooks.Stop = [{
        hooks = [{ type = "command"; command = "printf '\\a' > /proc/$PPID/fd/1"; }];
      }];
      memoryFragments = [ ./extra-rules.md ];
      extraSettings.effortLevel = "high";
    };

    home.sessionVariables.EDITOR = "nvim";

    home.file.".config/direnv/direnvrc".text =
      "source ${pkgs.nix-direnv}/share/nix-direnv/direnvrc";
  };
}
```

This is recognizable to anyone who has written an HM config. No bespoke `claude`/`packages`/`env`/`files` sub-attrsets.

## persist as a normal bundle

Today persist exports `plugin.${system}` per the nix-claude flake convention. Convert it to a standard bundle layout: a directory with `skills/`, `commands/`, and a `bundle.nix` (or just a Nix function) that returns `{ src, settings, package }`.

```nix
# persist/default.nix
{ pkgs }: {
  src = ./bundle;            # contains skills/persist/SKILL.md, commands/persist.md
  package = pkgs.callPackage ./package.nix {};
  settings = {};
}
```

Used as: `bundles = [ (import persist { inherit pkgs; }) ]`. No special flake convention, no `plugin.${system}` wrapper.

The `claude-plugins-official` and `agent-capabilities` plugins are already plain directories — they slot in as `bundles = [ { src = "${claude-plugins-official}/plugins/commit-commands"; } ];` with no wrapper at all.

## Sandbox entry script changes

Most of the pre-bwrap claude-config seeding logic in `flake.nix` lines 115-168 goes away. Currently it:
1. Merges system + user-env settings.json
2. Concatenates system + user-env + user-global CLAUDE.md
3. Copies skills and commands from user-env
4. Updates statusline.sh

After conversion, all of this happens at HM eval time. The user-env's `files/` directory already contains a fully-baked `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, `~/.claude/skills/`, `~/.claude/commands/`, and `~/.claude/statusline.sh`.

The pre-bwrap script reduces to:
- Copy `${USER_ENV}/files/` into `${CODING_CAVE_HOME}/`
- Seed `.claude.json` for `skipOnboarding` (HM doesn't manage this mutable file)
- Copy `.gitconfig` if provided

The inside-bwrap entry script reduces to:
- Add `${USER_ENV}/home-path/bin` to PATH
- Source `${USER_ENV}/env.sh`
- Existing git wrapper, GPG, proxy, project cd, exec

The user-global `CODING_CAVE_USER_CLAUDE_MD` append-to-CLAUDE.md feature stays in the entry script — the host's CLAUDE.md is per-session, not part of the cave build.

## Sharing `claude.nix` between host and cave

Today `nixos-config/claude.nix` exports an HM module for the host plus data for `gen-cave.nix` to serialize into `cave.nix`. The serialization is fragile.

Replace with a shared-import scheme:
1. Chezmoi puts `claude.nix` (an HM module) in a known location, e.g. `~/.config/coding-cave/claude.nix`.
2. The user's `cave.nix` imports it: `imports = [ ./claude.nix ];`.
3. `user_env.py` already copies `cave.nix` into the build directory; extend it to copy any `*.nix` siblings too, so the relative import resolves.
4. The host's `home.nix` imports the same file directly.

`gen-cave.nix` is deleted. The serialization codegen problem disappears.

## Migration steps

Order matters: the cave side should be working before the host side switches over, so that nix-claude can be removed cleanly.

1. **Add `home-manager` flake input** to `coding-cave/flake.nix`.
2. **Write `coding-cave/modules/claude-code.nix`** (the deep-merge layer).
3. **Write `coding-cave/lib/resolveBundle.nix`** (extract skills/commands from `src` directories).
4. **Write `coding-cave/modules/cave-system.nix`** (cave defaults, identity, statusline).
5. **Rewrite `coding-cave/lib/mkCaveEnv.nix`** to evaluate HM and extract outputs.
6. **Update `coding-cave/cav/user_env.py`**: stop generating the nix-claude wrapper, generate the HM-flavored wrapper instead. Copy sibling `*.nix` files alongside `cave.nix`.
7. **Simplify the sandbox entry blocks** in `coding-cave/flake.nix` (drop the merge logic, keep `.claude.json` seeding and gitconfig).
8. **Add tests**: build a fixture cave config, verify that hooks from system and user merge, verify that `home-files` ends up in the output.
9. **Convert `persist`**: drop the `plugin.${system}` wrapper, expose the standard bundle shape.
10. **Update `dotfiles/nixos-config/claude.nix`**: rewrite as an HM module that uses `coding-cave.claude-code.*`. Remove the `module` attribute, just be a module.
11. **Update `dotfiles/nixos-config/home.nix`**: import `coding-cave.homeModules.claudeCode` (the module), drop `nix-claude.homeModules.default`.
12. **Replace `dotfiles/nixos-config/gen-cave.nix`**: chezmoi-manage `~/.config/coding-cave/claude.nix` as a real file. The cave.nix imports it.
13. **Remove `nix-claude`** from both flakes' inputs and lock files.
14. **Archive the `nix-claude` repo** with a README pointing at coding-cave.

## Open questions and risks

- **HM module evaluation in the cave wants a real user identity for some checks.** Verified in the perf test: pure HM eval works with `cave-coder` even when that user does not exist on the build host. No workaround needed. The runtime sanity checks (USER/HOME match) only run during `home-manager activate`, which the cave never does.
- **Activation scripts in HM modules.** Some modules (e.g. `programs.git`) generate `home.activation.*` entries. We never run activation, so these are harmless — they live in the unused `activationPackage` derivation. Worth confirming during the test phase that no module errors out at *eval* time because of activation script content.
- **`programs.claude-code.package` vs sadjow's claude-code-nix.** Compatible. The package option accepts any derivation. The dotfiles overlay continues to work the same way for the host; for the cave, `mkCaveEnv` injects it explicitly.
- **`extraSettings` vs raw `programs.claude-code.settings`.** Both are allowed. Merge order is: bundle settings → `extraSettings` → raw `programs.claude-code.settings` (HM handles the last via `recursiveUpdate` semantics on its JSON type). Document clearly so users know which knob to use.
- **`nix-claude` is currently in dotfiles' lockfile.** Removing it will dirty the lockfile. Needs a one-shot rebuild and re-deploy. Coordinate so nothing breaks during the transition.
- **glibc-locales download.** First-time cave build on a fresh host pulls ~223 MB of glibc-locales because HM references it. ~36 seconds at 50 Mbps. One-time cost. Acceptable. Could be eliminated by setting `i18n.glibcLocales = pkgs.glibcLocales.override { allLocales = false; locales = ["en_US.UTF-8"]; };` if it ever becomes painful.
- **`coding-cave.claude-code.bundles` order matters.** Bundle settings merge in order, so a later bundle overrides an earlier one. Document that user-supplied bundles should come last. The cave-system module's `extraSettings` always wins over bundle defaults.
- **Persist conversion is a separate change.** It needs its own commit/PR, since `persist` is its own repo. Sequence: convert persist first, update its consumers, then proceed with the coding-cave changes.

## Out of scope

- Upstream contributions to HM's `programs.claude-code` (memory fragments, hooks-as-list type, `.claude.json` seeding). Worth doing eventually, but not blocking. Tracked separately.
- Replacing `mkClaudeConfig` for non-cave use. There is no other consumer; nix-claude can be archived once dotfiles and coding-cave both stop using it.
