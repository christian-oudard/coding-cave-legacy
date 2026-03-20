"""Tests for repo-local hook chaining in cave hooks."""
import os
import subprocess

import pytest

import wrapper
from coding_cave import _state
from tests.conftest import GIT_ENV, git_run, make_repo


def make_cave_session(gitdir, workdir):
    """Set up a working directory to use a gitdir (simulates .git file)."""
    (workdir / ".git").write_text(f"gitdir: {gitdir}\n")


def cave_git(workdir, *args, **kwargs):
    """Run git in a cave working directory with test identity."""
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git", "-C", str(workdir), "-c", "commit.gpgsign=false"] + list(args),
        capture_output=True, text=True, env=env, **kwargs,
    )


def _make_project(tmp_path, slug, hooks_path=".githooks"):
    """Create a host project with core.hooksPath set, plus a session."""
    _state.set_cave_data(tmp_path / "cave-data")
    projects_dir = _state.projects_dir()
    (projects_dir / slug).mkdir(parents=True)

    host = make_repo(tmp_path / "host")
    git_run("git", "-C", str(host), "config", "core.hooksPath", hooks_path)
    wrapper.initialize_gitdir(slug, host)

    overlay = tmp_path / "overlay"
    overlay.mkdir()
    session_gd = wrapper.create_session_gitdir(slug, "test-sid", overlay,
                                               source_dir=host)

    workdir = tmp_path / "work"
    workdir.mkdir()
    git_run("git", "--git-dir", str(session_gd), "--work-tree", str(workdir),
            "checkout", "HEAD", "--", ".")
    make_cave_session(session_gd, workdir)

    return type("NS", (), dict(
        host=host, slug=slug, session_gd=session_gd,
        workdir=workdir, overlay=overlay, tmp_path=tmp_path,
        hooks_path=hooks_path,
    ))()


