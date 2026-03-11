"""Configuration loading and environment setup."""
import os
import subprocess
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
        os.environ["CODING_CAVE_CLAUDE_MD"] = str(claude_md)

    claude_settings = Path.home() / ".config" / "coding-cave" / "settings.json"
    if claude_settings.exists():
        os.environ["CODING_CAVE_CLAUDE_SETTINGS"] = str(claude_settings)

    skills_dir = Path.home() / ".config" / "coding-cave" / "skills"
    if skills_dir.is_dir():
        os.environ["CODING_CAVE_SKILLS_DIR"] = str(skills_dir)

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

    user_shell = Path(os.environ.get("SHELL", "/bin/bash")).name
    if user_shell == "zsh":
        zdotdir = Path.home() / ".config" / "zsh"
        if zdotdir.is_dir():
            os.environ["CODING_CAVE_ZDOTDIR"] = str(zdotdir)
    else:
        bashrc = Path.home() / ".bashrc"
        if bashrc.exists():
            os.environ["CODING_CAVE_BASHRC"] = str(bashrc)
        bash_profile = Path.home() / ".bash_profile"
        if bash_profile.exists():
            os.environ["CODING_CAVE_BASH_PROFILE"] = str(bash_profile)
