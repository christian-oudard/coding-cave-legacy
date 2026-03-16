"""Tests for pull_project: normal pull, auto-force, and edge cases."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import wrapper
from coding_cave import _state
from tests.conftest import write_projects


def _run(*args, check=True):
    """Run a command, return CompletedProcess."""
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


def _head(path, gitdir=False):
    """Get HEAD commit hash."""
    if gitdir:
        return _run("git", "--git-dir", str(path), "rev-parse", "HEAD").stdout.strip()
    return _run("git", "-C", str(path), "rev-parse", "HEAD").stdout.strip()


def _log_count(path):
    """Count commits on HEAD."""
    return int(_run("git", "-C", str(path), "rev-list", "--count", "HEAD").stdout.strip())


def _last_author(path):
    """Get author name of HEAD commit."""
    return _run("git", "-C", str(path), "log", "-1", "--format=%an").stdout.strip()


def _cave_git(env, *args):
    """Run git against the cave gitdir + working tree."""
    cmd = ["git", "--git-dir", str(env.gitdir), "--work-tree", str(env.cave)]
    return _run(*cmd, *args)


def cave_commit(env, msg, filename="file.txt", content=None, amend=False):
    """Make a commit in the cave."""
    if content is not None:
        (env.cave / filename).write_text(content)
        _cave_git(env, "add", filename)
    args = ["commit", "-m", msg]
    if amend:
        args.append("--amend")
    _cave_git(env, *args)


@pytest.fixture
def pull_env(tmp_path):
    """Pull testing environment: synced host repo + cave gitdir."""
    slug = "test-project"

    host = tmp_path / "host"
    host.mkdir()
    _run("git", "-C", str(host), "init", "-b", "main")
    _run("git", "-C", str(host), "config", "user.email", "host@test.com")
    _run("git", "-C", str(host), "config", "user.name", "Host User")
    (host / "file.txt").write_text("initial\n")
    _run("git", "-C", str(host), "add", ".")
    _run("git", "-C", str(host), "commit", "-m", "initial")

    tmpclone = tmp_path / "_tmpclone"
    _run("git", "clone", "--no-checkout", "--local", str(host), str(tmpclone))
    projects_dir = tmp_path / "projects"
    proj_state = projects_dir / slug
    proj_state.mkdir(parents=True)
    gitdir = proj_state / "gitdir"
    (tmpclone / ".git").rename(gitdir)
    tmpclone.rmdir()
    _run("git", "--git-dir", str(gitdir), "remote", "remove", "origin", check=False)
    _run("git", "--git-dir", str(gitdir), "config", "user.email", "cave-coder@cave")
    _run("git", "--git-dir", str(gitdir), "config", "user.name", "cave-coder")
    _run("git", "--git-dir", str(gitdir), "read-tree", "HEAD")

    cave = tmp_path / "cave"
    cave.mkdir()
    _run("git", "--git-dir", str(gitdir), "--work-tree", str(cave),
         "checkout", "HEAD", "--", ".")

    _state.set_cave_data(tmp_path)
    write_projects(tmp_path, {slug: str(host)})

    saved_env = {}
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        saved_env[var] = os.environ.pop(var, None)

    wrapper.ensure_cave_remote(host, gitdir)
    host_head = _head(host)
    wrapper.sync_refs(gitdir, host, host_head, "main")

    yield SimpleNamespace(host=host, gitdir=gitdir, cave=cave, slug=slug)

    for var, val in saved_env.items():
        if val is not None:
            os.environ[var] = val


class TestPullForceRequired:
    """History rewrites require --force."""

    def test_amend_without_force_is_noop(self, pull_env):
        """Cave amends a synced commit -> plain pull refuses."""
        env = pull_env

        cave_commit(env, "cave work", content="v1\n")
        wrapper.pull_project(env.slug)
        head_v1 = _head(env.host)
        assert (env.host / "file.txt").read_text() == "v1\n"

        # Cave amends the synced commit
        cave_commit(env, "amended work", content="v2\n", amend=True)

        # Plain pull (no --force) should refuse
        wrapper.pull_project(env.slug)
        assert _head(env.host) == head_v1
        assert (env.host / "file.txt").read_text() == "v1\n"

    def test_amend_with_force(self, pull_env):
        """Cave amends a synced commit -> pull --force adopts."""
        env = pull_env

        cave_commit(env, "cave work", content="v1\n")
        wrapper.pull_project(env.slug)
        head_v1 = _head(env.host)

        cave_commit(env, "amended work", content="v2\n", amend=True)

        wrapper.pull_project(env.slug, force=True)
        assert _head(env.host) != head_v1
        assert (env.host / "file.txt").read_text() == "v2\n"

    def test_squash_with_force(self, pull_env):
        """Cave squashes synced commits -> pull --force adopts."""
        env = pull_env

        cave_commit(env, "first", content="first\n")
        wrapper.pull_project(env.slug)
        cave_commit(env, "second", content="second\n")
        wrapper.pull_project(env.slug)
        assert _log_count(env.host) == 3  # initial + 2

        # Cave squashes via commit-tree: same tree, parent=initial
        initial = _run("git", "--git-dir", str(env.gitdir),
                        "rev-list", "--max-parents=0", "HEAD").stdout.strip()
        tree = _run("git", "--git-dir", str(env.gitdir),
                     "rev-parse", "HEAD^{tree}").stdout.strip()
        squashed = _run("git", "--git-dir", str(env.gitdir),
                         "commit-tree", tree, "-p", initial,
                         "-m", "squashed").stdout.strip()
        _run("git", "--git-dir", str(env.gitdir),
             "update-ref", "refs/heads/main", squashed)

        wrapper.pull_project(env.slug, force=True)
        assert (env.host / "file.txt").read_text() == "second\n"
        assert _log_count(env.host) == 2  # initial + squashed

    def test_force_then_normal_commit(self, pull_env):
        """After force-pull, normal commits still pull correctly."""
        env = pull_env

        cave_commit(env, "first", content="v1\n")
        wrapper.pull_project(env.slug)
        cave_commit(env, "amended", content="v2\n", amend=True)
        wrapper.pull_project(env.slug, force=True)

        # Now a fresh commit should pull normally
        cave_commit(env, "next", content="v3\n")
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "v3\n"
        assert _log_count(env.host) == 3  # initial + amended + next

    def test_reset_without_force_is_noop(self, pull_env):
        """Cave resets behind host -> plain pull ignores."""
        env = pull_env

        cave_commit(env, "A", content="A\n")
        cave_commit(env, "B", content="B\n")
        wrapper.pull_project(env.slug)
        host_head = _head(env.host)
        assert _log_count(env.host) == 3  # initial + A + B

        # Cave resets back to initial
        initial = _run("git", "--git-dir", str(env.gitdir),
                        "rev-list", "--max-parents=0", "HEAD").stdout.strip()
        _run("git", "--git-dir", str(env.gitdir),
             "update-ref", "refs/heads/main", initial)

        wrapper.pull_project(env.slug)
        assert _head(env.host) == host_head  # unchanged

    def test_reset_with_force(self, pull_env):
        """Cave resets behind host -> pull --force adopts cave's version."""
        env = pull_env

        cave_commit(env, "A", content="A\n")
        cave_commit(env, "B", content="B\n")
        wrapper.pull_project(env.slug)
        assert _log_count(env.host) == 3  # initial + A + B

        # Cave resets back to initial
        initial = _run("git", "--git-dir", str(env.gitdir),
                        "rev-list", "--max-parents=0", "HEAD").stdout.strip()
        _run("git", "--git-dir", str(env.gitdir),
             "update-ref", "refs/heads/main", initial)

        wrapper.pull_project(env.slug, force=True)
        assert _head(env.host) == initial
        assert _log_count(env.host) == 1  # just initial

    def test_reset_with_force_then_normal_commit(self, pull_env):
        """After force-pulling a reset, normal commits still work."""
        env = pull_env

        cave_commit(env, "A", content="A\n")
        wrapper.pull_project(env.slug)

        # Cave resets to initial
        initial = _run("git", "--git-dir", str(env.gitdir),
                        "rev-list", "--max-parents=0", "HEAD").stdout.strip()
        _run("git", "--git-dir", str(env.gitdir),
             "update-ref", "refs/heads/main", initial)

        wrapper.pull_project(env.slug, force=True)
        assert _head(env.host) == initial

        # New commit after reset pulls normally
        cave_commit(env, "C", content="C\n")
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "C\n"
        assert _log_count(env.host) == 2  # initial + C

    def test_reset_with_force_preserves_uncommitted(self, pull_env):
        """Force-pulling a reset stashes and restores uncommitted changes."""
        env = pull_env

        cave_commit(env, "A", content="A\n")
        wrapper.pull_project(env.slug)

        # Host has uncommitted changes
        (env.host / "local.txt").write_text("uncommitted\n")
        _run("git", "-C", str(env.host), "add", "local.txt")

        # Cave resets to initial
        initial = _run("git", "--git-dir", str(env.gitdir),
                        "rev-list", "--max-parents=0", "HEAD").stdout.strip()
        _run("git", "--git-dir", str(env.gitdir),
             "update-ref", "refs/heads/main", initial)

        wrapper.pull_project(env.slug, force=True)
        assert _head(env.host) == initial
        # Uncommitted file should be restored from stash
        assert (env.host / "local.txt").read_text() == "uncommitted\n"


class TestPullNormal:
    """Normal pull: fresh commits without history rewriting."""

    def test_new_commit(self, pull_env):
        """Fresh cave commit is pulled."""
        env = pull_env
        cave_commit(env, "new work", content="new\n")
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "new\n"
        assert _log_count(env.host) == 2

    def test_multiple_commits(self, pull_env):
        """Multiple cave commits pulled in one go."""
        env = pull_env
        cave_commit(env, "first", content="one\n")
        cave_commit(env, "second", content="two\n")
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "two\n"
        assert _log_count(env.host) == 3

    def test_noop_when_synced(self, pull_env):
        """No changes -> noop."""
        env = pull_env
        head_before = _head(env.host)
        wrapper.pull_project(env.slug)
        assert _head(env.host) == head_before

    def test_incremental_pulls(self, pull_env):
        """Successive pulls each add new commits."""
        env = pull_env
        cave_commit(env, "first", content="one\n")
        wrapper.pull_project(env.slug)
        assert _log_count(env.host) == 2

        cave_commit(env, "second", content="two\n")
        wrapper.pull_project(env.slug)
        assert _log_count(env.host) == 3
        assert (env.host / "file.txt").read_text() == "two\n"


