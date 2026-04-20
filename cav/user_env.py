"""Build user environment from cave.nix.

The user's cave.nix (~/.config/coding-cave/cave.nix) is a simple config file,
not a flake. It declares user inputs and a config function that returns a
home-manager module:

    {
      inputs = {
        persist = { url = "github:christian-oudard/persist"; flake = false; };
      };
      config = { pkgs, persist, ... }: {
        home.packages = with pkgs; [ coreutils nano ];
        coding-cave.claude-code.bundles = [ { src = persist; } ];
      };
    }

This module wraps it in a proper flake (user-env/flake.nix) that wires in
system dependencies (nixpkgs, claude-code, home-manager, coding-cave) via
build-time substituted store paths. The user never sees those system URLs.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from . import config
from .constants import (
    CLAUDE_CODE_NIX_URL as _CLAUDE_CODE_NIX_URL,
    CODING_CAVE_URL as _CODING_CAVE_URL,
    HOME_MANAGER_URL as _HOME_MANAGER_URL,
    NIXPKGS_URL as _NIXPKGS_URL,
)


def _cave_nix_path():
    host_home = os.environ.get("CODING_CAVE_HOST_HOME", str(Path.home()))
    return Path(host_home) / ".config" / "coding-cave" / "cave.nix"


def _user_env_dir():
    return config.DATA_DIR / "user-env"


def _hash_file():
    return config.DATA_DIR / "user-env.hash"


def _compute_hash(cave_nix_content=None, sibling_files=None):
    """Hash combining system deps and user config."""
    parts = [
        _NIXPKGS_URL, _CLAUDE_CODE_NIX_URL, _HOME_MANAGER_URL,
        _CODING_CAVE_URL,
        cave_nix_content or "",
    ]
    if sibling_files:
        for name, content in sorted(sibling_files.items()):
            parts.append(name)
            parts.append(content)
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _needs_build(cave_nix_content=None, sibling_files=None):
    result_link = _user_env_dir() / "result"
    if not result_link.exists():
        return True
    hash_file = _hash_file()
    if not hash_file.exists():
        return True
    return hash_file.read_text().strip() != _compute_hash(
        cave_nix_content, sibling_files
    )


def _to_nix(value):
    """Convert a Python value (from JSON) to Nix syntax."""
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")
        return f'"{escaped}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        items = [f"{k} = {_to_nix(v)}" for k, v in value.items()]
        return "{ " + "; ".join(items) + "; }"
    if isinstance(value, list):
        return "[ " + " ".join(_to_nix(v) for v in value) + " ]"
    return str(value)


def _extract_user_inputs(cave_nix_path):
    """Extract the inputs attrset from cave.nix via nix eval."""
    r = subprocess.run(
        ["nix", "eval", "--json", "--impure", "--expr",
         f"(import {cave_nix_path}).inputs or {{}}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        return {}
    return json.loads(r.stdout)


_SYSTEM_INPUT_NAMES = {"self", "nixpkgs", "claude-code", "home-manager", "coding-cave"}


def _generate_flake(has_cave_nix, user_inputs=None):
    """Generate flake.nix that wraps the user's cave.nix config."""
    if user_inputs is None:
        user_inputs = {}

    lines = []
    lines.append("{\n")
    lines.append(f'  inputs.nixpkgs.url = "{_NIXPKGS_URL}";\n')
    lines.append("  inputs.claude-code = {\n")
    lines.append(f'    url = "{_CLAUDE_CODE_NIX_URL}";\n')
    lines.append('    inputs.nixpkgs.follows = "nixpkgs";\n')
    lines.append("  };\n")
    lines.append("  inputs.home-manager = {\n")
    lines.append(f'    url = "{_HOME_MANAGER_URL}";\n')
    lines.append('    inputs.nixpkgs.follows = "nixpkgs";\n')
    lines.append("  };\n")
    lines.append("  inputs.coding-cave = {\n")
    lines.append(f'    url = "{_CODING_CAVE_URL}";\n')
    lines.append('    inputs.nixpkgs.follows = "nixpkgs";\n')
    lines.append('    inputs.home-manager.follows = "home-manager";\n')
    lines.append("  };\n")

    for name, spec in user_inputs.items():
        if name in _SYSTEM_INPUT_NAMES:
            continue
        if isinstance(spec, str):
            lines.append(f'  inputs.{name}.url = "{spec}";\n')
        else:
            lines.append(f"  inputs.{name} = {_to_nix(spec)};\n")

    lines.append("\n")
    lines.append("  outputs = { nixpkgs, claude-code, home-manager, coding-cave, ... }@inputs:\n")

    if has_cave_nix:
        lines.append("    let\n")
        lines.append("      userCfg = import ./cave.nix;\n")
        lines.append("      userInputs = builtins.removeAttrs inputs\n")
        lines.append(f'        {_to_nix(sorted(_SYSTEM_INPUT_NAMES))};\n')
        lines.append("    in coding-cave.lib.mkCaveEnv {\n")
        lines.append("      inherit claude-code;\n")
        lines.append("      userModule = { pkgs, ... }:\n")
        lines.append("        userCfg.config (userInputs // { inherit pkgs; });\n")
        lines.append("    };\n")
    else:
        lines.append("    coding-cave.lib.mkCaveEnv {\n")
        lines.append("      inherit claude-code;\n")
        lines.append("      userModule = { ... }: { };\n")
        lines.append("    };\n")

    lines.append("}\n")
    return "".join(lines)


