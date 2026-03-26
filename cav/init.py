"""cav init: create the shared infrastructure."""

import subprocess
import sys
from pathlib import Path

from . import config, git


def install_hook(shared: Path):
    """Install pre-receive hook with correct python3 shebang."""
    hook_src = Path(__file__).parent / "hooks" / "pre-receive"
    hook_dst = shared / "hooks" / "pre-receive"
    content = hook_src.read_text()
    # Replace shebang with the current python3 interpreter
    content = content.replace(
        "#!/usr/bin/env python3",
        f"#!{sys.executable}",
        1,
    )
    hook_dst.write_text(content)
    hook_dst.chmod(0o755)


def init(path: str):
    base = Path(path).resolve()
    if not (base / ".git").exists():
        raise SystemExit(f"Not a git repository: {base}")

    cav = config.cav_dir(base)
    shared = config.shared_repo_path(base)

    if shared.exists():
        raise SystemExit(f"Already initialized: {shared}")

    # Check for conflicting remote before creating anything
    try:
        existing_url = git.git_output(
            "remote", "get-url", config.SHARED_REMOTE, cwd=base)
        raise SystemExit(
            f"Remote '{config.SHARED_REMOTE}' already exists, pointing to:\n"
            f"  {existing_url}\n"
            f"Remove it first: git remote remove {config.SHARED_REMOTE}")
    except subprocess.CalledProcessError:
        pass  # No conflicting remote, good

    # Create bare repo
    cav.mkdir(parents=True, exist_ok=True)
    git.git("init", "--bare", str(shared), echo=False)

    # Enable push options
    git.git("config", "receive.advertisePushOptions", "true", cwd=shared, echo=False)

    # Install pre-receive hook
    install_hook(shared)

    # Add cave remote
    git.git("remote", "add", config.SHARED_REMOTE, str(shared), cwd=base, echo=False)

    # Push all branches to cave
    git.git("push", "--no-set-upstream", config.SHARED_REMOTE, "--all", cwd=base, echo=False)

    print(f"Initialized Coding Cave at {cav}")