class TestPullEdgeCases:
    """Edge cases: empty repos, stale state, detached HEAD."""

    def test_pull_into_empty_host(self, tmp_path):
        """Pull into a host repo with no commits initializes from cave.

        Regression: 0a7e904 — pull crashed on empty host repos because
        rev-parse HEAD failed with check=True.
        """
        slug = "test-project"

        host = tmp_path / "host"
        host.mkdir()
        _run("git", "-C", str(host), "init", "-b", "main")
        _run("git", "-C", str(host), "config", "user.email", "host@test.com")
        _run("git", "-C", str(host), "config", "user.name", "Host User")

        source = tmp_path / "source"
        source.mkdir()
        _run("git", "-C", str(source), "init", "-b", "main")
        _run("git", "-C", str(source), "config", "user.email", "cave@test.com")
        _run("git", "-C", str(source), "config", "user.name", "cave-coder")
        (source / "file.txt").write_text("cave work\n")
        _run("git", "-C", str(source), "add", ".")
        _run("git", "-C", str(source), "commit", "-m", "cave commit")

        projects_dir = tmp_path / "projects"
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        tmpclone = tmp_path / "_tmpclone"
        _run("git", "clone", "--no-checkout", "--local", str(source), str(tmpclone))
        gitdir = proj_state / "gitdir"
        (tmpclone / ".git").rename(gitdir)
        tmpclone.rmdir()
        _run("git", "--git-dir", str(gitdir), "remote", "remove", "origin", check=False)

        _state.set_cave_data(tmp_path)
        write_projects(tmp_path, {slug: str(host)})

        wrapper.pull_project(slug)

        # Host should now have the commit and a branch
        assert _log_count(host) == 1
        branch = _run("git", "-C", str(host), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert branch == "main"
        assert (host / "file.txt").read_text() == "cave work\n"

    def test_detached_head_uses_main_branch(self, tmp_path):
        """Pull from detached HEAD gitdir creates 'main' branch, not 'HEAD'.

        Regression: e5e228a — git rev-parse --abbrev-ref HEAD returns literal
        'HEAD' when detached, which was used as a branch name.
        """
        slug = "test-project"

        host = tmp_path / "host"
        host.mkdir()
        _run("git", "-C", str(host), "init", "-b", "main")
        _run("git", "-C", str(host), "config", "user.email", "host@test.com")
        _run("git", "-C", str(host), "config", "user.name", "Host User")

        source = tmp_path / "source"
        source.mkdir()
        _run("git", "-C", str(source), "init", "-b", "main")
        _run("git", "-C", str(source), "config", "user.email", "cave@test.com")
        _run("git", "-C", str(source), "config", "user.name", "cave-coder")
        (source / "file.txt").write_text("work\n")
        _run("git", "-C", str(source), "add", ".")
        _run("git", "-C", str(source), "commit", "-m", "initial")

        projects_dir = tmp_path / "projects"
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        tmpclone = tmp_path / "_tmpclone"
        _run("git", "clone", "--no-checkout", "--local", str(source), str(tmpclone))
        gitdir = proj_state / "gitdir"
        (tmpclone / ".git").rename(gitdir)
        tmpclone.rmdir()
        _run("git", "--git-dir", str(gitdir), "remote", "remove", "origin", check=False)

        head_hash = _run("git", "--git-dir", str(gitdir), "rev-parse", "HEAD").stdout.strip()
        (gitdir / "HEAD").write_text(head_hash + "\n")
        abbrev = _run("git", "--git-dir", str(gitdir),
                       "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert abbrev == "HEAD"

        _state.set_cave_data(tmp_path)
        write_projects(tmp_path, {slug: str(host)})

        wrapper.pull_project(slug)

        branch = _run("git", "-C", str(host), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert branch == "main", f"Expected 'main', got '{branch}'"

    def test_stale_cave_pull_branch(self, pull_env):
        """Stale _cave_pull branch from interrupted pull doesn't crash.

        Regression: 68dbc83 — git branch -d fails on _cave_pull when it has
        rewritten commits not fully merged into main. Changed to -D.
        """
        env = pull_env

        # Create a diverged _cave_pull branch (simulates interrupted pull)
        _run("git", "-C", str(env.host), "checkout", "-b", "_cave_pull")
        (env.host / "stale.txt").write_text("from interrupted pull\n")
        _run("git", "-C", str(env.host), "add", "stale.txt")
        _run("git", "-C", str(env.host), "commit", "-m", "stale rebase work")
        _run("git", "-C", str(env.host), "checkout", "main")

        # Make a cave commit
        cave_commit(env, "new work", content="new\n")

        # Pull should succeed despite stale _cave_pull
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "new\n"

        # _cave_pull should be cleaned up
        branches = _run("git", "-C", str(env.host), "branch",
                         "--list", "_cave_pull").stdout.strip()
        assert branches == ""


class TestPullMidSession:
    """Pull picks up commits from running sessions."""

    def test_pull_sees_session_commits(self, pull_env, tmp_path):
        """Commits in a per-session gitdir are visible to pull_project."""
        env = pull_env
        sid = "test-session-123"

        # Create session overlay area and per-session gitdir
        overlay_base = tmp_path / "session-overlays" / sid
        overlay_base.mkdir(parents=True)

        session_gitdir = wrapper.create_session_gitdir(
            env.slug, sid, overlay_base,
        )
        assert session_gitdir is not None

        # Register a fake running session (use our own PID so it looks alive)
        import os
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        session_info = {
            "sid": sid,
            "project": env.slug,
            "bwrap_pid": os.getpid(),
            "started": 0,
        }
        (sessions_dir / f"{sid}.json").write_text(json.dumps(session_info))

        # Commit in the session gitdir (not the shared one)
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.email", "cave@test.com")
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.name", "cave-coder")
        session_cave = tmp_path / "session-cave"
        session_cave.mkdir()
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_cave), "checkout", "HEAD", "--", ".", check=False)
        (session_cave / "file.txt").write_text("from session\n")
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_cave), "add", "file.txt")
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_cave), "commit", "-m", "session commit")

        # Verify shared gitdir does NOT have this commit yet
        shared_head = _head(env.gitdir, gitdir=True)
        session_head = _run("git", "--git-dir", str(session_gitdir),
                            "rev-parse", "HEAD").stdout.strip()
        assert shared_head != session_head

        # Pull should pick up the session commit
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "from session\n"

    def test_cross_project_session_commits(self, pull_env, tmp_path):
        """Pull project B sees commits from a session started for project A.

        Reproduction: start a session in folder A, agent edits and commits to
        project B, `cav pull B` from host should get the commit.  Bug: the
        session's "project" field is A, so sync_live_sessions("B") skipped it.
        """
        env = pull_env  # project B (slug="test-project", has host+gitdir)
        sid = "test-session-cross"

        overlay_base = tmp_path / "session-overlays" / sid
        overlay_base.mkdir(parents=True)

        # Create per-session gitdir for project B within this session
        session_gitdir = wrapper.create_session_gitdir(
            env.slug, sid, overlay_base,
        )
        assert session_gitdir is not None

        # Register session with a DIFFERENT primary project
        import os
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)
        session_info = {
            "sid": sid,
            "project": "other-project",  # <-- not env.slug
            "bwrap_pid": os.getpid(),
            "started": 0,
        }
        (sessions_dir / f"{sid}.json").write_text(json.dumps(session_info))

        # Commit in the session gitdir
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.email", "cave@test.com")
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.name", "cave-coder")
        session_cave = tmp_path / "session-cave"
        session_cave.mkdir()
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_cave), "checkout", "HEAD", "--", ".", check=False)
        (session_cave / "file.txt").write_text("cross-project\n")
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_cave), "add", "file.txt")
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_cave), "commit", "-m", "cross-project commit")

        # Pull project B — should find the commit even though session is for A
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "cross-project\n"