def _copy_sibling_nix_files(cave_nix_src, user_env):
    """Copy *.nix files alongside cave.nix into the build dir, so relative
    imports like `imports = [ ./claude.nix ];` resolve. Returns dict of
    {filename: content} for hash computation.
    """
    sibling_files = {}
    src_dir = cave_nix_src.parent
    if not src_dir.is_dir():
        return sibling_files
    for entry in src_dir.iterdir():
        if entry.name == cave_nix_src.name:
            continue
        if entry.is_file() and entry.suffix == ".nix":
            content = entry.read_text()
            (user_env / entry.name).write_text(content)
            sibling_files[entry.name] = content
    return sibling_files


def _clean_stale_sibling_files(user_env, current_names):
    """Remove .nix files in user_env that are not in current_names and are
    not flake.nix or cave.nix."""
    keep = current_names | {"flake.nix", "cave.nix"}
    for entry in user_env.iterdir():
        if entry.is_file() and entry.suffix == ".nix" and entry.name not in keep:
            entry.unlink()


def _prepare():
    """Copy cave.nix and generate flake.nix wrapper.

    Returns (cave_nix_content, sibling_files) where sibling_files is a dict
    of {name: content} for any *.nix files copied alongside cave.nix.
    """
    user_env = _user_env_dir()
    user_env.mkdir(parents=True, exist_ok=True)

    cave_nix = _cave_nix_path()
    cave_nix_dest = user_env / "cave.nix"

    if cave_nix.exists():
        cave_nix_content = cave_nix.read_text()
        cave_nix_dest.write_text(cave_nix_content)
        sibling_files = _copy_sibling_nix_files(cave_nix, user_env)
        user_inputs = _extract_user_inputs(cave_nix_dest)
        flake = _generate_flake(has_cave_nix=True, user_inputs=user_inputs)
    else:
        cave_nix_content = None
        sibling_files = {}
        cave_nix_dest.unlink(missing_ok=True)
        flake = _generate_flake(has_cave_nix=False)

    _clean_stale_sibling_files(user_env, set(sibling_files.keys()))
    (user_env / "flake.nix").write_text(flake)

    return cave_nix_content, sibling_files


def _do_build(user_env):
    """Run nix build in the user-env directory. Returns output path or None."""
    result_link = user_env / "result"
    r = subprocess.run(
        ["nix", "build", "--no-link", "--print-out-paths"],
        stdout=subprocess.PIPE, text=True,
        cwd=str(user_env),
    )
    if r.returncode != 0:
        print("Warning: cave environment build failed.", file=sys.stderr)
        return None

    out_path = r.stdout.strip().split("\n")[0]
    old_path = result_link.resolve() if result_link.exists() else None
    result_link.unlink(missing_ok=True)
    result_link.symlink_to(out_path)
    if str(old_path) != out_path:
        print("Cave environment updated.", file=sys.stderr, flush=True)
    return Path(out_path)


def _save_hash(cave_nix_content, sibling_files):
    _hash_file().write_text(_compute_hash(cave_nix_content, sibling_files))


def _read_sibling_files():
    """Read current sibling .nix files from disk for hash check."""
    cave_nix = _cave_nix_path()
    sibling_files = {}
    if not cave_nix.exists():
        return sibling_files
    src_dir = cave_nix.parent
    if not src_dir.is_dir():
        return sibling_files
    for entry in src_dir.iterdir():
        if entry.name == cave_nix.name:
            continue
        if entry.is_file() and entry.suffix == ".nix":
            sibling_files[entry.name] = entry.read_text()
    return sibling_files


def build():
    """Build cave environment. Skips if unchanged."""
    cave_nix = _cave_nix_path()
    cave_nix_content = cave_nix.read_text() if cave_nix.exists() else None
    sibling_files = _read_sibling_files()

    result_link = _user_env_dir() / "result"
    if not _needs_build(cave_nix_content, sibling_files):
        return result_link.resolve()

    cave_nix_content, sibling_files = _prepare()
    out = _do_build(_user_env_dir())
    if out:
        _save_hash(cave_nix_content, sibling_files)
    return out


def update(input_name=None):
    """Update flake lock file without rebuilding."""
    _prepare()
    user_env = _user_env_dir()

    cmd = ["nix", "flake", "update"]
    if input_name:
        cmd.append(input_name)
    r = subprocess.run(cmd, cwd=str(user_env))
    if r.returncode != 0:
        raise SystemExit("nix flake update failed.")
    print("Run 'cav env build' to rebuild with updated inputs.", file=sys.stderr)

def metadata():
    """Show flake metadata for the cave environment."""
    _prepare()
    subprocess.run(
        ["nix", "flake", "metadata"],
        cwd=str(_user_env_dir()),
    )


def default_cave_nix():
    """Return default cave.nix content for cav init."""
    return """\
{
  config = { pkgs, ... }: {
    home.packages = with pkgs; [
      jq gawk findutils curl wget nano
    ];

    home.sessionVariables = {
      EDITOR = "nano";
    };
  };
}
"""
