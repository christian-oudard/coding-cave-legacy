#!/usr/bin/env bash
# Evaluate the nix modules the way their consumers do -- the host NixOS module
# as nixos-rebuild applies it, the cave user env as an agent launch builds it --
# but build-free, to catch argument / option / type regressions in
# modules/host.nix, modules/claude-code.nix, or their wiring in flake.nix
# before they reach a real rebuild or a failed cave launch.
#
# Why this shape:
#   - `nix flake check` only shape-checks the `nixosModules` output; it never
#     applies the module to a config, so it misses errors that fire on
#     application (e.g. the flake passing the wrong args into host.nix).
#   - `nix build` builds the cav package, not the module.
#   - `builtins.getFlake` inside `--expr` serves a stale flake evaluation: it
#     does NOT reliably reflect the current tree, so a script built around it
#     false-passes.
#
# So we lean on real flake outputs, checks.<sys>.hostModuleEval (instantiates
# the module in a nixosSystem) and checks.<sys>.caveEnvEval (instantiates a
# representative cave env through lib.mkCaveEnv). Evaluating their `.drvPath`
# via the flake CLI reflects the working tree correctly and forces the whole
# module fixpoint (options, config, the rendered files) -- but realises
# nothing, so there are no downloads and neither the system nor cav is built.
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
system="$(nix eval --impure --raw --expr 'builtins.currentSystem')"

cd "$root"
for check in hostModuleEval caveEnvEval; do
  nix eval --no-eval-cache --extra-experimental-features 'nix-command flakes' \
    ".#checks.${system}.${check}.drvPath" >/dev/null
done

echo "host NixOS module and cave env evaluate"