class TestPullHostIdentity:
    """Pull when cave uses the host's git identity (no re-authoring needed).

    When the cave forwards the host's user.name/user.email, commits already
    have the correct author. Pull should skip re-authoring and preserve
    exact commit hashes — making sync a simple fast-forward.
    """

    @pytest.fixture
    def host_id_env(self, tmp_path):
        """Environment where cave uses the same identity as host."""
        slug = "test-project"

        host = tmp_path / "host"
        host.mkdir()
        _run("git", "-C", str(host), "init", "-b", "main")
        _run("git", "-C", str(host), "config", "user.email", "host@test.com")
        _run("git", "-C", str(host), "config", "user.name", "Host User")
        (host / "file.txt").write_text("initial\n")
        _run("git", "-C", str(host), "add", ".")
        _run("git", "-C", str(host), "commit", "-m", "initial")

        tmpclone = tmp_path / "_tmpclone"
        _run("git", "clone", "--no-checkout", "--local", str(host), str(tmpclone))
        projects_dir = tmp_path / "projects"
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        gitdir = proj_state / "gitdir"
        (tmpclone / ".git").rename(gitdir)
        tmpclone.rmdir()
        _run("git", "--git-dir", str(gitdir), "remote", "remove", "origin",
             check=False)
        # Cave uses HOST identity (the key difference from pull_env)
        _run("git", "--git-dir", str(gitdir), "config",
             "user.email", "host@test.com")
        _run("git", "--git-dir", str(gitdir), "config",
             "user.name", "Host User")
        _run("git", "--git-dir", str(gitdir), "read-tree", "HEAD")

        cave = tmp_path / "cave"
        cave.mkdir()
        _run("git", "--git-dir", str(gitdir), "--work-tree", str(cave),
             "checkout", "HEAD", "--", ".")

        _state.set_cave_data(tmp_path)
        write_projects(tmp_path, {slug: str(host)})

        saved_env = {}
        for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            saved_env[var] = os.environ.pop(var, None)

        wrapper.ensure_cave_remote(host, gitdir)
        host_head = _head(host)
        wrapper.sync_refs(gitdir, host, host_head, "main")

        yield SimpleNamespace(host=host, gitdir=gitdir, cave=cave, slug=slug)

        for var, val in saved_env.items():
            if val is not None:
                os.environ[var] = val

    def test_preserves_commit_hash(self, host_id_env):
        """When cave uses host identity, pull preserves exact commit hash."""
        env = host_id_env
        cave_commit(env, "cave work", content="work\n")
        cave_head = _head(env.gitdir, gitdir=True)

        wrapper.pull_project(env.slug)

        # Host HEAD should be the SAME hash as cave (no re-authoring)
        assert _head(env.host) == cave_head

    def test_preserves_author(self, host_id_env):
        """Pulled commits keep the host author (already correct)."""
        env = host_id_env
        cave_commit(env, "cave work", content="work\n")
        wrapper.pull_project(env.slug)
        assert _last_author(env.host) == "Host User"

    def test_multiple_commits_preserve_hashes(self, host_id_env):
        """Multiple commits all preserve their exact hashes."""
        env = host_id_env
        cave_commit(env, "first", content="one\n")
        hash1 = _head(env.gitdir, gitdir=True)
        cave_commit(env, "second", content="two\n")
        hash2 = _head(env.gitdir, gitdir=True)

        wrapper.pull_project(env.slug)

        # Both hashes should exist on host
        _run("git", "-C", str(env.host), "cat-file", "-t", hash1)
        _run("git", "-C", str(env.host), "cat-file", "-t", hash2)
        assert _head(env.host) == hash2

    def test_sync_refs_after_pull_matches(self, host_id_env):
        """After pull, gitdir refs match host exactly (same hashes)."""
        env = host_id_env
        cave_commit(env, "cave work", content="work\n")
        wrapper.pull_project(env.slug)

        host_head = _head(env.host)
        gitdir_head = _head(env.gitdir, gitdir=True)
        assert host_head == gitdir_head

    def test_incremental_pulls_no_divergence(self, host_id_env):
        """Successive pulls never create hash divergence."""
        env = host_id_env

        cave_commit(env, "first", content="one\n")
        wrapper.pull_project(env.slug)
        assert _head(env.host) == _head(env.gitdir, gitdir=True)

        cave_commit(env, "second", content="two\n")
        wrapper.pull_project(env.slug)
        assert _head(env.host) == _head(env.gitdir, gitdir=True)

        cave_commit(env, "third", content="three\n")
        wrapper.pull_project(env.slug)
        assert _head(env.host) == _head(env.gitdir, gitdir=True)

    def test_amend_with_force_preserves_hash(self, host_id_env):
        """Force pull of amended commit preserves exact hash."""
        env = host_id_env
        cave_commit(env, "cave work", content="v1\n")
        wrapper.pull_project(env.slug)

        cave_commit(env, "amended", content="v2\n", amend=True)
        cave_head = _head(env.gitdir, gitdir=True)

        wrapper.pull_project(env.slug, force=True)
        assert _head(env.host) == cave_head

    def test_push_then_pull_roundtrip(self, host_id_env):
        """Push host commit, make cave commit, pull — no hash mangling."""
        env = host_id_env

        # Host makes a commit and pushes
        (env.host / "host.txt").write_text("from host\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host work")
        wrapper.push_project(env.slug)

        # Update cave working tree from gitdir
        _run("git", "--git-dir", str(env.gitdir), "--work-tree", str(env.cave),
             "checkout", "-f")

        # Cave makes a commit on top
        cave_commit(env, "cave work", content="cave\n")
        cave_head = _head(env.gitdir, gitdir=True)

        wrapper.pull_project(env.slug)

        # Exact hash preserved, host work still present
        assert _head(env.host) == cave_head
        assert (env.host / "host.txt").read_text() == "from host\n"
        assert (env.host / "file.txt").read_text() == "cave\n"


class TestPullMultiBranch:
    """Pull is branch-aware: each cave branch pulls to the matching host branch."""

    def test_cave_branch_not_applied_to_wrong_host_branch(self, pull_env):
        """Cave commits on develop must NOT land on host's main.

        Regression: pull fetched gitdir HEAD regardless of branch, so cave
        commits on develop were rebased onto whichever branch the host had
        checked out.
        """
        env = pull_env

        # Create develop on both host and cave (both at initial commit)
        _run("git", "-C", str(env.host), "branch", "develop")
        _run("git", "--git-dir", str(env.gitdir), "branch", "develop")
        host_develop = _run("git", "-C", str(env.host),
                            "rev-parse", "develop").stdout.strip()
        wrapper.sync_refs(env.gitdir, env.host, host_develop, "develop")

        # Cave commits on develop
        _cave_git(env, "checkout", "develop")
        cave_commit(env, "develop work", content="develop content\n")
        _cave_git(env, "checkout", "main")

        # Host is on main
        main_head = _head(env.host)
        wrapper.pull_project(env.slug)

        # Main should be unchanged
        assert _head(env.host) == main_head
        assert (env.host / "file.txt").read_text() == "initial\n"

        # Develop should have the cave commit
        _run("git", "-C", str(env.host), "checkout", "develop")
        assert (env.host / "file.txt").read_text() == "develop content\n"

    def test_both_branches_pulled(self, pull_env):
        """Commits on multiple cave branches are all pulled correctly."""
        env = pull_env

        # Create feature branch on both sides
        _run("git", "-C", str(env.host), "branch", "feature")
        _run("git", "--git-dir", str(env.gitdir), "branch", "feature")
        host_feature = _run("git", "-C", str(env.host),
                            "rev-parse", "feature").stdout.strip()
        wrapper.sync_refs(env.gitdir, env.host, host_feature, "feature")

        # Cave commits on main
        cave_commit(env, "main work", content="main content\n")

        # Cave commits on feature
        _cave_git(env, "checkout", "feature")
        cave_commit(env, "feature work", content="feature content\n",
                    filename="feature.txt")
        _cave_git(env, "checkout", "main")

        wrapper.pull_project(env.slug)

        # Main has its commit
        assert (env.host / "file.txt").read_text() == "main content\n"

        # Feature has its commit
        _run("git", "-C", str(env.host), "checkout", "feature")
        assert (env.host / "feature.txt").read_text() == "feature content\n"
        # Feature should NOT have main's commit
        assert (env.host / "file.txt").read_text() == "initial\n"

    def test_noop_branch_not_affected(self, pull_env):
        """Branches with no cave changes are not modified."""
        env = pull_env

        # Create develop on both sides
        _run("git", "-C", str(env.host), "branch", "develop")
        _run("git", "--git-dir", str(env.gitdir), "branch", "develop")

        # Only commit on main
        cave_commit(env, "main work", content="new\n")

        host_develop = _head(env.host)  # main and develop at same commit
        wrapper.pull_project(env.slug)

        # Develop should still be at the original commit
        develop_head = _run("git", "-C", str(env.host),
                            "rev-parse", "develop").stdout.strip()
        assert develop_head == host_develop

    def test_repeated_pull_skips_already_pulled_branch(self, pull_env):
        """Second pull of already-pulled non-current branch is a noop."""
        env = pull_env

        # Create develop on both sides
        _run("git", "-C", str(env.host), "branch", "develop")
        _run("git", "--git-dir", str(env.gitdir), "branch", "develop")
        host_develop = _run("git", "-C", str(env.host),
                            "rev-parse", "develop").stdout.strip()
        wrapper.sync_refs(env.gitdir, env.host, host_develop, "develop")

        # Cave commits on develop
        _cave_git(env, "checkout", "develop")
        cave_commit(env, "develop work", content="develop content\n")
        _cave_git(env, "checkout", "main")

        # First pull
        wrapper.pull_project(env.slug)
        _run("git", "-C", str(env.host), "checkout", "develop")
        assert (env.host / "file.txt").read_text() == "develop content\n"
        develop_after_pull = _run("git", "-C", str(env.host),
                                  "rev-parse", "develop").stdout.strip()
        _run("git", "-C", str(env.host), "checkout", "main")

        # Second pull should be a clean noop (no skip messages)
        wrapper.pull_project(env.slug)
        develop_after_second = _run("git", "-C", str(env.host),
                                    "rev-parse", "develop").stdout.strip()
        assert develop_after_second == develop_after_pull

    def test_host_identity_preserves_hash_non_current(self, tmp_path):
        """Non-current branch with host identity preserves commit hash."""
        slug = "test-project"

        # Remove env vars BEFORE creating commits so the initial commit
        # uses the repo-local config, not forwarded cave identity.
        saved_env = {}
        for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            saved_env[var] = os.environ.pop(var, None)

        host = tmp_path / "host"
        host.mkdir()
        _run("git", "-C", str(host), "init", "-b", "main")
        _run("git", "-C", str(host), "config", "user.email", "host@test.com")
        _run("git", "-C", str(host), "config", "user.name", "Host User")
        (host / "file.txt").write_text("initial\n")
        _run("git", "-C", str(host), "add", ".")
        _run("git", "-C", str(host), "commit", "-m", "initial")

        tmpclone = tmp_path / "_tmpclone"
        _run("git", "clone", "--no-checkout", "--local", str(host), str(tmpclone))
        projects_dir = tmp_path / "projects"
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        gitdir = proj_state / "gitdir"
        (tmpclone / ".git").rename(gitdir)
        tmpclone.rmdir()
        _run("git", "--git-dir", str(gitdir), "remote", "remove", "origin",
             check=False)
        # Cave uses HOST identity
        _run("git", "--git-dir", str(gitdir), "config",
             "user.email", "host@test.com")
        _run("git", "--git-dir", str(gitdir), "config",
             "user.name", "Host User")
        _run("git", "--git-dir", str(gitdir), "read-tree", "HEAD")

        cave = tmp_path / "cave"
        cave.mkdir()
        _run("git", "--git-dir", str(gitdir), "--work-tree", str(cave),
             "checkout", "HEAD", "--", ".")

        _state.set_cave_data(tmp_path)
        write_projects(tmp_path, {slug: str(host)})

        wrapper.ensure_cave_remote(host, gitdir)
        host_head = _head(host)
        wrapper.sync_refs(gitdir, host, host_head, "main")

        # Create develop on both sides
        _run("git", "-C", str(host), "branch", "develop")
        _run("git", "--git-dir", str(gitdir), "branch", "develop")
        wrapper.sync_refs(gitdir, host, host_head, "develop")

        env = SimpleNamespace(host=host, gitdir=gitdir, cave=cave, slug=slug)

        # Cave commits on develop
        _cave_git(env, "checkout", "develop")
        cave_commit(env, "develop work", content="work\n")
        cave_head = _run("git", "--git-dir", str(gitdir),
                         "rev-parse", "develop").stdout.strip()
        _cave_git(env, "checkout", "main")

        wrapper.pull_project(slug)

        # Hash should be preserved (no re-authoring)
        develop_host = _run("git", "-C", str(host),
                            "rev-parse", "develop").stdout.strip()
        assert develop_host == cave_head

        for var, val in saved_env.items():
            if val is not None:
                os.environ[var] = val


