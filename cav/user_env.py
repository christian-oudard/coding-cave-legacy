"""Build user environment from cave.nix.

The user's cave.nix (~/.config/coding-cave/cave.nix) is a simple config file,
not a flake. It declares user inputs and a config function:

    {
      inputs = {
        persist = "github:christian-oudard/persist";
      };
      config = { pkgs, persist, ... }: {
        packages = with pkgs; [ coreutils bash nix git nano ];
        claude.plugins = [ persist ];
      };
    }

This module wraps it in a proper flake (user-env/flake.nix) that wires in
system dependencies (nixpkgs, nix-claude, mkCaveEnv) via build-time
substituted store paths. The user never sees those system URLs.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from . import config


# Build-time substituted store paths and URLs.
_NIXPKGS_URL = "@NIXPKGS_URL@"
_CLAUDE_CODE_NIX_URL = "@CLAUDE_CODE_NIX_URL@"
_NIX_CLAUDE_URL = "@NIX_CLAUDE_URL@"
_MKCAVEENV_PATH = "@MKCAVEENV_PATH@"
_SYSTEM_CLAUDE_MD_PATH = "@SYSTEM_CLAUDE_MD_PATH@"
_DEFAULT_STATUSLINE_PATH = "@DEFAULT_STATUSLINE_PATH@"


def _cave_nix_path():
    host_home = os.environ.get("CODING_CAVE_HOST_HOME", str(Path.home()))
    return Path(host_home) / ".config" / "coding-cave" / "cave.nix"


def _user_env_dir():
    return config.DATA_DIR / "user-env"


def _hash_file():
    return config.DATA_DIR / "user-env.hash"


def _compute_hash(cave_nix_content=None):
    """Hash combining system deps and user config."""
    parts = [
        _NIXPKGS_URL, _CLAUDE_CODE_NIX_URL, _NIX_CLAUDE_URL,
        _MKCAVEENV_PATH, _SYSTEM_CLAUDE_MD_PATH, _DEFAULT_STATUSLINE_PATH,
        cave_nix_content or "",
    ]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _needs_build(cave_nix_content=None):
    result_link = _user_env_dir() / "result"
    if not result_link.exists():
        return True
    hash_file = _hash_file()
    if not hash_file.exists():
        return True
    return hash_file.read_text().strip() != _compute_hash(cave_nix_content)


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


def _generate_flake(has_cave_nix, user_inputs=None):
    """Generate flake.nix that wraps the user's cave.nix config."""
    if user_inputs is None:
        user_inputs = {}

    lines = []
    lines.append("{\n")
    lines.append(f'  inputs.nixpkgs.url = "{_NIXPKGS_URL}";\n')
    lines.append(f"  inputs.claude-code = {{\n")
    lines.append(f'    url = "{_CLAUDE_CODE_NIX_URL}";\n')
    lines.append(f"    inputs.nixpkgs.follows = \"nixpkgs\";\n")
    lines.append(f"  }};\n")
    lines.append(f"  inputs.nix-claude = {{\n")
    lines.append(f'    url = "{_NIX_CLAUDE_URL}";\n')
    lines.append(f"    inputs.nixpkgs.follows = \"nixpkgs\";\n")
    lines.append(f"  }};\n")

    for name, spec in user_inputs.items():
        if isinstance(spec, str):
            lines.append(f'  inputs.{name}.url = "{spec}";\n')
        else:
            lines.append(f"  inputs.{name} = {_to_nix(spec)};\n")

    lines.append("\n")
    lines.append("  outputs = { nixpkgs, claude-code, nix-claude, ... }@inputs:\n")
    lines.append("    let\n")
    lines.append("      mkCaveEnv = import ./mkCaveEnv.nix {\n")
    lines.append("        inherit nixpkgs nix-claude;\n")
    lines.append("        systemClaudeMd = ./system-claude.md;\n")
    lines.append("        defaultStatusline = ./statusline.sh;\n")
    lines.append("      };\n")

    if has_cave_nix:
        lines.append("      userCfg = import ./cave.nix;\n")
        lines.append("      userInputs = builtins.removeAttrs inputs\n")
        lines.append('        ["self" "nixpkgs" "claude-code" "nix-claude"];\n')
        lines.append("    in mkCaveEnv {\n")
        lines.append("      inherit claude-code;\n")
        lines.append("      config = { pkgs }: userCfg.config (userInputs // { inherit pkgs; });\n")
        lines.append("    };\n")
    else:
        lines.append("    in mkCaveEnv { inherit claude-code; };\n")

    lines.append("}\n")
    return "".join(lines)


def _prepare():
    """Copy cave.nix and generate flake.nix wrapper. Returns cave_nix_content or None."""
    user_env = _user_env_dir()
    user_env.mkdir(parents=True, exist_ok=True)

    cave_nix = _cave_nix_path()
    cave_nix_dest = user_env / "cave.nix"

    if cave_nix.exists():
        cave_nix_content = cave_nix.read_text()
        cave_nix_dest.write_text(cave_nix_content)
        user_inputs = _extract_user_inputs(cave_nix_dest)
        flake = _generate_flake(has_cave_nix=True, user_inputs=user_inputs)
    else:
        cave_nix_content = None
        cave_nix_dest.unlink(missing_ok=True)
        flake = _generate_flake(has_cave_nix=False)

    (user_env / "flake.nix").write_text(flake)

    # Copy system files so the flake can reference them with relative paths
    # (absolute store paths are forbidden in pure evaluation mode).
    import shutil
    for src, dest_name in [
        (_MKCAVEENV_PATH, "mkCaveEnv.nix"),
        (_SYSTEM_CLAUDE_MD_PATH, "system-claude.md"),
        (_DEFAULT_STATUSLINE_PATH, "statusline.sh"),
    ]:
        dest = user_env / dest_name
        shutil.copyfile(src, dest)

    return cave_nix_content


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
        if result_link.exists():
            return result_link.resolve()
        return None

    out_path = r.stdout.strip().split("\n")[0]
    old_path = result_link.resolve() if result_link.exists() else None
    result_link.unlink(missing_ok=True)
    result_link.symlink_to(out_path)
    if str(old_path) != out_path:
        print("Cave environment updated.", file=sys.stderr, flush=True)
    return Path(out_path)


def _save_hash(cave_nix_content):
    _hash_file().write_text(_compute_hash(cave_nix_content))


def build():
    """Build cave environment. Skips if unchanged."""
    cave_nix = _cave_nix_path()
    cave_nix_content = cave_nix.read_text() if cave_nix.exists() else None

    result_link = _user_env_dir() / "result"
    if not _needs_build(cave_nix_content):
        return result_link.resolve()

    cave_nix_content = _prepare()
    out = _do_build(_user_env_dir())
    if out:
        _save_hash(cave_nix_content)
    return out


def update(input_name=None):
    """Update flake inputs and rebuild."""
    _prepare()
    user_env = _user_env_dir()

    cmd = ["nix", "flake", "update"]
    if input_name:
        cmd.append(input_name)
    r = subprocess.run(cmd, cwd=str(user_env))
    if r.returncode != 0:
        raise SystemExit("nix flake update failed.")

    cave_nix = _cave_nix_path()
    cave_nix_content = cave_nix.read_text() if cave_nix.exists() else None
    out = _do_build(user_env)
    if out:
        _save_hash(cave_nix_content)
    return out


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
    packages = with pkgs; [
      gnupg jq gnugrep gnused gawk findutils
      curl wget nano
    ];

    env = {
      EDITOR = "nano";
    };
  };
}
"""
