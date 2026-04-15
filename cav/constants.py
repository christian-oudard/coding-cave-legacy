"""Fixed constants for Coding Cave. Nothing here is configurable."""

import re

VERSION = "@VERSION@"

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