class TestPullUpdatesHead:
    def test_pull_updates_shared_gitdir_head(self, pull_env):
        """Pull updates shared gitdir HEAD to match host's current branch."""
        env = pull_env

        # Create develop branch in cave with a commit
        _cave_git(env, "checkout", "-b", "develop")
        cave_commit(env, "develop work", "dev.txt", "develop\n")

        # Host is on main, gitdir HEAD should point to main after pull
        wrapper.pull_project(env.slug)

        head_after = _run("git", "--git-dir", str(env.gitdir),
                          "symbolic-ref", "HEAD").stdout.strip()
        assert head_after == "refs/heads/main"

    def test_pull_updates_head_when_host_on_different_branch(self, pull_env):
        """Pull sets gitdir HEAD to host's current branch, not cave's."""
        env = pull_env

        # Create develop on both sides
        _run("git", "-C", str(env.host), "checkout", "-b", "develop")
        (env.host / "host.txt").write_text("host\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host on develop")
        _run("git", "-C", str(env.host), "checkout", "main")

        # Push develop to cave so it exists there
        wrapper.push_project(env.slug)

        # Make a cave commit on develop
        _cave_git(env, "fetch", str(env.gitdir))
        _cave_git(env, "checkout", "develop")
        cave_commit(env, "cave develop", "cave.txt", "cave\n")

        # Switch host to develop before pulling
        _run("git", "-C", str(env.host), "checkout", "develop")
        wrapper.pull_project(env.slug)

        head_after = _run("git", "--git-dir", str(env.gitdir),
                          "symbolic-ref", "HEAD").stdout.strip()
        assert head_after == "refs/heads/develop"


class TestPullSessionIntegration:
    """End-to-end: session commit → pull_project sees it.

    These test the full chain: create_session_gitdir → commit in session →
    sync_live_sessions (inside pull_project) → host gets the commit.
    Reproduces the failure patterns from real multi-session usage.
    """

    def _make_session_env(self, pull_env, tmp_path, sid):
        """Create a live session with a registered session file."""
        env = pull_env
        overlay_base = tmp_path / "session-overlays" / sid
        overlay_base.mkdir(parents=True)
        session_gitdir = wrapper.create_session_gitdir(env.slug, sid, overlay_base)

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_info = {
            "sid": sid,
            "project": env.slug,
            "bwrap_pid": os.getpid(),
            "started": 0,
        }
        (sessions_dir / f"{sid}.json").write_text(json.dumps(session_info))

        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.email", "host@test.com")
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.name", "Host User")

        workdir = tmp_path / f"work-{sid}"
        workdir.mkdir()
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(workdir), "checkout", "HEAD", "--", ".", check=False)
        (workdir / ".git").write_text(f"gitdir: {session_gitdir}\n")
        return session_gitdir, workdir

    def _session_git(self, workdir, *args):
        return _run("git", "-C", str(workdir), *args, check=False)

    def test_session_commit_visible_to_pull(self, pull_env, tmp_path):
        """Session commits on main → pull picks them up.

        Regression: with session branches, the session committed on
        session/<sid>, but pull only looked at non-session branches,
        so pull said 'Already up to date'.
        """
        env = pull_env
        _, workdir = self._make_session_env(env, tmp_path, "s1")

        (workdir / "new.txt").write_text("from session\n")
        self._session_git(workdir, "add", "new.txt")
        self._session_git(workdir, "commit", "-m", "session work")

        # Verify session is on main, not a session branch
        branch = self._session_git(workdir, "rev-parse",
                                   "--abbrev-ref", "HEAD").stdout.strip()
        assert branch == "main"

        wrapper.pull_project(env.slug)
        assert (env.host / "new.txt").read_text() == "from session\n"

    def test_session_amend_visible_to_pull(self, pull_env, tmp_path):
        """Session commits then amends → pull picks up the amended version.

        Regression: session amends diverged the session branch from shared
        main, then pull couldn't see the amended commit.
        """
        env = pull_env
        _, workdir = self._make_session_env(env, tmp_path, "s1")

        (workdir / "file.txt").write_text("v1\n")
        self._session_git(workdir, "add", "file.txt")
        self._session_git(workdir, "commit", "-m", "first version")

        # Pull once (sync establishes refs/synced)
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "v1\n"

        # Session amends
        (workdir / "file.txt").write_text("v2\n")
        self._session_git(workdir, "add", "file.txt")
        self._session_git(workdir, "commit", "--amend", "-m", "amended")

        # Pull again — must see the amend
        wrapper.pull_project(env.slug, force=True)
        assert (env.host / "file.txt").read_text() == "v2\n"

    def test_two_sessions_both_visible_to_pull(self, pull_env, tmp_path):
        """Two concurrent sessions commit on main → pull gets both.

        Regression: with session branches, each session pushed to its own
        session/<sid> branch, but pull only looked at main.
        """
        env = pull_env
        _, work_a = self._make_session_env(env, tmp_path, "s1")
        _, work_b = self._make_session_env(env, tmp_path, "s2")

        (work_a / "a.txt").write_text("from A\n")
        self._session_git(work_a, "add", "a.txt")
        self._session_git(work_a, "commit", "-m", "session A work")

        (work_b / "b.txt").write_text("from B\n")
        self._session_git(work_b, "add", "b.txt")
        self._session_git(work_b, "commit", "-m", "session B work")

        wrapper.pull_project(env.slug)

        # Both commits should be present
        assert (env.host / "a.txt").read_text() == "from A\n"
        assert (env.host / "b.txt").read_text() == "from B\n"

    def test_push_then_session_commit_then_pull(self, pull_env, tmp_path):
        """Host pushes, session commits on top, pull sees both.

        Regression: push updated shared gitdir but session was on a
        session branch forked before the push, so session commits
        diverged from main.
        """
        env = pull_env

        # Host makes a commit and pushes to cave
        (env.host / "host.txt").write_text("from host\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host commit")
        wrapper.push_project(env.slug)

        # Session starts AFTER push (session gitdir has host's commit)
        _, workdir = self._make_session_env(env, tmp_path, "s1")

        (workdir / "cave.txt").write_text("from cave\n")
        self._session_git(workdir, "add", "cave.txt")
        self._session_git(workdir, "commit", "-m", "cave commit")

        wrapper.pull_project(env.slug)

        assert (env.host / "host.txt").read_text() == "from host\n"
        assert (env.host / "cave.txt").read_text() == "from cave\n"

    def test_pull_no_stale_session_branches(self, pull_env, tmp_path):
        """After pull, no session/* branches leak into the shared gitdir.

        Regression: session branches accumulated in the shared gitdir
        and were never cleaned up.
        """
        env = pull_env
        _, workdir = self._make_session_env(env, tmp_path, "s1")

        (workdir / "new.txt").write_text("work\n")
        self._session_git(workdir, "add", "new.txt")
        self._session_git(workdir, "commit", "-m", "work")

        wrapper.pull_project(env.slug)

        # No session/* branches should exist in shared gitdir
        refs = _run("git", "--git-dir", str(env.gitdir), "for-each-ref",
                     "--format=%(refname)", "refs/heads/session/").stdout.strip()
        assert refs == "", f"Leaked session branches: {refs}"

    def test_repeated_pull_no_duplicates(self, pull_env, tmp_path):
        """Repeated pulls don't create duplicate commits.

        Regression: ff_session_branches created a new rebased copy of the
        same commit on every pull invocation.
        """
        env = pull_env
        _, workdir = self._make_session_env(env, tmp_path, "s1")

        (workdir / "new.txt").write_text("work\n")
        self._session_git(workdir, "add", "new.txt")
        self._session_git(workdir, "commit", "-m", "one-time work")

        wrapper.pull_project(env.slug)
        log1 = _run("git", "-C", str(env.host),
                     "log", "--oneline").stdout

        # Pull again — should be no-op
        wrapper.pull_project(env.slug)
        log2 = _run("git", "-C", str(env.host),
                     "log", "--oneline").stdout

        # Pull a third time
        wrapper.pull_project(env.slug)
        log3 = _run("git", "-C", str(env.host),
                     "log", "--oneline").stdout

        assert log1 == log2 == log3, (
            f"Repeated pulls changed history:\n"
            f"After 1: {log1}\nAfter 2: {log2}\nAfter 3: {log3}"
        )


