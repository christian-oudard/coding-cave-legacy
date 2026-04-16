"""cav init: create the shared infrastructure."""

import os
import shutil
import sys
from pathlib import Path

from . import config, git, registry
from .constants import CAVE_SHARED_REMOTE, HOST_SHARED_REMOTE, HOST_SHARED_REPO_DIR


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


def _is_bare_repo(path: Path) -> bool:
    """True if path looks like a git bare repo (has HEAD or config)."""
    return path.is_dir() and ((path / "HEAD").exists() or (path / "config").exists())


def _publish_bases(base: Path):
    """Push base branches (not dev branches) to cave-shared.git.

    A user's main checkout has bases in refs/heads/. An agent
    workspace (marked by a host-cave remote) has only its dev branch
    there; its bases live under refs/remotes/host-cave/, mirrored from
    the parent shared repo. Fetch first if the source is a remote.
    """
    has_host_cave = git.git(
        "remote", "get-url", HOST_SHARED_REMOTE,
        cwd=base, check=False, capture=True, echo=False,
    ).returncode == 0
    if has_host_cave:
        git.git("fetch", HOST_SHARED_REMOTE, cwd=base,
                echo=False, check=False, capture=True)
        prefix = f"refs/remotes/{HOST_SHARED_REMOTE}/"
    else:
        prefix = "refs/heads/"
    refs = git.git_output(
        "for-each-ref", "--format=%(refname)", prefix, cwd=base)
    for ref in refs.splitlines():
        name = ref[len(prefix):]
        if name == "HEAD" or config.parse_dev_branch(name):
            continue
        git.git("push", "--no-set-upstream", CAVE_SHARED_REMOTE,
                f"{ref}:refs/heads/{name}",
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


def init(path: str, reinitialize: bool = False):
    from . import workspace

    base = Path(path).resolve()
    if not (base / ".git").exists():
        git.git("init", str(base))
        print()

    cav = config.cav_dir(base)
    shared = config.shared_repo_path(base)

    # Inside a cave, the parent's shared repo must be bind-mounted at
    # host-cave-shared.git. If it's missing, the cave wasn't launched
    # right; bail now rather than fail confusingly later.
    if config.inside_cave():
        host_shared = cav / HOST_SHARED_REPO_DIR
        if not host_shared.exists():
            raise SystemExit(
                f"{host_shared} missing. Expected a bind mount from the "
                f"parent cave. Relaunch via 'cav agent' on the host."
            )

    if _is_bare_repo(shared):
        if not reinitialize:
            print(f"Coding Cave already initialized at {cav}")
            return
        install_hooks(shared)
        git.git("config", "receive.advertisePushOptions", "true",
                cwd=shared, echo=False)
        git.git("config", "receive.shallowUpdate", "true",
                cwd=shared, echo=False)
        registry.register_project(base)
        workspace.reconfigure(base)
        _require_commits(base)
        _warn_dirty(base)
        _publish_bases(base)
        print(f"Coding Cave reinitialized at {cav}")
        return

    # Fresh init. Any directory at `shared` that isn't a real bare repo
    # (e.g. a pre-split stub) is in the way; remove it.
    if shared.exists():
        shutil.rmtree(shared)

    # Refuse if the user already has a 'cave' remote pointing elsewhere.
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
    git.git("config", "receive.advertisePushOptions", "true",
            cwd=shared, echo=False)
    git.git("config", "receive.shallowUpdate", "true",
            cwd=shared, echo=False)
    install_hooks(shared)
    git.git("remote", "add", CAVE_SHARED_REMOTE, str(shared),
            cwd=base, echo=False)

    _warn_dirty(base)
    _publish_bases(base)
    registry.register_project(base)
    print(f"Coding Cave initialized at {cav}")

    if not config.inside_cave():
        _offer_cave_nix()
