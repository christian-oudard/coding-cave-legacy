"""cav init: create the shared infrastructure."""

import os
import subprocess
import sys
from pathlib import Path

from . import config, git, registry


def install_hooks(shared: Path):
    """Install git hooks with correct python3 shebang."""
    hooks_dir = Path(__file__).parent / "hooks"
    for name in ("pre-receive", "post-receive"):
        src = hooks_dir / name
        dst = shared / "hooks" / name
        content = src.read_text()
        content = content.replace(
            "#!/usr/bin/env python3",
            f"#!{sys.executable}",
            1,
        )
        dst.write_text(content)
        dst.chmod(0o755)


def _require_commits(base: Path):
    """Error if the repo has no commits."""
    has_refs = git.git("rev-parse", "HEAD", cwd=base, check=False, capture=True, echo=False).returncode == 0
    if not has_refs:
        raise SystemExit(
            "No commits yet. Commit your files first:\n"
            "  git add -A && git commit -m 'Initial commit'")


def _warn_dirty(base: Path):
    """Warn if the working tree has uncommitted changes."""
    dirty = git.git("status", "--porcelain", "--ignore-submodules",
                    cwd=base, capture=True, echo=False).stdout
    # Filter out .coding-cave/ which we just created.
    dirty = "\n".join(
        l for l in dirty.splitlines()
        if not l.endswith(".coding-cave/") and not l.endswith(".coding-cave")
    ).strip()
    if dirty:
        print("Warning: uncommitted changes will not be visible to agents.", file=sys.stderr)
        print("Commit them first, or use 'cav cp' to copy files in later.\n", file=sys.stderr)


def _push_shared_branches(base: Path):
    """Push only base branches (not dev branches) to the shared remote."""
    branches = git.git_output(
        "for-each-ref", "--format=%(refname:short)", "refs/heads/",
        cwd=base)
    for branch in branches.splitlines():
        if config.parse_dev_branch(branch):
            continue
        git.git("push", "--no-set-upstream", config.CAVE_SHARED_REMOTE, branch,
                cwd=base, echo=False, check=False, capture=True)


def _offer_cave_nix():
    """Offer to create a default cave.nix if none exists."""
    cave_nix = Path(os.environ.get("CODING_CAVE_HOST_HOME", Path.home())) / ".config" / "coding-cave" / "cave.nix"
    if cave_nix.exists():
        return
    try:
        answer = input("Create default cave.nix? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if answer and answer != "y":
        return
    from . import user_env
    cave_nix.parent.mkdir(parents=True, exist_ok=True)
    cave_nix.write_text(user_env.default_cave_nix())
    print(f"Created {cave_nix}")


def init(path: str):
    base = Path(path).resolve()
    if not (base / ".git").exists():
        git.git("init", str(base))
        print()

    cav = config.cav_dir(base)
    shared = config.shared_repo_path(base)

    if shared.exists():
        install_hooks(shared)
        git.git("config", "receive.advertisePushOptions", "true",
                cwd=shared, echo=False)
        registry.register_project(base)
        # Re-sync: push any commits added since last init.
        _require_commits(base)
        _warn_dirty(base)
        _push_shared_branches(base)
        print(f"Coding Cave initialized at {cav}")
        return

    # Check for conflicting remote before creating anything
    try:
        existing_url = git.git_output(
            "remote", "get-url", config.CAVE_SHARED_REMOTE, cwd=base)
        raise SystemExit(
            f"Remote '{config.CAVE_SHARED_REMOTE}' already exists, pointing to:\n"
            f"  {existing_url}\n"
            f"Remove it first: git remote remove {config.CAVE_SHARED_REMOTE}")
    except subprocess.CalledProcessError:
        pass  # No conflicting remote, good

    _require_commits(base)

    # Create bare repo
    cav.mkdir(parents=True, exist_ok=True)
    git.git("init", "--bare", str(shared), echo=False)

    # Enable push options
    git.git("config", "receive.advertisePushOptions", "true", cwd=shared, echo=False)

    # Install hooks
    install_hooks(shared)

    # Add shared remote
    git.git("remote", "add", config.CAVE_SHARED_REMOTE, str(shared), cwd=base, echo=False)

    _warn_dirty(base)
    _push_shared_branches(base)

    registry.register_project(base)
    print(f"Coding Cave initialized at {cav}")

    _offer_cave_nix()