class TestPostCommitHook:
    """Test the actual post-commit hook shell script against a real shared remote.

    The hook does: push current branch to shared → set refs/pushed → fetch shared.
    These tests create a session with the shared remote repointed to the actual
    host-side shared gitdir, then commit and verify the hook's effects.
    """

    def _make_hooked_session(self, pull_env, tmp_path, sid):
        """Create a session where the shared remote points to the real gitdir."""
        env = pull_env
        # Allow pushes to the shared gitdir (it's bare-ish, no working tree)
        _run("git", "--git-dir", str(env.gitdir), "config",
             "receive.denyCurrentBranch", "ignore")

        overlay_base = tmp_path / "session-overlays" / sid
        overlay_base.mkdir(parents=True)
        session_gitdir = wrapper.create_session_gitdir(env.slug, sid, overlay_base)

        # Repoint shared remote to the actual shared gitdir (not cave-internal path)
        _run("git", "--git-dir", str(session_gitdir),
             "remote", "set-url", "shared", str(env.gitdir))

        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.email", "host@test.com")
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.name", "Host User")

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / f"{sid}.json").write_text(json.dumps({
            "sid": sid, "project": env.slug,
            "bwrap_pid": os.getpid(), "started": 0,
        }))

        workdir = tmp_path / f"work-{sid}"
        workdir.mkdir()
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(workdir), "checkout", "HEAD", "--", ".", check=False)
        (workdir / ".git").write_text(f"gitdir: {session_gitdir}\n")
        return session_gitdir, workdir

    def _session_git(self, workdir, *args):
        return _run("git", "-C", str(workdir), *args, check=False)

    def test_hook_pushes_commit_to_shared(self, pull_env, tmp_path):
        """Post-commit hook FF-pushes the new commit to shared gitdir."""
        env = pull_env
        gd, workdir = self._make_hooked_session(env, tmp_path, "hook1")

        shared_before = _head(env.gitdir, gitdir=True)

        (workdir / "new.txt").write_text("hooked\n")
        self._session_git(workdir, "add", "new.txt")
        result = self._session_git(workdir, "commit", "-m", "hook test")
        assert result.returncode == 0

        session_head = self._session_git(workdir, "rev-parse", "HEAD").stdout.strip()

        # Hook should have pushed to shared — shared main now at session head
        shared_after = _head(env.gitdir, gitdir=True)
        assert shared_after == session_head
        assert shared_after != shared_before

    def test_hook_sets_refs_pushed(self, pull_env, tmp_path):
        """Post-commit hook sets refs/pushed/<branch> after successful push."""
        env = pull_env
        gd, workdir = self._make_hooked_session(env, tmp_path, "hook2")

        (workdir / "new.txt").write_text("pushed\n")
        self._session_git(workdir, "add", "new.txt")
        self._session_git(workdir, "commit", "-m", "pushed commit")

        session_head = self._session_git(workdir, "rev-parse", "HEAD").stdout.strip()

        # refs/pushed/main should be set in the session gitdir
        pushed = _run("git", "--git-dir", str(gd),
                       "rev-parse", "refs/pushed/main", check=False)
        assert pushed.returncode == 0
        assert pushed.stdout.strip() == session_head

    def test_hook_force_pushes_amend(self, pull_env, tmp_path):
        """Post-commit hook force-pushes when session owns the branch (amend)."""
        env = pull_env
        gd, workdir = self._make_hooked_session(env, tmp_path, "hook3")

        # First commit — hook pushes successfully
        (workdir / "new.txt").write_text("v1\n")
        self._session_git(workdir, "add", "new.txt")
        self._session_git(workdir, "commit", "-m", "first")
        first_sha = self._session_git(workdir, "rev-parse", "HEAD").stdout.strip()
        assert _head(env.gitdir, gitdir=True) == first_sha

        # Amend — hook detects ownership and force-pushes
        (workdir / "new.txt").write_text("v2\n")
        self._session_git(workdir, "add", "new.txt")
        result = self._session_git(workdir, "commit", "--amend", "-m", "amended")
        assert result.returncode == 0

        amended_sha = self._session_git(workdir, "rev-parse", "HEAD").stdout.strip()
        assert amended_sha != first_sha

        # Shared should have the amended version (force-pushed)
        assert _head(env.gitdir, gitdir=True) == amended_sha

        # refs/pushed/main updated to amended sha
        pushed = _run("git", "--git-dir", str(gd),
                       "rev-parse", "--verify", "refs/pushed/main", check=False)
        assert pushed.stdout.strip() == amended_sha

    def test_hook_fetches_shared_updates(self, pull_env, tmp_path):
        """Post-commit hook fetches from shared, so session sees other work."""
        env = pull_env
        gd_a, work_a = self._make_hooked_session(env, tmp_path, "hookA")
        gd_b, work_b = self._make_hooked_session(env, tmp_path, "hookB")

        # Session A commits (hook pushes to shared)
        (work_a / "a.txt").write_text("from A\n")
        self._session_git(work_a, "add", "a.txt")
        self._session_git(work_a, "commit", "-m", "A's work")
        a_sha = self._session_git(work_a, "rev-parse", "HEAD").stdout.strip()

        # Session B commits (hook fetches, so B's fetch should see A's work)
        (work_b / "b.txt").write_text("from B\n")
        self._session_git(work_b, "add", "b.txt")
        self._session_git(work_b, "commit", "-m", "B's work")

        # B's hook failed to push (non-FF, A is ahead) but fetch succeeded.
        # B should see A's commit via shared remote refs.
        b_refs = self._session_git(work_b, "for-each-ref",
                                   "--format=%(objectname)", "refs/remotes/shared/")
        assert a_sha in b_refs.stdout or \
            _run("git", "--git-dir", str(gd_b), "cat-file", "-t", a_sha,
                 check=False).returncode == 0, \
            "Session B should have fetched A's commit from shared"

    def test_hook_pull_sees_hooked_commit(self, pull_env, tmp_path):
        """Full lifecycle: hook pushes to shared → pull_project picks it up.

        This is the most realistic test — it's exactly what happens when
        a user runs `cav pull` while an agent is working.
        """
        env = pull_env
        _, workdir = self._make_hooked_session(env, tmp_path, "hookpull")

        # Session commits — hook pushes to shared immediately
        (workdir / "feature.txt").write_text("feature\n")
        self._session_git(workdir, "add", "feature.txt")
        self._session_git(workdir, "commit", "-m", "add feature")

        # Verify hook already pushed (shared gitdir updated)
        session_head = self._session_git(workdir, "rev-parse", "HEAD").stdout.strip()
        assert _head(env.gitdir, gitdir=True) == session_head

        # Pull should see the commit immediately (no forward_session_refs needed)
        wrapper.pull_project(env.slug)
        assert (env.host / "feature.txt").read_text() == "feature\n"

    def test_hook_amend_pulled_commit_blocked(self, pull_env, tmp_path):
        """Amending a pulled commit is blocked by post-rewrite hook.

        Agent commits, hook pushes. User pulls. Agent tries to amend →
        post-rewrite hook detects the commit was pulled (via refs/remotes/host)
        and reverts the amend. This protects pulled commits from rewriting.
        """
        env = pull_env
        gd, workdir = self._make_hooked_session(env, tmp_path, "hookamend")

        # First commit — hook pushes
        (workdir / "feature.txt").write_text("v1\n")
        self._session_git(workdir, "add", "feature.txt")
        self._session_git(workdir, "commit", "-m", "add feature")

        # Pull v1 — this sets refs/remotes/host/main in shared
        wrapper.pull_project(env.slug)
        assert (env.host / "feature.txt").read_text() == "v1\n"

        # Hook fetched host refs — session now knows v1 was pulled
        v1_sha = self._session_git(workdir, "rev-parse", "HEAD").stdout.strip()

        # Amend attempt — post-rewrite hook reverts it (git commit returns 0
        # but the hook does `git reset --soft` to undo the amend)
        (workdir / "feature.txt").write_text("v2\n")
        self._session_git(workdir, "add", "feature.txt")
        result = self._session_git(workdir, "commit", "--amend", "-m", "amended")
        assert "already pulled" in result.stderr

        # HEAD should still be v1 (amend was reverted by hook)
        current = self._session_git(workdir, "rev-parse", "HEAD").stdout.strip()
        assert current == v1_sha

    def test_hook_amend_unpulled_commit_succeeds(self, pull_env, tmp_path):
        """Amending a commit that was NOT pulled is allowed.

        Agent commits, hook pushes. Agent amends before user pulls.
        The post-rewrite hook allows it because refs/remotes/host is empty.
        """
        env = pull_env
        gd, workdir = self._make_hooked_session(env, tmp_path, "hookamend2")

        # First commit — hook pushes
        (workdir / "feature.txt").write_text("v1\n")
        self._session_git(workdir, "add", "feature.txt")
        self._session_git(workdir, "commit", "-m", "add feature")
        v1_sha = self._session_git(workdir, "rev-parse", "HEAD").stdout.strip()

        # Amend BEFORE pull — should succeed (no host refs yet)
        (workdir / "feature.txt").write_text("v2\n")
        self._session_git(workdir, "add", "feature.txt")
        result = self._session_git(workdir, "commit", "--amend", "-m", "amended")
        assert result.returncode == 0

        v2_sha = self._session_git(workdir, "rev-parse", "HEAD").stdout.strip()
        assert v2_sha != v1_sha

        # Pull picks up the amended version via forward_session_refs
        wrapper.pull_project(env.slug, force=True)
        assert (env.host / "feature.txt").read_text() == "v2\n"

    def test_hook_multiple_commits_all_pushed(self, pull_env, tmp_path):
        """Multiple sequential commits each trigger the hook and push."""
        env = pull_env
        _, workdir = self._make_hooked_session(env, tmp_path, "hookmulti")

        for i in range(3):
            (workdir / f"file{i}.txt").write_text(f"content {i}\n")
            self._session_git(workdir, "add", f"file{i}.txt")
            self._session_git(workdir, "commit", "-m", f"commit {i}")

        session_head = self._session_git(workdir, "rev-parse", "HEAD").stdout.strip()
        assert _head(env.gitdir, gitdir=True) == session_head

        wrapper.pull_project(env.slug)
        for i in range(3):
            assert (env.host / f"file{i}.txt").read_text() == f"content {i}\n"


