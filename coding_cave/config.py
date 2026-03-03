"""Configuration loading and environment setup."""
import os
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
