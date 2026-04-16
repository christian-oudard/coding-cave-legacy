"""cav init: create the shared infrastructure."""

import os
import sys
from pathlib import Path

from . import config, git, registry
from .constants import CAVE_SHARED_REMOTE, HOST_SHARED_REPO_DIR


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


def _ensure_alternates(base: Path):
    """Migrate pre-split workspace alternates to the host-cave-shared.git path.

    No-op for full clones (no alternates) and already-correct workspaces.
    Idempotent: safe to call on every init.
    """
    alt = base / ".git" / "objects" / "info" / "alternates"
    if not alt.exists():
        return
    expected = f"../../.coding-cave/{HOST_SHARED_REPO_DIR}/objects"
    if alt.read_text().strip() == expected:
        return
    shared = config.shared_repo_path(base)
    mount_point = base / ".coding-cave" / HOST_SHARED_REPO_DIR
    mount_point.mkdir(parents=True, exist_ok=True)
    objects_link = mount_point / "objects"
    if not objects_link.exists() and not objects_link.is_symlink():
        objects_link.symlink_to(shared / "objects")
    alt.write_text(expected + "\n")


def _remove_cave_shared_stub(base: Path):
    """Remove pre-split cave-shared.git stubs left behind inside a cave.

    Before the two-repo split, cave-shared.git was a bind mount of the
    host's shared repo. Post-split, host-cave-shared.git plays that role
    and the old directory survives as an unusable stub (broken objects
    symlink). Inside a cave, a real cave-shared.git is only needed when
    spawning subcaves, so the stub is just dead weight.

    Only removes stubs (no HEAD/config). Real bare repos are left alone.
    """
    import shutil
    stub = config.shared_repo_path(base)
    if not stub.exists():
        return
    if (stub / "HEAD").exists() or (stub / "config").exists():
        return
    shutil.rmtree(stub)


def _push_shared_branches(base: Path):
    """Push only base branches (not dev branches) to the shared remote."""
    branches = git.git_output(
        "for-each-ref", "--format=%(refname:short)", "refs/heads/",
        cwd=base)
    for branch in branches.splitlines():
        if config.parse_dev_branch(branch):
            continue
        git.git("push", "--no-set-upstream", CAVE_SHARED_REMOTE, branch,
                cwd=base, echo=False, check=False, capture=True)


def _offer_cave_nix():
    """Offer to create a default cave.nix if none exists."""
    cave_nix = Path(os.environ.get("CODING_CAVE_HOST_HOME", Path.home())) / ".config" / "coding-cave" / "cave.nix"
    if cave_nix.exists() or not sys.stdin.isatty():
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

    if config.inside_cave():
        # Inside a cave, the parent's shared repo is bind-mounted at
        # host-cave-shared.git. Our own cave-shared.git is only needed
        # when spawning subcaves (lazily created in workspace.create).
        host_shared = cav / HOST_SHARED_REPO_DIR
        if not host_shared.exists():
            raise SystemExit(
                f"{host_shared} missing. Expected a bind mount from the "
                f"parent cave. Relaunch via 'cav agent' on the host."
            )
        _ensure_alternates(base)
        _remove_cave_shared_stub(base)
        registry.register_project(base)
        print(f"Coding Cave initialized at {cav}")
        return

    shared = config.shared_repo_path(base)
    if shared.exists():
        install_hooks(shared)
        git.git("config", "receive.advertisePushOptions", "true",
                cwd=shared, echo=False)
        git.git("config", "receive.shallowUpdate", "true",
                cwd=shared, echo=False)
        registry.register_project(base)
        _ensure_alternates(base)
        # Re-sync: push any commits added since last init.
        _require_commits(base)
        _warn_dirty(base)
        _push_shared_branches(base)
        print(f"Coding Cave initialized at {cav}")
        return

    # Check for conflicting remote before creating anything
    r = git.git("remote", "get-url", CAVE_SHARED_REMOTE,
                cwd=base, check=False, capture=True, echo=False)
    if r.returncode == 0:
        raise SystemExit(
            f"Remote '{CAVE_SHARED_REMOTE}' already exists, pointing to:\n"
            f"  {r.stdout.strip()}\n"
            f"Remove it first: git remote remove {CAVE_SHARED_REMOTE}")

    _require_commits(base)

    cav.mkdir(parents=True, exist_ok=True)
    git.git("init", "--bare", str(shared), echo=False)

    # Enable push options and shallow updates (for shallow clones)
    git.git("config", "receive.advertisePushOptions", "true", cwd=shared, echo=False)
    git.git("config", "receive.shallowUpdate", "true", cwd=shared, echo=False)

    # Install hooks
    install_hooks(shared)

    # Add shared remote
    git.git("remote", "add", CAVE_SHARED_REMOTE, str(shared), cwd=base, echo=False)

    _warn_dirty(base)
    _push_shared_branches(base)

    registry.register_project(base)
    print(f"Coding Cave initialized at {cav}")

    _offer_cave_nix()
