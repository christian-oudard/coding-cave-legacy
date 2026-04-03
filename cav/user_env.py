"""Build user environment from cave.nix."""
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from . import config


_FLAKE_TEMPLATE = """\
{{
  inputs = {{
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nix-claude = {{
      url = "@NIX_CLAUDE_URL@";
      inputs.nixpkgs.follows = "nixpkgs";
    }};
{input_declarations}  }};

  outputs = {{ self, nixpkgs, nix-claude{input_names_param} }}: let
    pkgs = import nixpkgs {{ system = "{system}"; }};
    mkClaudeConfig = nix-claude.lib.mkClaudeConfig;
    caveNix = import ./cave.nix;
    userConfig = caveNix.config {{
      inherit pkgs;
{input_passes}    }};
    claude = userConfig.claude or {{}};
    claudeConfig = mkClaudeConfig ({{ inherit pkgs; }} // claude);
    packages = (userConfig.packages or []) ++ (claudeConfig.packages or []);
    env = userConfig.env or {{}};
    files = userConfig.files or {{}};
  in {{
    packages.{system}.default = pkgs.runCommand "cave-user-env" {{}} ''
      mkdir -p $out/packages

      ${{pkgs.lib.concatMapStringsSep "\\n" (p:
        let name = p.pname or (builtins.parseDrvName p.name).name; in
        ''
          if [ -d "${{p}}/bin" ]; then
            ln -s ${{p}} $out/packages/${{name}}
          fi
        ''
      ) packages}}

      ln -s ${{claudeConfig}} $out/claude

      cat > $out/env.sh << 'ENVEOF'
${{pkgs.lib.concatStringsSep "\\n" (pkgs.lib.mapAttrsToList (k: v:
  "export ${{k}}=\\"${{v}}\\""
) env)}}
ENVEOF

      ${{pkgs.lib.concatStringsSep "\\n" (pkgs.lib.mapAttrsToList (relPath: content:
        let
          safeName = builtins.replaceStrings ["/"] ["-"] relPath;
          fileStore = pkgs.writeText "cave-file-${{safeName}}" content;
        in ''
          mkdir -p "$out/files/$(dirname "${{relPath}}")"
          cp ${{fileStore}} "$out/files/${{relPath}}"
        ''
      ) files)}}
    '';
  }};
}}
"""


def _cave_nix_path():
    host_home = os.environ.get("CODING_CAVE_HOST_HOME", str(Path.home()))
    return Path(host_home) / ".config" / "coding-cave" / "cave.nix"


def _user_env_dir():
    return config.DATA_DIR / "user-env"


def _generate_flake(cave_nix_path, user_env_dir):
    """Generate flake.nix wrapper around cave.nix."""
    r = subprocess.run(
        ["nix", "eval", "--json", "-f", str(cave_nix_path), "inputs"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        inputs = {}
    else:
        try:
            inputs = json.loads(r.stdout)
        except json.JSONDecodeError:
            inputs = {}

    input_declarations = ""
    input_names = []
    input_passes = ""
    for name, spec in inputs.items():
        if isinstance(spec, str):
            input_declarations += f'    {name}.url = "{spec}";\n'
        elif isinstance(spec, dict):
            for attr, val in spec.items():
                if isinstance(val, bool):
                    input_declarations += f'    {name}.{attr} = {"true" if val else "false"};\n'
                elif isinstance(val, str):
                    input_declarations += f'    {name}.{attr} = "{val}";\n'
        input_names.append(name)
        input_passes += f'      {name} = {name};\n'

    system = f"{platform.machine()}-linux"
    input_names_param = ""
    if input_names:
        input_names_param = ", " + ", ".join(input_names)

    (user_env_dir / "flake.nix").write_text(_FLAKE_TEMPLATE.format(
        system=system,
        input_declarations=input_declarations,
        input_names_param=input_names_param,
        input_passes=input_passes,
    ))


def build():
    """Build user environment from cave.nix. Returns result path or None."""
    cave_nix = _cave_nix_path()
    if not cave_nix.exists():
        return None

    user_env = _user_env_dir()
    user_env.mkdir(parents=True, exist_ok=True)

    content = cave_nix.read_text()
    (user_env / "cave.nix").write_text(content)

    _generate_flake(cave_nix, user_env)

    result_link = user_env / "result"

    r = subprocess.run(
        ["nix", "build", "--no-link", "--print-out-paths"],
        stdout=subprocess.PIPE, text=True,
        cwd=str(user_env),
    )
    if r.returncode != 0:
        print(f"Warning: cave.nix build failed.", file=sys.stderr)
        if result_link.exists():
            return result_link.resolve()
        return None

    out_path = r.stdout.strip().split("\n")[0]
    old_path = result_link.resolve() if result_link.exists() else None
    result_link.unlink(missing_ok=True)
    result_link.symlink_to(out_path)
    if str(old_path) != out_path:
        print(f"User environment updated from {cave_nix}.", file=sys.stderr, flush=True)
    return Path(out_path)
