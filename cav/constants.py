"""Fixed constants for Coding Cave.

Build-time values (version, nix store paths, flake URLs) come from a
JSON config file whose path is in `CODING_CAVE_BUILD_CONFIG`. The cav
wrapper binary sets that env var at install time; running from source
(nix develop) leaves it unset and build values fall back to "dev".
"""

import json
import os
import re
from pathlib import Path


def _load_build_config() -> dict:
    path = os.environ.get("CODING_CAVE_BUILD_CONFIG")
    if not path:
        return {}
    return json.loads(Path(path).read_text())


_build = _load_build_config()

VERSION = _build.get("version", "dev")
VERSION_DATE = _build.get("versionDate", "dev")
CAV_BIN = _build.get("cavBin", "")
SANDBOX_CONFIG = _build.get("sandboxConfig", "")
NIXPKGS_URL = _build.get("nixpkgsUrl", "")
CLAUDE_CODE_NIX_URL = _build.get("claudeCodeNixUrl", "")
HOME_MANAGER_URL = _build.get("homeManagerUrl", "")
CODING_CAVE_URL = _build.get("codingCaveUrl", "")

# supervisor.py ships inside cav/sandbox/; no build-time injection needed.
SUPERVISOR_PY = str(Path(__file__).parent / "sandbox" / "supervisor.py")

# Git remote names
CAVE_SHARED_REMOTE = "cave"
HOST_SHARED_REMOTE = "host-cave"

# Bare repo directory names under .coding-cave/
SHARED_REPO_DIR = "cave-shared.git"
HOST_SHARED_REPO_DIR = "host-cave-shared.git"

# Agent naming
AGENT_ID_RE = re.compile(r"^\d+$")
AGENT_BRANCH_RE = re.compile(r"^agent-(\d+)/(.+)$")

# Networking
DAEMON_BASE_PORT = 61000
