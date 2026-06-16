#!/usr/bin/env bash
# Evaluate the host NixOS module the way nixos-rebuild does, but build-free,
# to catch argument / option / type regressions in modules/host.nix or its
# wiring in flake.nix before they reach a real rebuild.
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
# So we lean on a real flake output, checks.<sys>.hostModuleEval, which
# instantiates the module in a nixosSystem. Evaluating its `.drvPath` via the
# flake CLI reflects the working tree correctly and forces the whole module
# fixpoint (options, config, the rendered config file) -- but realises
# nothing, so there are no downloads and the system/cav are never built.
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
system="$(nix eval --impure --raw --expr 'builtins.currentSystem')"

cd "$root"
nix eval --no-eval-cache --extra-experimental-features 'nix-command flakes' \
  ".#checks.${system}.hostModuleEval.drvPath" >/dev/null

echo "host NixOS module evaluates"