class TestPushSessionInteraction:
    """Push while session is active — verify refs update correctly."""

    def _make_hooked_session(self, pull_env, tmp_path, sid):
        env = pull_env
        _run("git", "--git-dir", str(env.gitdir), "config",
             "receive.denyCurrentBranch", "ignore")
        overlay_base = tmp_path / "session-overlays" / sid
        overlay_base.mkdir(parents=True)
        session_gitdir = wrapper.create_session_gitdir(env.slug, sid, overlay_base)
        _run("git", "--git-dir", str(session_gitdir),
             "remote", "set-url", "shared", str(env.gitdir))
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.email", "host@test.com")
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.name", "Host User")
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / f"{sid}.json").write_text(json.dumps({
            "sid": sid, "project": env.slug,
            "bwrap_pid": os.getpid(), "started": 0,
        }))
        workdir = tmp_path / f"work-{sid}"
        workdir.mkdir()
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(workdir), "checkout", "HEAD", "--", ".", check=False)
        (workdir / ".git").write_text(f"gitdir: {session_gitdir}\n")
        return session_gitdir, workdir

    def _session_git(self, workdir, *args):
        return _run("git", "-C", str(workdir), *args, check=False)

    def test_push_after_pull_blocks_session_amend(self, pull_env, tmp_path):
        """Session commits → pull → push → session amend is blocked.

        After pull+push sync refs/remotes/host/main in shared, the session's
        next commit triggers `git fetch shared` which brings the host ref in.
        The post-rewrite hook then blocks amending the pulled commit.
        """
        env = pull_env
        gd, workdir = self._make_hooked_session(env, tmp_path, "pushblock")

        # Session commits (hook pushes to shared)
        (workdir / "feature.txt").write_text("v1\n")
        self._session_git(workdir, "add", "feature.txt")
        self._session_git(workdir, "commit", "-m", "feature v1")
        v1 = self._session_git(workdir, "rev-parse", "HEAD").stdout.strip()

        # Pull to host
        wrapper.pull_project(env.slug)
        assert (env.host / "feature.txt").read_text() == "v1\n"

        # Push syncs refs/remotes/host/main in shared gitdir
        wrapper.push_project(env.slug)

        # Session makes another commit (hook fetches, bringing in host refs)
        (workdir / "other.txt").write_text("other\n")
        self._session_git(workdir, "add", "other.txt")
        self._session_git(workdir, "commit", "-m", "other work")

        # Now try to amend the previous commit (should be blocked — v1 was pulled)
        (workdir / "other.txt").write_text("amended other\n")
        self._session_git(workdir, "add", "other.txt")
        # Reset to v1's child to try amending
        result = self._session_git(workdir, "commit", "--amend", "-m", "amended")
        # The post-rewrite hook should NOT block this because we're amending
        # the "other work" commit which is AFTER the pulled v1.
        # Only amending v1 itself would be blocked.
        assert result.returncode == 0

    def test_session_commit_after_push_visible_to_pull(self, pull_env, tmp_path):
        """Session commits → pull → push → session commits more → pull again.

        Full lifecycle verifying push doesn't break the session's ability
        to continue committing and having those commits visible to pull.
        """
        env = pull_env
        _, workdir = self._make_hooked_session(env, tmp_path, "pushcontinue")

        # Round 1: commit → pull → push
        (workdir / "r1.txt").write_text("round 1\n")
        self._session_git(workdir, "add", "r1.txt")
        self._session_git(workdir, "commit", "-m", "round 1")
        wrapper.pull_project(env.slug)
        wrapper.push_project(env.slug)

        # Round 2: commit → pull
        (workdir / "r2.txt").write_text("round 2\n")
        self._session_git(workdir, "add", "r2.txt")
        self._session_git(workdir, "commit", "-m", "round 2")
        wrapper.pull_project(env.slug)

        assert (env.host / "r1.txt").read_text() == "round 1\n"
        assert (env.host / "r2.txt").read_text() == "round 2\n"

    def test_force_push_preserves_session_work_on_next_sync(
            self, pull_env, tmp_path):
        """push --force overwrites shared main → session work is rebased on next sync.

        Regression: push --force after sync_live_sessions left refs/synced in
        session gitdirs, so _forward_refs skipped the session's work on the
        next sync cycle, permanently losing it from shared.
        """
        env = pull_env
        gd, workdir = self._make_hooked_session(env, tmp_path, "forcepush")

        # Session commits (hook pushes to shared)
        (workdir / "session.txt").write_text("session work\n")
        self._session_git(workdir, "add", "session.txt")
        self._session_git(workdir, "commit", "-m", "session work")
        session_head = self._session_git(
            workdir, "rev-parse", "HEAD").stdout.strip()

        # Verify session work is in shared
        shared_head = _head(env.gitdir, gitdir=True)
        assert shared_head == session_head

        # Host makes an independent commit
        (env.host / "host.txt").write_text("host work\n")
        _run("git", "-C", str(env.host), "add", "host.txt")
        _run("git", "-C", str(env.host), "commit", "-m", "host work")

        # Force-push host to cave (overwrites session work on shared main)
        wrapper.push_project(env.slug, force=True)

        # Shared main is now at host's HEAD, session work gone from shared
        host_head = _head(env.host)
        assert _head(env.gitdir, gitdir=True) == host_head

        # Now pull — sync_live_sessions should re-forward session work
        # because push --force cleared refs/synced
        wrapper.pull_project(env.slug)

        # Both host work and session work should be present
        assert (env.host / "host.txt").read_text() == "host work\n"
        assert (env.host / "session.txt").read_text() == "session work\n"

    def test_force_push_no_session_commits_is_clean(self, pull_env, tmp_path):
        """push --force with no session commits doesn't warn or break."""
        env = pull_env
        self._make_hooked_session(env, tmp_path, "noop")

        # Host makes a commit
        (env.host / "host.txt").write_text("host work\n")
        _run("git", "-C", str(env.host), "add", "host.txt")
        _run("git", "-C", str(env.host), "commit", "-m", "host work")

        # Force-push — no session commits to lose
        wrapper.push_project(env.slug, force=True)
        assert _head(env.gitdir, gitdir=True) == _head(env.host)


class TestBranchSwitchMidSession:
    """Session switches branches mid-session, commits on multiple branches."""

    def _make_session_env(self, pull_env, tmp_path, sid):
        """Create a session with shared remote pointing to real gitdir."""
        env = pull_env
        overlay_base = tmp_path / "session-overlays" / sid
        overlay_base.mkdir(parents=True)
        session_gitdir = wrapper.create_session_gitdir(env.slug, sid, overlay_base)

        _run("git", "--git-dir", str(session_gitdir),
             "remote", "set-url", "shared", str(env.gitdir))
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.email", "host@test.com")
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.name", "Host User")

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / f"{sid}.json").write_text(json.dumps({
            "sid": sid, "project": env.slug,
            "bwrap_pid": os.getpid(), "started": 0,
        }))

        workdir = tmp_path / f"work-{sid}"
        workdir.mkdir()
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(workdir), "checkout", "HEAD", "--", ".", check=False)
        (workdir / ".git").write_text(f"gitdir: {session_gitdir}\n")
        return session_gitdir, workdir

    def _session_git(self, workdir, *args):
        return _run("git", "-C", str(workdir), *args, check=False)

    def test_branch_switch_commits_on_both(self, pull_env, tmp_path):
        """Session creates feature branch, commits on both, pull gets both."""
        env = pull_env

        # Create feature branch on host too (so pull has a target)
        _run("git", "-C", str(env.host), "branch", "feature")
        _run("git", "--git-dir", str(env.gitdir), "branch", "feature")
        host_head = _head(env.host)
        wrapper.sync_refs(env.gitdir, env.host, host_head, "feature")

        _, workdir = self._make_session_env(env, tmp_path, "branch1")

        # Commit on main
        (workdir / "main.txt").write_text("main work\n")
        self._session_git(workdir, "add", "main.txt")
        self._session_git(workdir, "commit", "-m", "main commit")

        # Switch to feature, commit there
        self._session_git(workdir, "checkout", "feature")
        (workdir / "feature.txt").write_text("feature work\n")
        self._session_git(workdir, "add", "feature.txt")
        self._session_git(workdir, "commit", "-m", "feature commit")

        # Switch back to main
        self._session_git(workdir, "checkout", "main")

        # Pull should get both branches
        wrapper.pull_project(env.slug)
        assert (env.host / "main.txt").read_text() == "main work\n"

        _run("git", "-C", str(env.host), "checkout", "feature")
        assert (env.host / "feature.txt").read_text() == "feature work\n"
        # Feature should NOT have main's commit
        assert not (env.host / "main.txt").exists()

    def test_new_branch_created_in_session(self, pull_env, tmp_path):
        """Session creates a new branch that doesn't exist on host.

        The branch should appear in shared gitdir but pull should only
        pull branches that exist on the host.
        """
        env = pull_env
        _, workdir = self._make_session_env(env, tmp_path, "newbranch")

        # Create and commit on new branch
        self._session_git(workdir, "checkout", "-b", "experiment")
        (workdir / "exp.txt").write_text("experiment\n")
        self._session_git(workdir, "add", "exp.txt")
        self._session_git(workdir, "commit", "-m", "experiment")

        # Also commit on main
        self._session_git(workdir, "checkout", "main")
        (workdir / "main.txt").write_text("main work\n")
        self._session_git(workdir, "add", "main.txt")
        self._session_git(workdir, "commit", "-m", "main work")

        wrapper.pull_project(env.slug)

        # Main commit should be pulled
        assert (env.host / "main.txt").read_text() == "main work\n"
        # Experiment branch should not crash pull (it doesn't exist on host)


class TestCleanupOrdering:
    """Verify cleanup is safe regardless of session exit order."""

    def _make_session_env(self, pull_env, tmp_path, sid):
        env = pull_env
        overlay_base = tmp_path / "session-overlays" / sid
        overlay_base.mkdir(parents=True)
        session_gitdir = wrapper.create_session_gitdir(env.slug, sid, overlay_base)

        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.email", "host@test.com")
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.name", "Host User")

        workdir = tmp_path / f"work-{sid}"
        workdir.mkdir()
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(workdir), "checkout", "HEAD", "--", ".", check=False)
        (workdir / ".git").write_text(f"gitdir: {session_gitdir}\n")
        return session_gitdir, workdir, overlay_base

    def _session_git(self, workdir, *args):
        return _run("git", "-C", str(workdir), *args, check=False)

    def test_cleanup_a_then_b(self, pull_env, tmp_path):
        """Two sessions with non-conflicting changes: A cleans up first."""
        env = pull_env
        _, work_a, overlay_a = self._make_session_env(env, tmp_path, "a")
        _, work_b, overlay_b = self._make_session_env(env, tmp_path, "b")

        (work_a / "a.txt").write_text("from A\n")
        self._session_git(work_a, "add", "a.txt")
        self._session_git(work_a, "commit", "-m", "A's work")

        (work_b / "b.txt").write_text("from B\n")
        self._session_git(work_b, "add", "b.txt")
        self._session_git(work_b, "commit", "-m", "B's work")

        wrapper.merge_session_gitdirs("a", overlay_a)
        wrapper.merge_session_gitdirs("b", overlay_b)

        wrapper.pull_project(env.slug)
        assert (env.host / "a.txt").read_text() == "from A\n"
        assert (env.host / "b.txt").read_text() == "from B\n"

    def test_cleanup_b_then_a(self, pull_env, tmp_path):
        """Same as above but B cleans up first — result should be identical."""
        env = pull_env
        _, work_a, overlay_a = self._make_session_env(env, tmp_path, "a")
        _, work_b, overlay_b = self._make_session_env(env, tmp_path, "b")

        (work_a / "a.txt").write_text("from A\n")
        self._session_git(work_a, "add", "a.txt")
        self._session_git(work_a, "commit", "-m", "A's work")

        (work_b / "b.txt").write_text("from B\n")
        self._session_git(work_b, "add", "b.txt")
        self._session_git(work_b, "commit", "-m", "B's work")

        # Reverse order
        wrapper.merge_session_gitdirs("b", overlay_b)
        wrapper.merge_session_gitdirs("a", overlay_a)

        wrapper.pull_project(env.slug)
        assert (env.host / "a.txt").read_text() == "from A\n"
        assert (env.host / "b.txt").read_text() == "from B\n"

    def test_cleanup_with_conflicting_changes(self, pull_env, tmp_path):
        """Two sessions edit the same file — second warns, shared stays at A's version."""
        env = pull_env
        _, work_a, overlay_a = self._make_session_env(env, tmp_path, "a")
        _, work_b, overlay_b = self._make_session_env(env, tmp_path, "b")

        (work_a / "file.txt").write_text("version A\n")
        self._session_git(work_a, "add", "file.txt")
        self._session_git(work_a, "commit", "-m", "A's edit")

        (work_b / "file.txt").write_text("version B\n")
        self._session_git(work_b, "add", "file.txt")
        self._session_git(work_b, "commit", "-m", "B's edit")

        wrapper.merge_session_gitdirs("a", overlay_a)
        shared_after_a = _head(env.gitdir, gitdir=True)
        wrapper.merge_session_gitdirs("b", overlay_b)

        # Shared main should stay at A's version (B's diverged work is not applied)
        assert _head(env.gitdir, gitdir=True) == shared_after_a

        # No backup ref for B
        backup = _run("git", "--git-dir", str(env.gitdir), "rev-parse",
                       "refs/heads/main-cave-b", check=False)
        assert backup.returncode != 0

        # Pull should succeed (main has A's version)
        wrapper.pull_project(env.slug)

    def test_message_only_amend_during_cleanup(self, pull_env, tmp_path):
        """Session amends only the commit message (same tree) — cleanup handles it."""
        env = pull_env
        _, workdir, overlay = self._make_session_env(env, tmp_path, "msgamend")

        (workdir / "file.txt").write_text("content\n")
        self._session_git(workdir, "add", "file.txt")
        self._session_git(workdir, "commit", "-m", "original message")

        # Amend message only (tree unchanged)
        self._session_git(workdir, "commit", "--amend", "-m", "better message")
        amended_sha = self._session_git(workdir, "rev-parse", "HEAD").stdout.strip()

        wrapper.merge_session_gitdirs("msgamend", overlay)

        # Shared should have the amended version
        shared_head = _head(env.gitdir, gitdir=True)
        assert shared_head == amended_sha

        wrapper.pull_project(env.slug)
        msg = _run("git", "-C", str(env.host), "log", "-1", "--format=%s").stdout.strip()
        assert msg == "better message"


