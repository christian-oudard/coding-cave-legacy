#!/usr/bin/env python3
"""
coding-cave wrapper: reads config, sets up environment, launches sandbox
"""
import os
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib


def main():
    config_path = Path.home() / ".config" / "coding-cave" / "config.toml"

    if config_path.exists():
        with open(config_path, "rb") as f:
            config = tomllib.load(f)

        token = config.get("credentials", {}).get("claude-oauth-token")
        if token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token

    # SANDBOX_BIN will be replaced by Nix
    sandbox_bin = "@SANDBOX_BIN@"
    os.execv(sandbox_bin, ["codingcave"] + sys.argv[1:])


if __name__ == "__main__":
    main()
