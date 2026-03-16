"""Shared test fixtures."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_cave import _state


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t",
}


def git_run(*args, **kwargs):
    """Run a git command with test identity."""
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(list(args), capture_output=True, text=True, env=env, **kwargs)


def git_head(path, gitdir=False):
    if gitdir:
        return git_run("git", "--git-dir", str(path), "rev-parse", "HEAD").stdout.strip()
    return git_run("git", "-C", str(path), "rev-parse", "HEAD").stdout.strip()


def make_repo(path, branch="main"):
    """Create a git repo with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    git_run("git", "-C", str(path), "init", "-b", branch)
    git_run("git", "-C", str(path), "config", "user.email", "test@test.com")
    git_run("git", "-C", str(path), "config", "user.name", "Test")
    (path / "file.txt").write_text("initial\n")
    git_run("git", "-C", str(path), "add", ".")
    git_run("git", "-C", str(path), "commit", "-m", "initial")
    return path


def write_projects(cave_data, projects):
    """Write a projects.json into the test cave_data."""
    cave_data.mkdir(parents=True, exist_ok=True)
    with open(cave_data / "projects.json", "w") as f:
        json.dump(projects, f)


def write_session(cave_data, sid, project, pid=None):
    """Write a session file into the test cave_data."""
    sessions_dir = cave_data / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    import time
    with open(sessions_dir / f"{sid}.json", "w") as f:
        json.dump({
            "sid": sid,
            "project": project,
            "bwrap_pid": pid or os.getpid(),
            "started": time.time(),
        }, f)


@pytest.fixture(autouse=True)
def _reset_cave_data():
    """Reset cave_data after every test."""
    yield
    _state.set_cave_data(None)


@pytest.fixture
def cave_env(tmp_path):
    """Minimal cave environment pointing at a temp dir."""
    cave_data = tmp_path / "cave-data"
    (cave_data / "projects").mkdir(parents=True)
    (cave_data / "sessions").mkdir(parents=True)
    _state.set_cave_data(cave_data)

    # Unset cave git identity so tests use repo config
    old_env = {}
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        old_env[var] = os.environ.pop(var, None)

    # Suppress cave-send in post-commit hooks so tests don't
    # spam real sessions with notifications.
    os.environ["CODING_CAVE_NO_SEND"] = "1"

    yield SimpleNamespace(
        cave_data=cave_data,
        projects_dir=cave_data / "projects",
        sessions_dir=cave_data / "sessions",
        projects_file=cave_data / "projects.json",
        tmp_path=tmp_path,
    )

    os.environ.pop("CODING_CAVE_NO_SEND", None)
    for var, val in old_env.items():
        if val is not None:
            os.environ[var] = val