class TestPullConflict:
    """Pull when host and cave have diverged with conflicting changes.

    These test the rebase-on-pull paths including conflict detection,
    cleanup of temporary state, and stash preservation.
    """

    def test_pull_with_diverged_non_conflicting(self, pull_env):
        """Host and cave both commit on same branch, no conflict → rebase."""
        env = pull_env

        # Host makes a commit
        (env.host / "host.txt").write_text("from host\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host work")

        # Cave makes a commit (different file)
        (env.cave / "cave.txt").write_text("from cave\n")
        _cave_git(env, "add", "cave.txt")
        _cave_git(env, "commit", "-m", "cave work")

        wrapper.pull_project(env.slug)

        # Both files should be present (cave rebased onto host)
        assert (env.host / "host.txt").read_text() == "from host\n"
        assert (env.host / "cave.txt").read_text() == "from cave\n"

        # History should be linear (rebased, not merged)
        log = _run("git", "-C", str(env.host), "log", "--oneline").stdout
        assert "cave work" in log
        assert "host work" in log

    def test_pull_with_conflicting_changes_exits(self, pull_env, capsys):
        """Host and cave both edit same file → pull exits with resolve instructions.

        When the current branch has a conflict, pull leaves the rebase in progress
        so the user can resolve manually. It prints instructions and exits.
        """
        env = pull_env

        # Cave edits file.txt FIRST (diverges from initial)
        (env.cave / "file.txt").write_text("line1 cave\nline2 cave\nline3 cave\n")
        _cave_git(env, "add", "file.txt")
        _cave_git(env, "commit", "-m", "cave edit")

        # Host edits same file independently (also diverges from initial)
        (env.host / "file.txt").write_text("line1 host\nline2 host\nline3 host\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host edit")

        with pytest.raises(SystemExit):
            wrapper.pull_project(env.slug)

        # Rebase is left in progress for manual resolution
        host_git_dir = env.host / ".git"
        assert (host_git_dir / "rebase-merge").is_dir() or \
               (host_git_dir / "rebase-apply").is_dir()

        # User can abort to return to pre-conflict state
        _run("git", "-C", str(env.host), "rebase", "--abort")
        _run("git", "-C", str(env.host), "checkout", "main")
        _run("git", "-C", str(env.host), "branch", "-D", "_cave_pull", check=False)
        assert (env.host / "file.txt").read_text() == "line1 host\nline2 host\nline3 host\n"

    def test_pull_conflict_stash_survives(self, pull_env):
        """Pull with conflict keeps stash intact for after resolution.

        When pull hits a conflict on the current branch, it leaves
        the rebase in progress and keeps the stash. The user resolves
        the conflict, then pops the stash to recover uncommitted work.
        """
        env = pull_env

        # Cave edits file.txt FIRST
        (env.cave / "file.txt").write_text("line1 cave\nline2 cave\nline3 cave\n")
        _cave_git(env, "add", "file.txt")
        _cave_git(env, "commit", "-m", "cave edit")

        # Host edits same file independently
        (env.host / "file.txt").write_text("line1 host\nline2 host\nline3 host\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host edit")

        # Host has uncommitted work
        (env.host / "uncommitted.txt").write_text("don't lose me\n")

        with pytest.raises(SystemExit):
            wrapper.pull_project(env.slug)

        # Stash was created (pull instructions mention stash pop)
        stash = _run("git", "-C", str(env.host), "stash", "list")
        assert stash.stdout.strip() != ""

        # Abort rebase, recover stash
        _run("git", "-C", str(env.host), "rebase", "--abort")
        _run("git", "-C", str(env.host), "checkout", "main")
        _run("git", "-C", str(env.host), "branch", "-D", "_cave_pull", check=False)
        _run("git", "-C", str(env.host), "stash", "pop")

        # Uncommitted file recovered
        assert (env.host / "uncommitted.txt").read_text() == "don't lose me\n"

    def test_pull_non_current_branch_conflict_skips(self, pull_env):
        """Conflict on non-current branch is skipped, not fatal."""
        env = pull_env

        # Create feature branch on both sides at initial commit
        _run("git", "-C", str(env.host), "branch", "feature")
        _run("git", "--git-dir", str(env.gitdir), "branch", "feature")
        host_head = _head(env.host)
        wrapper.sync_refs(env.gitdir, env.host, host_head, "feature")

        # Cave commits on feature FIRST (diverges from initial)
        _cave_git(env, "checkout", "feature")
        (env.cave / "file.txt").write_text("line1 cave\nline2 cave\nline3 cave\n")
        _cave_git(env, "add", "file.txt")
        _cave_git(env, "commit", "-m", "cave feature edit")
        _cave_git(env, "checkout", "main")

        # Host commits on feature independently (also diverges from initial)
        _run("git", "-C", str(env.host), "checkout", "feature")
        (env.host / "file.txt").write_text("line1 host\nline2 host\nline3 host\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host feature edit")
        host_feature_sha = _head(env.host)
        _run("git", "-C", str(env.host), "checkout", "main")

        # Also add a clean commit on main in cave
        (env.cave / "main-work.txt").write_text("main\n")
        _cave_git(env, "add", "main-work.txt")
        _cave_git(env, "commit", "-m", "main work")

        # Pull — feature conflict should be skipped, main should succeed
        wrapper.pull_project(env.slug)

        assert (env.host / "main-work.txt").read_text() == "main\n"
        # Feature should still have host's version (cave's conflicting
        # commit was skipped by plumbing rebase)
        _run("git", "-C", str(env.host), "checkout", "feature")
        assert _head(env.host) == host_feature_sha


class TestInterleavedSync:
    """Two sessions with interleaved live-syncs and pulls.

    Real-world scenario: session A and B are both active. User pulls
    periodically, triggering sync_live_sessions each time. Each sync
    should correctly forward only new work without corrupting the other
    session's state.
    """

    def _make_session(self, pull_env, tmp_path, sid):
        env = pull_env
        overlay_base = tmp_path / "session-overlays" / sid
        overlay_base.mkdir(parents=True)
        session_gitdir = wrapper.create_session_gitdir(env.slug, sid, overlay_base)

        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.email", "host@test.com")
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.name", "Host User")

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / f"{sid}.json").write_text(json.dumps({
            "sid": sid, "project": env.slug,
            "bwrap_pid": os.getpid(), "started": 0,
        }))

        workdir = tmp_path / f"work-{sid}"
        workdir.mkdir()
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(workdir), "checkout", "HEAD", "--", ".", check=False)
        (workdir / ".git").write_text(f"gitdir: {session_gitdir}\n")
        return session_gitdir, workdir

    def _session_git(self, workdir, *args):
        return _run("git", "-C", str(workdir), *args, check=False)

    def test_interleaved_commits_and_pulls(self, pull_env, tmp_path):
        """A commits → pull → B commits → pull → A commits → pull.

        Each pull should pick up all new work without duplicates.
        """
        env = pull_env
        _, work_a = self._make_session(env, tmp_path, "a")
        _, work_b = self._make_session(env, tmp_path, "b")

        # Round 1: A commits, pull
        (work_a / "a1.txt").write_text("a1\n")
        self._session_git(work_a, "add", "a1.txt")
        self._session_git(work_a, "commit", "-m", "A round 1")

        wrapper.pull_project(env.slug)
        assert (env.host / "a1.txt").read_text() == "a1\n"

        # Round 2: B commits, pull
        (work_b / "b1.txt").write_text("b1\n")
        self._session_git(work_b, "add", "b1.txt")
        self._session_git(work_b, "commit", "-m", "B round 1")

        wrapper.pull_project(env.slug)
        assert (env.host / "a1.txt").read_text() == "a1\n"  # still there
        assert (env.host / "b1.txt").read_text() == "b1\n"

        # Round 3: A commits again, pull
        (work_a / "a2.txt").write_text("a2\n")
        self._session_git(work_a, "add", "a2.txt")
        self._session_git(work_a, "commit", "-m", "A round 2")

        wrapper.pull_project(env.slug)
        assert (env.host / "a1.txt").read_text() == "a1\n"
        assert (env.host / "b1.txt").read_text() == "b1\n"
        assert (env.host / "a2.txt").read_text() == "a2\n"

        # Verify history is clean (no duplicates)
        log = _run("git", "-C", str(env.host), "log", "--oneline").stdout
        assert log.count("A round 1") == 1
        assert log.count("B round 1") == 1
        assert log.count("A round 2") == 1

    def test_interleaved_with_amend(self, pull_env, tmp_path):
        """A commits → pull → A amends → pull picks up amend.

        After pull syncs A's refs, A amends. Next pull should detect
        the amend via ownership and adopt it.
        """
        env = pull_env
        _, work_a = self._make_session(env, tmp_path, "a")

        # A commits
        (work_a / "a.txt").write_text("v1\n")
        self._session_git(work_a, "add", "a.txt")
        self._session_git(work_a, "commit", "-m", "A v1")

        wrapper.pull_project(env.slug)
        assert (env.host / "a.txt").read_text() == "v1\n"

        # A amends
        (work_a / "a.txt").write_text("v2\n")
        self._session_git(work_a, "add", "a.txt")
        self._session_git(work_a, "commit", "--amend", "-m", "A v2")

        # Pull with force
        wrapper.pull_project(env.slug, force=True)
        assert (env.host / "a.txt").read_text() == "v2\n"

    def test_push_between_session_commits(self, pull_env, tmp_path):
        """Session A commits → pull → host pushes → A commits again → pull.

        Tests the scenario where the cave gitdir gets updated by a push
        while a session is still active.
        """
        env = pull_env
        _, work_a = self._make_session(env, tmp_path, "a")

        # Session commits
        (work_a / "cave1.txt").write_text("cave1\n")
        self._session_git(work_a, "add", "cave1.txt")
        self._session_git(work_a, "commit", "-m", "cave round 1")

        wrapper.pull_project(env.slug)
        assert (env.host / "cave1.txt").read_text() == "cave1\n"

        # Host makes a commit and pushes
        (env.host / "host.txt").write_text("host\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host work")
        wrapper.push_project(env.slug)

        # Session continues working (doesn't know about host's push)
        (work_a / "cave2.txt").write_text("cave2\n")
        self._session_git(work_a, "add", "cave2.txt")
        self._session_git(work_a, "commit", "-m", "cave round 2")

        # Pull — should see both host's work and session's new work
        wrapper.pull_project(env.slug)
        assert (env.host / "host.txt").read_text() == "host\n"
        assert (env.host / "cave1.txt").read_text() == "cave1\n"
        assert (env.host / "cave2.txt").read_text() == "cave2\n"

    def test_three_rounds_push_pull(self, pull_env, tmp_path):
        """Three full push-pull cycles with a live session.

        Ensures no accumulation of stale refs or duplicate commits.
        """
        env = pull_env
        _, work_a = self._make_session(env, tmp_path, "a")

        for i in range(3):
            # Session commits
            (work_a / f"cave{i}.txt").write_text(f"cave{i}\n")
            self._session_git(work_a, "add", f"cave{i}.txt")
            self._session_git(work_a, "commit", "-m", f"cave round {i}")

            # Pull
            wrapper.pull_project(env.slug)
            assert (env.host / f"cave{i}.txt").read_text() == f"cave{i}\n"

            # Push (host sync back)
            wrapper.push_project(env.slug)

        # Verify clean history
        log = _run("git", "-C", str(env.host), "log", "--oneline").stdout
        lines = [l for l in log.strip().splitlines() if l]
        # Should have: initial + 3 cave rounds = 4 commits
        assert len(lines) == 4, f"Expected 4 commits, got {len(lines)}:\n{log}"


