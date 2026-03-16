"""Project registry management."""
import json
import re
import subprocess
import sys
from pathlib import Path

from . import _state, git as git_mod


def load_projects():
    """Load the project registry (slug -> source path)."""
    if _state.projects_file().exists():
        with open(_state.projects_file()) as f:
            return json.load(f)
    return {}


def save_projects(projects):
    _state.cave_data().mkdir(parents=True, exist_ok=True)
    with open(_state.projects_file(), "w") as f:
        json.dump(projects, f, indent=2, sort_keys=True)


def list_projects():
    return list(load_projects().items())


def slugify(text, max_len=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


def detect_project_from_cwd():
    cwd = Path.cwd().resolve()
    if cwd == Path.home().resolve():
        return None
    for _slug, source in list_projects():
        source_path = Path(source).resolve()
        if cwd == source_path or cwd.is_relative_to(source_path):
            return source_path
    try:
        answer = input(f"Add {cwd}/ to the coding cave? [Y/n] ").strip().lower()
    except EOFError:
        return None
    if answer in ("", "y", "yes"):
        return cwd
    sys.exit(0)


def ensure_project(project_dir):
    """Add a project to the registry and create overlay dirs. Returns slug."""
    project_dir = Path(project_dir).resolve()
    slug = project_dir.name
    proj_state = _state.projects_dir() / slug

    (proj_state / "upper").mkdir(parents=True, exist_ok=True)
    (proj_state / "work").mkdir(parents=True, exist_ok=True)

    # Every project must have a git repo
    if not (project_dir / ".git").is_dir():
        subprocess.run(
            ["git", "init", "-b", "main", str(project_dir)],
            check=True, capture_output=True,
        )

    git_mod.initialize_gitdir(slug, project_dir)

    projects = load_projects()
    projects[slug] = str(project_dir)
    save_projects(projects)

    return slug


def cave_workdir(project):
    """Compute the working directory inside the cave based on host CWD."""
    cwd = Path.cwd().resolve()
    projects = load_projects()
    source = Path(projects.get(project, "")).resolve()
    try:
        rel = cwd.relative_to(source)
    except ValueError:
        return f"/projects/{project}"
    return f"/projects/{project}/{rel}" if str(rel) != "." else f"/projects/{project}"


def resolve_slug(slug=None):
    """Resolve a project slug or detect from CWD. Returns slug or None."""
    if slug:
        return slug
    cwd = Path.cwd().resolve()
    for slug, source in load_projects().items():
        if cwd == Path(source).resolve() or cwd.is_relative_to(Path(source).resolve()):
            return slug
    return None