class TestHookChaining:

    def test_pre_commit_runs(self, tmp_path):
        """Repo pre-commit hook runs during commit."""
        ns = _make_project(tmp_path, "test-project")
        marker = ns.tmp_path / "pre-commit-ran"

        hooks_dir = ns.workdir / ns.hooks_path
        hooks_dir.mkdir()
        (hooks_dir / "pre-commit").write_text(
            f"#!/bin/bash\ntouch {marker}\n"
        )
        (hooks_dir / "pre-commit").chmod(0o755)

        (ns.workdir / "new.txt").write_text("content\n")
        cave_git(ns.workdir, "add", "new.txt")
        cave_git(ns.workdir, "add", ns.hooks_path)
        result = cave_git(ns.workdir, "commit", "-m", "test commit")
        assert result.returncode == 0, result.stderr
        assert marker.exists(), "repo pre-commit was not chained"

    def test_pre_commit_blocks(self, tmp_path):
        """Repo pre-commit that exits 1 blocks the commit."""
        ns = _make_project(tmp_path, "test-project")

        hooks_dir = ns.workdir / ns.hooks_path
        hooks_dir.mkdir()
        (hooks_dir / "pre-commit").write_text(
            "#!/bin/bash\necho 'blocked by repo hook' >&2\nexit 1\n"
        )
        (hooks_dir / "pre-commit").chmod(0o755)

        (ns.workdir / "new.txt").write_text("content\n")
        cave_git(ns.workdir, "add", "new.txt")
        cave_git(ns.workdir, "add", ns.hooks_path)
        result = cave_git(ns.workdir, "commit", "-m", "should fail")
        assert result.returncode != 0
        assert "blocked by repo hook" in result.stderr

    def test_core_hookspath_forwarded(self, tmp_path):
        """Host core.hooksPath is saved to cave.repoHooksPath in session."""
        ns = _make_project(tmp_path, "test-project", hooks_path="myhooks")

        result = git_run("git", "--git-dir", str(ns.session_gd),
                         "config", "cave.repoHooksPath")
        assert result.stdout.strip() == "myhooks"

    def test_custom_hookspath_used(self, tmp_path):
        """Hooks from a non-default core.hooksPath directory are chained."""
        ns = _make_project(tmp_path, "test-project", hooks_path="myhooks")
        marker = ns.tmp_path / "hookspath-ran"

        hooks_dir = ns.workdir / "myhooks"
        hooks_dir.mkdir()
        (hooks_dir / "pre-commit").write_text(
            f"#!/bin/bash\ntouch {marker}\n"
        )
        (hooks_dir / "pre-commit").chmod(0o755)

        (ns.workdir / "new.txt").write_text("content\n")
        cave_git(ns.workdir, "add", "new.txt")
        cave_git(ns.workdir, "add", "myhooks")
        result = cave_git(ns.workdir, "commit", "-m", "test commit")
        assert result.returncode == 0, result.stderr
        assert marker.exists(), "core.hooksPath hook was not chained"

    def test_commit_msg_passthrough(self, tmp_path):
        """Repo commit-msg hook runs via the passthrough stub."""
        ns = _make_project(tmp_path, "test-project")
        marker = ns.tmp_path / "commit-msg-ran"

        hooks_dir = ns.workdir / ns.hooks_path
        hooks_dir.mkdir()
        (hooks_dir / "commit-msg").write_text(
            f"#!/bin/bash\ntouch {marker}\n"
        )
        (hooks_dir / "commit-msg").chmod(0o755)

        (ns.workdir / "new.txt").write_text("content\n")
        cave_git(ns.workdir, "add", "new.txt")
        cave_git(ns.workdir, "add", ns.hooks_path)
        result = cave_git(ns.workdir, "commit", "-m", "test commit")
        assert result.returncode == 0, result.stderr
        assert marker.exists(), "repo commit-msg was not chained"

    def test_no_repo_hook_still_commits(self, tmp_path):
        """Commit succeeds when no repo-local hooks exist."""
        ns = _make_project(tmp_path, "test-project")

        (ns.workdir / "new.txt").write_text("content\n")
        cave_git(ns.workdir, "add", "new.txt")
        result = cave_git(ns.workdir, "commit", "-m", "normal commit")
        assert result.returncode == 0, result.stderr

    def test_default_git_hooks_copied(self, tmp_path):
        """Host hooks in .git/hooks/ (no core.hooksPath) are copied and chained."""
        slug = "default-hooks"
        _state.set_cave_data(tmp_path / "cave-data")
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        host = make_repo(tmp_path / "host")
        # No core.hooksPath — hooks in default .git/hooks/
        host_hooks = host / ".git" / "hooks"
        host_hooks.mkdir(exist_ok=True)
        marker = tmp_path / "default-hook-ran"
        (host_hooks / "pre-commit").write_text(
            f"#!/bin/bash\ntouch {marker}\n"
        )
        (host_hooks / "pre-commit").chmod(0o755)
        wrapper.initialize_gitdir(slug, host)

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        session_gd = wrapper.create_session_gitdir(slug, "test-sid", overlay,
                                                   source_dir=host)

        # cave.repoHooksPath should be set to absolute cave path
        result = git_run("git", "--git-dir", str(session_gd),
                         "config", "cave.repoHooksPath")
        assert result.returncode == 0
        assert result.stdout.strip() == f"/projects/.gitdirs/{slug}/repo-hooks"

        # The hook should have been copied
        assert (session_gd / "repo-hooks" / "pre-commit").is_file()

    def test_default_git_hooks_samples_skipped(self, tmp_path):
        """Hook samples (.sample suffix) are not copied."""
        slug = "sample-hooks"
        _state.set_cave_data(tmp_path / "cave-data")
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        host = make_repo(tmp_path / "host")
        host_hooks = host / ".git" / "hooks"
        host_hooks.mkdir(exist_ok=True)
        # Only samples, no real hooks
        (host_hooks / "pre-commit.sample").write_text("#!/bin/bash\n")
        (host_hooks / "pre-commit.sample").chmod(0o755)
        wrapper.initialize_gitdir(slug, host)

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        session_gd = wrapper.create_session_gitdir(slug, "test-sid", overlay,
                                                   source_dir=host)

        result = git_run("git", "--git-dir", str(session_gd),
                         "config", "cave.repoHooksPath")
        assert result.returncode != 0, "Samples should not trigger hook forwarding"

    def test_no_hooks_no_config(self, tmp_path):
        """No hooks on host, no cave.repoHooksPath set."""
        slug = "no-hooks"
        _state.set_cave_data(tmp_path / "cave-data")
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        host = make_repo(tmp_path / "host")
        wrapper.initialize_gitdir(slug, host)

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        session_gd = wrapper.create_session_gitdir(slug, "test-sid", overlay,
                                                   source_dir=host)

        result = git_run("git", "--git-dir", str(session_gd),
                         "config", "cave.repoHooksPath")
        assert result.returncode != 0, "Should not have cave.repoHooksPath"

    def test_no_source_dir_skips_hookspath(self, tmp_path):
        """create_session_gitdir without source_dir doesn't set cave.repoHooksPath."""
        slug = "no-source"
        _state.set_cave_data(tmp_path / "cave-data")
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        host = make_repo(tmp_path / "host")
        git_run("git", "-C", str(host), "config", "core.hooksPath", "myhooks")
        wrapper.initialize_gitdir(slug, host)

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        session_gd = wrapper.create_session_gitdir(slug, "test-sid", overlay)

        result = git_run("git", "--git-dir", str(session_gd),
                         "config", "cave.repoHooksPath")
        assert result.returncode != 0, "Should not have cave.repoHooksPath"