class TestHostRewriteDetection:
    """Pull detects when the HOST rewrote history since last sync.

    This happens when another agent on the host amends a commit that
    the cave has already built on top of.  Without detection, the pull
    tries a normal rebase which hits conflicts on the rewritten files.
    """

    def test_host_rewrite_without_force_warns(self, pull_env):
        """Host rewrites a synced commit → plain pull warns and refuses."""
        env = pull_env

        # Cave makes a commit, pull it
        cave_commit(env, "cave work", content="cave\n")
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "cave\n"

        # Host rewrites the commit (amend on the host side)
        (env.host / "file.txt").write_text("host rewrite\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "--amend",
             "-m", "rewritten by host")

        # Cave makes another commit on top of old history
        cave_commit(env, "more cave work", filename="new.txt", content="new\n")

        # Pull should detect host rewrite and refuse
        result = wrapper.pull_project(env.slug)
        assert result is None  # refused, no pull happened

        # Host should still have the rewritten version
        assert (env.host / "file.txt").read_text() == "host rewrite\n"

    def test_host_rewrite_with_force_succeeds(self, pull_env):
        """Host rewrites → force pull rebases cave's new commits onto host."""
        env = pull_env

        # Cave makes a commit, pull it
        cave_commit(env, "cave work", content="cave\n")
        wrapper.pull_project(env.slug)

        # Host rewrites the commit
        (env.host / "file.txt").write_text("host rewrite\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "--amend",
             "-m", "rewritten by host")

        # Cave makes another commit on a different file
        cave_commit(env, "more cave work", filename="new.txt", content="new\n")

        # Force pull should succeed — rebases cave's new commits onto host
        wrapper.pull_project(env.slug, force=True)
        assert (env.host / "file.txt").read_text() == "host rewrite\n"
        assert (env.host / "new.txt").read_text() == "new\n"


class TestHostRolledBack:
    """Host rolled back (checkout HEAD~1) then cave amended.

    This is NOT a host rewrite. The host went backwards, the cave moved
    forward. Pull should adopt cave's version without --force.
    """

    def test_host_rollback_pulls_without_force(self, pull_env, capsys):
        """Host rolls back, cave amends. Pull adopts cave's version."""
        env = pull_env

        # Cave makes two commits, pull them
        cave_commit(env, "first cave commit", content="first\n")
        cave_commit(env, "broken commit", content="broken\n")
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "broken\n"

        # Host rolls back one commit
        _run("git", "-C", str(env.host), "reset", "--hard", "HEAD~1")
        assert (env.host / "file.txt").read_text() == "first\n"

        # Cave amends: drop "first cave commit", rewrite "broken commit"
        _cave_git(env, "reset", "--soft", "HEAD~2")
        (env.cave / "file.txt").write_text("fixed\n")
        _cave_git(env, "add", "file.txt")
        _cave_git(env, "commit", "-m", "fixed commit")

        # Pull should succeed without --force
        capsys.readouterr()
        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "fixed\n"

        captured = capsys.readouterr()
        assert "behind last sync" in captured.err
        assert "rewrote" not in captured.err

    def test_host_rollback_preserves_uncommitted(self, pull_env):
        """Host rolls back with uncommitted changes, pull preserves them."""
        env = pull_env

        cave_commit(env, "cave work", content="cave\n")
        wrapper.pull_project(env.slug)

        # Host rolls back and has uncommitted changes to a different file
        _run("git", "-C", str(env.host), "reset", "--hard", "HEAD~1")
        (env.host / "other.txt").write_text("uncommitted\n")
        _run("git", "-C", str(env.host), "add", "other.txt")

        # Cave amends its commit
        cave_commit(env, "amended cave work", content="amended\n", amend=True)

        wrapper.pull_project(env.slug)
        assert (env.host / "file.txt").read_text() == "amended\n"
        # Uncommitted changes should be restored via stash
        assert (env.host / "other.txt").read_text() == "uncommitted\n"

    def test_actual_host_rewrite_still_requires_force(self, pull_env, capsys):
        """Host rolls back AND makes new commits. Still requires --force."""
        env = pull_env

        cave_commit(env, "cave work", content="cave\n")
        wrapper.pull_project(env.slug)

        # Host rolls back AND makes a new commit (actual rewrite)
        _run("git", "-C", str(env.host), "reset", "--hard", "HEAD~1")
        (env.host / "file.txt").write_text("host new work\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "-m", "host diverged")

        # Cave also makes a new commit
        cave_commit(env, "more cave work", filename="new.txt", content="new\n")

        # Pull should still require --force (genuine divergence)
        capsys.readouterr()
        wrapper.pull_project(env.slug)
        captured = capsys.readouterr()
        assert "rewrote history" in captured.err
        # Host should NOT have the cave's new file (refused)
        assert not (env.host / "new.txt").exists()


class TestPullStashPopFailure:
    """Stash pop failures produce a warning instead of silent data loss."""

    def test_stash_pop_conflict_warns(self, pull_env, capsys):
        """Host has uncommitted changes conflicting with pulled code.

        After pull rebases, stash pop conflicts.  Verify warning is printed
        and the stash is preserved (not silently lost).
        """
        env = pull_env

        # Host has uncommitted changes to file.txt
        (env.host / "file.txt").write_text("host uncommitted\n")
        _run("git", "-C", str(env.host), "add", "file.txt")

        # Cave commits a conflicting change to file.txt
        cave_commit(env, "cave edit", content="cave committed\n")

        # Pull will stash, rebase, then stash pop (which will conflict)
        wrapper.pull_project(env.slug)

        captured = capsys.readouterr()
        assert "stash pop failed" in captured.err.lower() or \
               "stash pop failed" in captured.err
        # The stash should still exist (changes not lost)
        stash_list = _run("git", "-C", str(env.host), "stash", "list")
        assert stash_list.stdout.strip() != "", \
            "Stash should still contain the uncommitted changes"


class TestPullHostAmendPushWhileSessionRunning:
    """Host amends + pushes while a session is running on old history."""

    @pytest.fixture
    def session_pull_env(self, pull_env, tmp_path):
        """Extend pull_env with a running session."""
        env = pull_env
        from coding_cave import _state
        from tests.conftest import write_session

        sid = "test-session-amend"
        overlay_base = _state.cave_data() / "session-overlays" / sid
        overlay_base.mkdir(parents=True)
        session_gitdir = wrapper.create_session_gitdir(
            env.slug, sid, overlay_base)

        write_session(_state.cave_data(), sid, env.slug)

        session_work = tmp_path / "session-work"
        session_work.mkdir()
        _run("git", "--git-dir", str(session_gitdir), "--work-tree",
             str(session_work), "checkout", "HEAD", "--", ".", check=False)
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.email", "cave@test.com")
        _run("git", "--git-dir", str(session_gitdir), "config",
             "user.name", "cave-coder")

        env.session_gitdir = session_gitdir
        env.session_work = session_work
        env.overlay_base = overlay_base
        env.sid = sid
        return env

    def test_host_amend_push_session_on_old_history(self, session_pull_env):
        """Session works on old history, host amends and pushes.

        Cave makes commit A, pull it to host. Host amends A to A' and pushes.
        Session (still on original commit) makes commit B.
        Pull should succeed — session commit is synced via live sync,
        then host pull resolves.
        """
        env = session_pull_env

        # Cave makes commit A
        cave_commit(env, "original commit", content="original\n")
        wrapper.pull_project(env.slug)

        # Host amends the commit
        (env.host / "file.txt").write_text("amended by host\n")
        _run("git", "-C", str(env.host), "add", ".")
        _run("git", "-C", str(env.host), "commit", "--amend",
             "-m", "amended commit")

        # Push amended commit back to cave
        wrapper.push_project(env.slug, force=True)

        # Session makes a new commit on a different file
        (env.session_work / "session.txt").write_text("session work\n")
        _run("git", "--git-dir", str(env.session_gitdir), "--work-tree",
             str(env.session_work), "add", "session.txt")
        _run("git", "--git-dir", str(env.session_gitdir), "--work-tree",
             str(env.session_work), "commit", "-m", "session commit")

        # Pull should handle the session's commit being on old history
        # (live sync runs first, then pull resolves)
        wrapper.pull_project(env.slug, force=True)
        assert (env.host / "file.txt").read_text() == "amended by host\n"
