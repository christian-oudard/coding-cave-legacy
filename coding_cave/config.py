"""Configuration loading and environment setup."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from . import _state


def load_global_config():
    config_path = Path.home() / ".config" / "coding-cave" / "config.toml"
    if config_path.exists():
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    return {}


def _git_config(key):
    """Read a git config value from the host, or None."""
    r = subprocess.run(
        ["git", "config", "--global", key],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _detect_gpg_agent_socket():
    """Find the gpg-agent socket path, or None."""
    # gpgconf --list-dirs gives us the actual socket path
    try:
        r = subprocess.run(
            ["gpgconf", "--list-dirs", "agent-socket"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            sock = Path(r.stdout.strip())
            if sock.exists():
                return str(sock)
    except FileNotFoundError:
        pass  # gpgconf not installed
    # Fallback: standard location
    gnupghome = os.environ.get("GNUPGHOME", str(Path.home() / ".gnupg"))
    sock = Path(gnupghome) / "S.gpg-agent"
    if sock.exists():
        return str(sock)
    return None


def setup_env(global_config):
    """Set environment variables from global config."""
    token = global_config.get("credentials", {}).get("claude-oauth-token")
    if token:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token

    claude_md = Path.home() / ".config" / "coding-cave" / "CLAUDE.md"
    if claude_md.exists():
        os.environ["CODING_CAVE_USER_CLAUDE_MD"] = str(claude_md)

    # Forward host git identity into cave (eliminates re-authoring on pull)
    for key, env_var in [
        ("user.name", "CODING_CAVE_GIT_NAME"),
        ("user.email", "CODING_CAVE_GIT_EMAIL"),
        ("user.signingkey", "CODING_CAVE_GIT_SIGNING_KEY"),
        ("commit.gpgsign", "CODING_CAVE_GIT_GPG_SIGN"),
    ]:
        val = _git_config(key)
        if val:
            os.environ[env_var] = val

    # Forward GPG agent socket for commit signing
    gpg_socket = _detect_gpg_agent_socket()
    if gpg_socket:
        os.environ["CODING_CAVE_GPG_AGENT_SOCK"] = gpg_socket
    # Forward GNUPGHOME so cave gpg finds the pubring
    gnupghome = os.environ.get("GNUPGHOME", str(Path.home() / ".gnupg"))
    if Path(gnupghome).is_dir():
        os.environ["CODING_CAVE_GNUPGHOME"] = gnupghome

    # Shell dotfiles — only forwarded if mirror config enables them
    mirror = global_config.get("mirror", {})
    if mirror.get("zsh"):
        zdotdir = Path.home() / ".config" / "zsh"
        if zdotdir.is_dir():
            os.environ["CODING_CAVE_ZDOTDIR"] = str(zdotdir)
    if mirror.get("bash", True):
        bashrc = Path.home() / ".bashrc"
        if bashrc.exists():
            os.environ["CODING_CAVE_BASHRC"] = str(bashrc)
        bash_profile = Path.home() / ".bash_profile"
        if bash_profile.exists():
            os.environ["CODING_CAVE_BASH_PROFILE"] = str(bash_profile)
    if mirror.get("git"):
        gitconfig = Path.home() / ".gitconfig"
        if gitconfig.exists():
            os.environ["CODING_CAVE_GITCONFIG"] = str(gitconfig)
    if mirror.get("nvim"):
        xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        nvim_config = Path(xdg_config) / "nvim"
        if nvim_config.is_dir():
            os.environ["CODING_CAVE_NVIM_CONFIG"] = str(nvim_config)
        # Discover host nvim wrapper's nix store path so the cave uses the
        # same binary (with its plugins, LUA_PATH, etc.) instead of the bare
        # cave neovim.  The nix store is available in the cave via overlay.
        import shutil
        host_nvim = shutil.which("nvim")
        if host_nvim:
            resolved = str(Path(host_nvim).resolve())
            if resolved.startswith("/nix/store/"):
                os.environ["CODING_CAVE_NVIM_HOST_BIN"] = resolved


_DEFAULT_CAVE_NIX = """\
{
  inputs = {};
  config = { pkgs }: {
    # packages = with pkgs; [ python3 uv ];
    # settings = {};
    # env = {};
    # skills = {};
  };
}
"""

_GENERATED_FLAKE_TEMPLATE = """\
{{
  inputs = {{
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
{input_declarations}
  }};

  outputs = {{ self, nixpkgs{input_names_param} }}: let
    pkgs = import nixpkgs {{ system = "x86_64-linux"; }};
    caveNix = import ./cave.nix;
    userConfig = caveNix.config {{
      inherit pkgs;
{input_passes}
    }};
    # Resolve plugins: user passes raw flake inputs, we extract plugin.${{system}}
    rawPlugins = userConfig.plugins or [];
    plugins = map (p: if p ? plugin then p.plugin.x86_64-linux else p) rawPlugins;
    pluginPackages = builtins.concatMap (p: if p ? package then [p.package] else []) plugins;
    packages = (userConfig.packages or []) ++ pluginPackages;
    pluginSettings = builtins.foldl' (acc: p:
      pkgs.lib.recursiveUpdate acc (p.settings or {{}})
    ) {{}} plugins;
    settings = pkgs.lib.recursiveUpdate pluginSettings (userConfig.settings or {{}});
    env = userConfig.env or {{}};
    skills = userConfig.skills or {{}};
    pluginSkills = builtins.concatMap (p: p.skills or []) plugins;
  in {{
    packages.x86_64-linux.default = pkgs.runCommand "cave-user-env" {{}} ''
      mkdir -p $out/packages $out/claude/skills

      # Symlink package bin dirs
      ${{pkgs.lib.concatMapStringsSep "\\n" (p: ''
        if [ -d "${{p}}/bin" ]; then
          ln -s ${{p}} $out/packages/${{p.pname or p.name or (builtins.parseDrvName p.name).name}}
        fi
      '') packages}}

      # Write settings.json
      printf '%s' '${{builtins.toJSON settings}}' > $out/claude/settings.json

      # Write env.json
      printf '%s' '${{builtins.toJSON env}}' > $out/claude/env.json

      # Write inline skills
      ${{pkgs.lib.concatStringsSep "\\n" (pkgs.lib.mapAttrsToList (name: content: ''
        mkdir -p $out/claude/skills/${{name}}
        cat > $out/claude/skills/${{name}}/SKILL.md << 'SKILL_EOF'
${{content}}
SKILL_EOF
      '') skills)}}

      # Copy plugin skills (strip nix hash prefix from directory names)
      ${{pkgs.lib.concatMapStringsSep "\\n" (skillPath:
        let name = builtins.substring 33 (-1) (builtins.baseNameOf skillPath);
        in ''
        rm -rf "$out/claude/skills/${{name}}"
        mkdir -p "$out/claude/skills/${{name}}"
        for f in ${{skillPath}}/*; do
          install -m 644 "$f" "$out/claude/skills/${{name}}/"
        done
      '') pluginSkills}}
    '';
  }};
}}
"""


def _cave_nix_path():
    return Path.home() / ".config" / "coding-cave" / "cave.nix"


def _user_env_dir():
    return _state.cave_data() / "user-env"


def build_user_env():
    """Build user environment from cave.nix. Returns result path or None."""
    cave_nix = _cave_nix_path()

    # Auto-generate cave.nix if missing
    if not cave_nix.exists():
        cave_nix.parent.mkdir(parents=True, exist_ok=True)
        cave_nix.write_text(_DEFAULT_CAVE_NIX)

    user_env = _user_env_dir()
    user_env.mkdir(parents=True, exist_ok=True)

    # Copy cave.nix into build dir
    dest_cave_nix = user_env / "cave.nix"
    cave_nix_content = cave_nix.read_text()
    dest_cave_nix.write_text(cave_nix_content)

    # Check if rebuild needed (hash cave.nix + flake.lock)
    h = hashlib.sha256(cave_nix_content.encode())
    lock_file = user_env / "flake.lock"
    if lock_file.exists():
        h.update(lock_file.read_bytes())
    content_hash = h.hexdigest()[:16]
    hash_file = user_env / ".cave-nix-hash"
    result_link = user_env / "result"
    if hash_file.exists() and hash_file.read_text() == content_hash and result_link.exists():
        return result_link.resolve()

    # Extract inputs from cave.nix via nix eval
    r = subprocess.run(
        ["nix", "eval", "--json", "-f", str(dest_cave_nix), "inputs"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"Warning: failed to eval cave.nix inputs: {r.stderr.strip()}", file=sys.stderr)
        return _try_existing_result(result_link)

    try:
        inputs = json.loads(r.stdout)
    except json.JSONDecodeError:
        print(f"Warning: cave.nix inputs is not valid JSON", file=sys.stderr)
        return _try_existing_result(result_link)

    # Generate flake.nix
    input_declarations = ""
    input_names = []
    input_passes = ""
    for name, url in inputs.items():
        input_declarations += f'    {name}.url = "{url}";\n'
        input_names.append(name)
        input_passes += f'      {name} = {name};\n'

    input_names_param = ""
    if input_names:
        input_names_param = ", " + ", ".join(input_names)

    flake_content = _GENERATED_FLAKE_TEMPLATE.format(
        input_declarations=input_declarations,
        input_names_param=input_names_param,
        input_passes=input_passes,
    )
    (user_env / "flake.nix").write_text(flake_content)

    # Build and register a GC root so nix-collect-garbage on the host
    # can't delete the cave-user-env or its closure.  nix build --out-link
    # does NOT register GC roots (unlike nix-build), so we use
    # nix-store --add-root explicitly.
    print("Building user environment from cave.nix...", file=sys.stderr, flush=True)
    r = subprocess.run(
        ["nix", "build", "--no-link", "--print-out-paths"],
        capture_output=True, text=True,
        cwd=str(user_env),
    )
    if r.returncode != 0:
        print(f"Warning: cave.nix build failed: {r.stderr.strip()}", file=sys.stderr)
        return _try_existing_result(result_link)

    out_path = r.stdout.strip().split("\n")[0]

    # Create result symlink as a GC root.
    # nix-store --add-root creates an indirect root: a symlink in
    # /nix/var/nix/gcroots/auto/ points to result_link, which points
    # to the store path.  This protects the entire closure from GC.
    result_link.unlink(missing_ok=True)
    subprocess.run(
        ["nix-store", "--add-root", str(result_link), "--indirect",
         "-r", out_path],
        capture_output=True, check=True,
    )

    hash_file.write_text(content_hash)
    return Path(out_path)


def _try_existing_result(result_link):
    """Return existing build result if available, else None."""
    if result_link.exists():
        return result_link.resolve()
    return None
