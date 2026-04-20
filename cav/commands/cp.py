"""Copy files between the host and a cave agent worktree."""

import glob as _glob
import os
import shutil
import stat
import sys
from pathlib import Path

from .. import config, registry
from ..git import git


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

    Strict: a leaf symlink pointing outside also fails. Use this for
    source paths and glob matches, where following the link would read
    host data. For destinations (where we unlink-and-replace before
    writing), use _check_dest_in_dir instead.
    """
    real_dir = str(directory.resolve())
    # Resolve what exists, keep the tail that doesn't exist yet.
    # This catches symlinks in intermediate directories.
    real_path = str(_resolve_existing(path))
    if real_path != real_dir and not real_path.startswith(real_dir + os.sep):
        raise SystemExit(f"Path escapes worktree: {path}")


def _check_dest_in_dir(path: Path, directory: Path) -> None:
    """Like _check_in_dir but treats the leaf component literally.

    Host-to-cave copies unlink any existing leaf symlink before writing
    (cp() does base.unlink() + copy2 with follow_symlinks=False), so the
    write lands at the literal path regardless of what the leaf used to
    point to. Following a leaf symlink here would wrongly reject the
    legitimate "overwrite a symlink with real content" case.

    Intermediate symlinks are still resolved and escapes still rejected,
    so `ws/evil/target` with `evil -> /etc` still fails.
    """
    real_dir = str(directory.resolve())
    path = Path(os.path.normpath(path))
    parent = path.parent
    if parent == path:
        # Degenerate root path; fall back to strict check.
        _check_in_dir(path, directory)
        return
    parent_real = _resolve_existing(parent)
    final = os.path.normpath(str(parent_real / path.name))
    if final != real_dir and not final.startswith(real_dir + os.sep):
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


def _worktree_for_slug(slug: str, agent_id: str | None) -> tuple[Path, str]:
    """Return (worktree_dir, agent_id) for slug and optional agent_id.

    Prompts when slug has multiple registered agents and agent_id is None.
    """
    if agent_id is not None:
        ws = config.worktree_dir(slug, agent_id)
        if not ws.exists():
            raise SystemExit(f"No worktree found for {slug}@{agent_id}.")
        return ws, agent_id

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
        return agents[0], agents[0].name

    aid = _pick_agent_prompt(slug, [p.name for p in agents])
    return config.worktree_dir(slug, aid), aid


def _pick_agent_prompt(slug: str, agent_ids: list[str]) -> str:
    """Prompt the user to pick one of agent_ids for slug."""
    print(f"Multiple agents for '{slug}':", file=sys.stderr)
    for aid in agent_ids:
        state = "running" if registry.is_agent_running(aid) else "exited"
        print(f"  {aid}) {slug}@{aid}  [{state}]", file=sys.stderr)
    try:
        choice = input("Agent: ").strip()
    except EOFError:
        raise SystemExit("Invalid choice.")
    if choice not in agent_ids:
        raise SystemExit("Invalid choice.")
    return choice


def _cave_root_for_slug(slug: str, agent_id: str | None) -> tuple[Path, str]:
    """Return (/proc/<cave_pid>/root, agent_id) for a running cave.

    Picks an agent from slug that has a live cave_pid (i.e. the bwrap
    child is up). Prompts if multiple candidates. Raises SystemExit with
    a clear message if no agent is running or /proc is unreachable.
    """
    candidates = [
        aid for aid in registry.all_agent_ids(slug)
        if registry.is_agent_running(aid) and registry.cave_pid(aid) is not None
    ]
    if agent_id is not None:
        if agent_id not in candidates:
            raise SystemExit(
                f"Agent {slug}@{agent_id} is not running. Cave-internal "
                f"absolute paths require a live cave (only /projects/"
                f"{slug}/... is accessible on exited agents)."
            )
        chosen = agent_id
    elif not candidates:
        raise SystemExit(
            f"No running cave for project '{slug}'. Cave-internal absolute "
            f"paths require a live cave."
        )
    elif len(candidates) == 1:
        chosen = candidates[0]
    else:
        chosen = _pick_agent_prompt(slug, candidates)

    pid = registry.cave_pid(chosen)
    cave_root = Path(f"/proc/{pid}/root")
    # Smoke-test access: if our PID namespace can't see the target,
    # stat() on the magic link fails with ENOENT or EACCES.
    try:
        os.stat(cave_root)
    except FileNotFoundError:
        raise SystemExit(
            f"Cannot reach cave {slug}@{chosen} (pid {pid}) via /proc. "
            f"cav cp for cave-internal paths only works from the host; "
            f"from inside another cave the target's PID namespace is "
            f"not visible."
        )
    except PermissionError:
        raise SystemExit(
            f"Permission denied on /proc/{pid}/root. "
            f"Cave {slug}@{chosen} runs as a different user."
        )
    return cave_root, chosen


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


def _resolve_cave_path(
    spec: str, is_dest: bool = False
) -> tuple[Path | None, str | None, str | None]:
    """Parse a cave path spec. Returns (host_path, slug, agent_id).

    Returns (None, None, None) if spec has no cave prefix.

    Spec forms:
      slug:path         project-relative path in agent worktree
      slug@N:path       specific agent N's worktree
      cave:path         auto-detect slug from CWD
      slug:~/path       persistent cave home (no worktree needed)
      slug:/abs/path    absolute path; if under /projects/<slug>, resolves
                        to the worktree. Otherwise reaches into the
                        running cave via /proc/<cave_pid>/root/<path>.

    agent_id in the return value is the agent ultimately selected, even
    for forms where the caller didn't specify one (e.g. via the picker
    or a single-agent auto-pick). It's None for home-only paths which
    don't bind to any agent.

    is_dest: when True, the path will be written to (host -> cave
    direction). A leaf symlink is tolerated because cp() unlinks it
    before writing. When False (default), leaf symlinks are rejected
    as escapes.
    """
    if ":" not in spec:
        return None, None, None

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
        return result, slug, None

    check = _check_dest_in_dir if is_dest else _check_in_dir

    if path.startswith("/"):
        project_prefix = f"/projects/{slug}"
        if path == project_prefix or path.startswith(project_prefix + "/"):
            rel = path[len(project_prefix):].lstrip("/")
            ws, chosen = _worktree_for_slug(slug, agent_id)
            result = _norm(ws, rel) if rel else Path(os.path.normpath(ws))
            check(result, ws)
            return result, slug, chosen

        # Cave-internal absolute path: reach in via /proc/<cave_pid>/root.
        # The kernel's pathwalk resolves the path inside the target's
        # mount namespace, so symlinks and .. stay contained. We still
        # normalize client-side to strip any leading .. that would
        # otherwise be meaningless here.
        cave_root, chosen = _cave_root_for_slug(slug, agent_id)
        rel = os.path.normpath("/" + path).lstrip("/")
        result = cave_root / rel if rel else cave_root
        return result, slug, chosen

    ws, chosen = _worktree_for_slug(slug, agent_id)
    result = _norm(ws, path) if path else Path(os.path.normpath(ws))
    check(result, ws)
    return result, slug, chosen


def _complete_cp_path(ctx, param, incomplete: str):
    """Click completer for `cav cp` paths.

    No `:` in the partial -> emit a file marker so the shell does its
    own filename completion. With `:`, parse the cave prefix and list
    matching entries from the worktree (or persistent home for `~/`).
    Errors during completion are swallowed: a missing project, dead
    worktree, etc. just produce no suggestions instead of a traceback.
    """
    from click.shell_completion import CompletionItem

    if ":" not in incomplete:
        return [CompletionItem(incomplete, type="file")]

    prefix, _, path_part = incomplete.partition(":")
    p = prefix
    agent_id = None
    if "@" in p:
        p, id_str = p.rsplit("@", 1)
        if not id_str.isdigit():
            return []
        agent_id = id_str

    try:
        slug = _completer_slug(p, agent_id)
        if slug is None:
            return []
        if p == "cave" and agent_id is None:
            # cave: may resolve an agent_id from the dev branch
            agent_id = _completer_branch_agent_id(slug)

        if path_part.startswith("~"):
            base_dir = config.persistent_home()
            if path_part == "~":
                rel = ""
                marker = "~"
            elif path_part.startswith("~/"):
                rel = path_part[2:]
                marker = "~/"
            else:
                return []
        else:
            project_prefix = f"/projects/{slug}"
            if (path_part.startswith("/")
                    and path_part != project_prefix
                    and not path_part.startswith(project_prefix + "/")):
                # Cave-internal absolute path: list from /proc/<cave_pid>/root.
                cave_root = _completer_cave_root(slug, agent_id)
                if cave_root is None:
                    return []
                base_dir = cave_root
                rel = path_part.lstrip("/")
                marker = "/"
            else:
                ws = _completer_worktree(slug, agent_id)
                if ws is None:
                    return []
                base_dir = ws
                # Treat /projects/<slug>/... as worktree-relative for listing.
                if path_part == project_prefix or path_part.startswith(project_prefix + "/"):
                    rel = path_part[len(project_prefix):].lstrip("/")
                    marker = f"{project_prefix}/"
                else:
                    rel = path_part.lstrip("/")
                    marker = "/" if path_part.startswith("/") else ""
    except Exception:
        return []

    if "/" in rel:
        head, _, tail = rel.rpartition("/")
        list_dir = base_dir / head
        head_with_slash = head + "/"
    else:
        head_with_slash = ""
        tail = rel
        list_dir = base_dir

    try:
        if not list_dir.is_dir():
            return []
        entries = sorted(list_dir.iterdir())
    except OSError:
        return []

    items = []
    for entry in entries:
        if not entry.name.startswith(tail):
            continue
        if entry.name.startswith(".") and not tail.startswith("."):
            continue
        sep = "/" if entry.is_dir() else ""
        value = f"{prefix}:{marker}{head_with_slash}{entry.name}{sep}"
        items.append(CompletionItem(value))
    return items


def _completer_slug(prefix_word: str, agent_id: str | None) -> str | None:
    """Resolve a project slug for completion. Never prompts."""
    if prefix_word == "cave":
        if agent_id is not None:
            return registry.agent_project(agent_id)
        cwd = Path.cwd().resolve()
        for s, path in registry.all_projects():
            proj = Path(path).resolve()
            if cwd == proj or cwd.is_relative_to(proj):
                return s
        try:
            branch = git.current_branch()
        except Exception:
            return None
        parsed = config.parse_dev_branch(branch)
        if parsed:
            return registry.agent_project(parsed[1])
        return None
    if registry.project_path(prefix_word):
        return prefix_word
    return None


def _completer_branch_agent_id(slug: str) -> str | None:
    """Return the agent_id implied by the current dev branch, if any."""
    try:
        branch = git.current_branch()
    except Exception:
        return None
    parsed = config.parse_dev_branch(branch)
    if parsed and registry.agent_project(parsed[1]) == slug:
        return parsed[1]
    return None


def _completer_worktree(slug: str, agent_id: str | None) -> Path | None:
    """Return a worktree to list for completion. Never prompts.

    With multiple agents and no agent_id given, pick the lowest-numbered
    one rather than asking. The user can disambiguate by typing `slug@N:`.
    """
    if agent_id is not None:
        ws = config.worktree_dir(slug, agent_id)
        return ws if ws.exists() else None
    slug_dir = config.DATA_DIR / "worktrees" / slug
    if not slug_dir.exists():
        return None
    registered = set(registry.all_agent_ids(slug))
    candidates = [p for p in slug_dir.iterdir()
                  if p.is_dir() and p.name in registered]
    if not candidates:
        return None
    candidates.sort(key=lambda p: int(p.name) if p.name.isdigit() else p.name)
    return candidates[0]


def _completer_cave_root(slug: str, agent_id: str | None) -> Path | None:
    """Return /proc/<cave_pid>/root for completion. Never prompts.

    Picks the lowest-numbered running agent with a live cave_pid. With an
    explicit agent_id, requires that specific agent to be running. Returns
    None (suppressing suggestions) if no cave is reachable, since the
    completer swallows errors rather than erroring at the shell.
    """
    candidates = [
        aid for aid in registry.all_agent_ids(slug)
        if registry.is_agent_running(aid) and registry.cave_pid(aid) is not None
    ]
    if not candidates:
        return None
    if agent_id is not None:
        if agent_id not in candidates:
            return None
        chosen = agent_id
    else:
        chosen = sorted(candidates, key=int)[0]
    pid = registry.cave_pid(chosen)
    root = Path(f"/proc/{pid}/root")
    try:
        os.stat(root)
    except OSError:
        return None
    return root


def _parse_path_component(p: str) -> tuple[str, str, bool]:
    """Process backslash escapes in a single path component.

    Returns (literal, pattern, has_unescaped_magic):
      - literal: the component with \\X collapsed to X (used for literal lookup)
      - pattern: the component with \\X collapsed to glob.escape(X) — i.e.
        magic chars become a single-char class like [*] so Path.glob treats
        them literally — and unescaped magic kept as-is
      - has_unescaped_magic: True iff *, ?, or [ appears unescaped
    """
    literal: list[str] = []
    pattern: list[str] = []
    has_magic = False
    i = 0
    while i < len(p):
        c = p[i]
        if c == "\\" and i + 1 < len(p):
            nxt = p[i + 1]
            literal.append(nxt)
            pattern.append(_glob.escape(nxt))
            i += 2
        elif c in "*?[":
            literal.append(c)
            pattern.append(c)
            has_magic = True
            i += 1
        else:
            literal.append(c)
            pattern.append(c)
            i += 1
    return "".join(literal), "".join(pattern), has_magic


def _expand_cave_glob(spec: str) -> list[str]:
    """Expand glob characters in a cave path spec.

    Returns a list of cave specs (one per match). If the spec has no
    unescaped glob characters or no cave prefix, returns [spec] unchanged
    (with backslash escapes stripped, if any). Raises SystemExit if a
    glob matches nothing.

    Backslash escapes the next character: cave:foo\\*.txt looks up a file
    literally named "foo*.txt". Use single quotes on the shell to keep
    the backslash from being eaten by the shell.
    """
    if ":" not in spec:
        return [spec]
    prefix, _, path = spec.partition(":")

    parsed = [_parse_path_component(p) for p in path.split("/")]
    if not any(has for _, _, has in parsed):
        # No unescaped magic. Strip escapes and return as a single literal.
        literal_path = "/".join(lit for lit, _, _ in parsed)
        if literal_path == path:
            return [spec]
        return [f"{prefix}:{literal_path}"]

    # Split at the first component containing unescaped magic. Static
    # prefix uses literal forms (so escaped magic resolves correctly),
    # the glob remainder uses the escape-aware pattern form.
    split_at = next(i for i, (_, _, has) in enumerate(parsed) if has)
    static_path = "/".join(lit for lit, _, _ in parsed[:split_at])
    if not static_path:
        static_path = "~" if path.startswith("~") else "."
    pattern = "/".join(pat for _, pat, _ in parsed[split_at:])

    base_host, _slug, chosen_id = _resolve_cave_path(f"{prefix}:{static_path}")
    if not base_host.is_dir():
        raise SystemExit(f"Not a directory: {prefix}:{static_path}")

    matches = sorted(base_host.glob(pattern))
    if not matches:
        raise SystemExit(f"No matches for: {spec}")

    # Reject symlink escapes on worktree-based globs. For /proc/<pid>/root
    # paths the kernel's pathwalk already contains symlinks within the
    # cave's mount namespace, so no client-side check is needed (and
    # Path.resolve would give wrong answers because it walks symlinks
    # using host-side semantics).
    is_proc_path = str(base_host).startswith("/proc/")
    if not is_proc_path:
        for m in matches:
            _check_in_dir(m, base_host)

    # Encode the selected agent into the expanded specs so subsequent
    # _resolve_cave_path calls don't re-prompt the picker. Home paths
    # don't bind to any agent, so chosen_id is None there.
    out_prefix = prefix
    if "@" not in prefix and chosen_id is not None:
        out_prefix = f"{prefix}@{chosen_id}"

    results = []
    for m in matches:
        rel = m.relative_to(base_host).as_posix()
        joined = rel if static_path == "." else f"{static_path}/{rel}"
        results.append(f"{out_prefix}:{joined}")
    return results


def cp(srcs: list[str], dest: str, recursive: bool = False, force: bool = False):
    """Copy files between host and a cave agent worktree."""
    expanded: list[str] = []
    for src in srcs:
        expanded.extend(_expand_cave_glob(src))
    srcs = expanded

    # The destination is about to be written to (host -> cave direction),
    # so a leaf symlink is fine: cp() unlinks it before copying. Sources
    # use the strict check.
    dest_path, _dest_slug, _dest_aid = _resolve_cave_path(dest, is_dest=True)
    # Direction is determined by the first src: if it has a cave prefix,
    # we're copying cave -> host regardless of how many globbed matches.
    first_src_path, _src_slug, _src_aid = _resolve_cave_path(srcs[0])

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

    elif first_src_path is not None:
        # cave -> host
        host_dest = Path(dest).resolve()
        host_dest.parent.mkdir(parents=True, exist_ok=True)
        if len(srcs) > 1 and not host_dest.is_dir():
            raise SystemExit(f"cp: base '{dest}': not a directory")
        for src in srcs:
            src_path, _, _ = _resolve_cave_path(src)
            # Use O_NOFOLLOW to atomically reject symlinks, preventing TOCTOU
            # where the agent replaces a regular file with a symlink between
            # our check and the copy.
            if src_path.is_symlink():
                raise SystemExit(f"Refusing to copy symlink from cave: {src}")
            if not src_path.exists():
                raise SystemExit(f"Source not found in cave: {src}")
            base = host_dest / src_path.name if host_dest.is_dir() else host_dest
            if src_path.is_dir() and not recursive:
                raise SystemExit(f"cp: -R not specified; omitting directory '{src}'")
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
            print(f"  {src} -> {dest}", file=sys.stderr)

    else:
        raise SystemExit(
            "One of source or destination must use a cave path prefix.\n"
            "  cav cp <file>... <slug>:<path>        host -> project worktree\n"
            "  cav cp <file>... <slug>@N:<path>      host -> agent @N worktree\n"
            "  cav cp <file>... cave:<path>          host -> CWD project\n"
            "  cav cp <slug>:<path> <dest>           project worktree -> host\n"
            "  cav cp <slug>:~/path <dest>           cave home -> host\n"
        )
