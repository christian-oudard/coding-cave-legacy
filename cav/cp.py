"""Copy files between the host and a cave agent worktree."""

import os
import shutil
import stat
import sys
from pathlib import Path

from . import config, git, registry


def _copy_nofollow(src: str, dst: str, **_kwargs) -> None:
    """Copy a regular file, refusing to follow symlinks (TOCTOU-safe).

    Opens the source with O_NOFOLLOW so that even if the agent replaces
    a regular file with a symlink between our is_symlink() check and
    this open, the open fails instead of following the symlink.
    """
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        # O_NOFOLLOW raises ELOOP on symlinks. Also skip other errors.
        return
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return
        dst_obj = Path(dst)
        if dst_obj.exists() and not os.access(dst, os.W_OK):
            dst_obj.unlink()
        with open(fd, "rb", closefd=False) as src_f:
            with open(dst, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
        shutil.copystat(src, dst, follow_symlinks=False)
    finally:
        os.close(fd)


def _norm(base: Path, rel: str) -> Path:
    """Normalize base/rel without following symlinks (resolves .. only)."""
    return Path(os.path.normpath(base / rel))


def _check_in_dir(path: Path, directory: Path) -> None:
    """Raise SystemExit if path is not inside directory.

    Resolves symlinks in existing ancestor components to catch symlink
    escapes (e.g. agent creates evil -> /etc in worktree, user copies
    to cave:evil/target which would write to /etc/target on host).
    """
    real_dir = str(directory.resolve())
    # Resolve what exists, keep the tail that doesn't exist yet.
    # This catches symlinks in intermediate directories.
    real_path = str(_resolve_existing(path))
    if real_path != real_dir and not real_path.startswith(real_dir + os.sep):
        raise SystemExit(f"Path escapes worktree: {path}")


def _resolve_existing(path: Path) -> Path:
    """Resolve symlinks in the existing prefix of a path.

    For /a/b/c where /a/b exists but c doesn't, resolves /a/b
    and appends c. This catches symlink escapes in intermediate
    directories without requiring the leaf to exist.
    """
    path = Path(os.path.normpath(path))
    if path.exists() or path.is_symlink():
        return path.resolve()
    # Walk up to find the existing ancestor, resolve it, append the rest
    parts = []
    current = path
    while not current.exists() and not current.is_symlink():
        parts.append(current.name)
        parent = current.parent
        if parent == current:
            break
        current = parent
    resolved = current.resolve()
    for part in reversed(parts):
        resolved = resolved / part
    return resolved


def _worktree_for_slug(slug: str, agent_id: str | None) -> Path:
    """Return the worktree directory for slug and optional agent_id."""
    if agent_id is not None:
        ws = config.worktree_dir(slug, agent_id)
        if not ws.exists():
            raise SystemExit(f"No worktree found for {slug}@{agent_id}.")
        return ws

    slug_dir = config.DATA_DIR / "worktrees" / slug
    if not slug_dir.exists():
        raise SystemExit(f"No worktrees found for project '{slug}'.")

    registered = set(registry.all_agent_ids(slug))
    agents = sorted([p for p in slug_dir.iterdir()
                     if p.is_dir() and p.name in registered],
                   key=lambda p: int(p.name) if p.name.isdigit() else p.name)
    if not agents:
        raise SystemExit(f"No worktrees found for project '{slug}'.")
    if len(agents) == 1:
        return agents[0]

    # Multiple agents: prompt the user to pick one.
    print(f"Multiple agents for '{slug}':", file=sys.stderr)
    by_key = {}
    for ws in agents:
        aid = ws.name
        state = "running" if registry.is_agent_running(aid) else "exited"
        print(f"  {aid}) {slug}@{aid}  [{state}]", file=sys.stderr)
        by_key[aid] = ws
    try:
        choice = input("Agent: ").strip()
        return by_key[choice]
    except (KeyError, EOFError):
        raise SystemExit("Invalid choice.")


def _cwd_info() -> tuple[str, str | None]:
    """Return (slug, agent_id | None) for the current working directory.

    Tries registered project paths first (agent_id=None, any worktree).
    Falls back to the current dev branch so that running cav cp from a
    repo checked out on a dev branch also works without explicit slug,
    and implicitly selects that agent's worktree.
    """
    cwd = Path.cwd().resolve()
    for slug, path in registry.all_projects():
        proj = Path(path).resolve()
        if cwd == proj or cwd.is_relative_to(proj):
            return slug, None

    try:
        branch = git.current_branch()
        parsed = config.parse_dev_branch(branch)
        if parsed:
            _, agent_id = parsed
            slug = registry.agent_project(agent_id)
            if slug:
                return slug, agent_id
    except Exception:
        pass

    raise SystemExit("CWD is not inside a registered project.")


def _resolve_cave_path(spec: str) -> tuple[Path, str] | tuple[None, None]:
    """Parse a cave path spec. Returns (host_path, slug) or (None, None).

    Spec forms:
      slug:path         project-relative path in agent worktree
      slug@N:path       specific agent N's worktree
      cave:path         auto-detect slug from CWD
      slug:~/path       persistent cave home (no worktree needed)
      slug:/abs/path    absolute path inside /projects/<slug>
    """
    if ":" not in spec:
        return None, None

    prefix, _, path = spec.partition(":")

    agent_id = None
    if "@" in prefix:
        prefix, id_str = prefix.rsplit("@", 1)
        try:
            agent_id = str(int(id_str))
        except ValueError:
            raise SystemExit(f"Invalid agent ID: {id_str}")

    if prefix == "cave":
        if agent_id is not None:
            # Explicit cave@N:path — look up slug from the registry, no CWD needed.
            slug = registry.agent_project(agent_id)
            if not slug:
                raise SystemExit(f"No agent {agent_id} found in registry.")
        else:
            # Auto-detect from CWD; may also return the agent_id from the branch.
            slug, detected_id = _cwd_info()
            agent_id = detected_id
    else:
        slug = prefix

    if path.startswith("~/") or path == "~":
        home = config.persistent_home()
        rel = path[2:] if path.startswith("~/") else ""
        result = _norm(home, rel) if rel else Path(os.path.normpath(home))
        if not str(result).startswith(str(Path(os.path.normpath(home))) + os.sep) \
                and str(result) != str(Path(os.path.normpath(home))):
            raise SystemExit(f"Path escapes cave home: {result}")
        return result, slug

    if path.startswith("/"):
        project_prefix = f"/projects/{slug}"
        if path == project_prefix or path.startswith(project_prefix + "/"):
            rel = path[len(project_prefix):].lstrip("/")
        else:
            raise SystemExit(f"Absolute cave path not under /projects/{slug}: {path}")
        ws = _worktree_for_slug(slug, agent_id)
        result = _norm(ws, rel) if rel else Path(os.path.normpath(ws))
        _check_in_dir(result, ws)
        return result, slug

    ws = _worktree_for_slug(slug, agent_id)
    result = _norm(ws, path) if path else Path(os.path.normpath(ws))
    _check_in_dir(result, ws)
    return result, slug


def cp(srcs: list[str], dest: str, recursive: bool = False, force: bool = False):
    """Copy files between host and a cave agent worktree."""
    dest_path, _dest_slug = _resolve_cave_path(dest)
    src_path, _src_slug = _resolve_cave_path(srcs[0]) if len(srcs) == 1 else (None, None)

    if dest_path is not None:
        # host -> cave
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if len(srcs) > 1 and not dest_path.is_dir():
            raise SystemExit(f"cp: base '{dest}': not a directory")
        for src in srcs:
            src_obj = Path(src).resolve()
            if not src_obj.exists():
                raise SystemExit(f"Source not found: {src}")
            if src_obj.is_dir():
                if not recursive:
                    raise SystemExit(f"cp: -R not specified; omitting directory '{src}'")
                base = dest_path / src_obj.name if dest_path.is_dir() else dest_path
            else:
                base = dest_path / src_obj.name if dest_path.is_dir() else dest_path
            if not force and base.exists():
                try:
                    answer = input(f"Overwrite {base}? [y/N] ").strip().lower()
                except EOFError:
                    answer = ""
                if answer != "y":
                    raise SystemExit("Aborted.")
            if src_obj.is_dir():
                shutil.copytree(src_obj, base, dirs_exist_ok=True)
            else:
                if base.is_symlink():
                    base.unlink()
                shutil.copy2(src_obj, base, follow_symlinks=False)
            print(f"  {src} -> {dest}", file=sys.stderr)

    elif src_path is not None:
        # cave -> host
        # Use O_NOFOLLOW to atomically reject symlinks, preventing TOCTOU
        # where the agent replaces a regular file with a symlink between
        # our check and the copy.
        if src_path.is_symlink():
            raise SystemExit(f"Refusing to copy symlink from cave: {srcs[0]}")
        if not src_path.exists():
            raise SystemExit(f"Source not found in cave: {srcs[0]}")
        host_dest = Path(dest).resolve()
        host_dest.parent.mkdir(parents=True, exist_ok=True)
        base = host_dest / src_path.name if host_dest.is_dir() else host_dest
        if src_path.is_dir() and not recursive:
            raise SystemExit(f"cp: -R not specified; omitting directory '{srcs[0]}'")
        if not force and base.exists():
            try:
                answer = input(f"Overwrite {base}? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer != "y":
                raise SystemExit("Aborted.")
        if src_path.is_dir():
            shutil.copytree(src_path, base, dirs_exist_ok=True,
                            copy_function=_copy_nofollow)
        else:
            _copy_nofollow(str(src_path), str(base))
        print(f"  {srcs[0]} -> {dest}", file=sys.stderr)

    else:
        raise SystemExit(
            "One of source or destination must use a cave path prefix.\n"
            "  cav cp <file>... <slug>:<path>        host -> project worktree\n"
            "  cav cp <file>... <slug>@N:<path>      host -> agent @N worktree\n"
            "  cav cp <file>... cave:<path>          host -> CWD project\n"
            "  cav cp <slug>:<path> <dest>           project worktree -> host\n"
            "  cav cp <slug>:~/path <dest>           cave home -> host\n"
        )
