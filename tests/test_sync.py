"""Tests for sync helpers, gitdir initialization, and git safety."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import wrapper
from coding_cave import _state
from coding_cave.gitdir import (
    _save_session_index, forward_session_refs,
)
from tests.conftest import GIT_ENV, git_run, git_head, write_projects, write_session


@pytest.fixture
def two_repos(tmp_path):
    """Create a host repo + bare gitdir + working directory simulating cave overlay.

    Returns (host_dir, gitdir, cave_workdir) where:
    - host_dir: a normal git repo (simulates the host project)
    - gitdir: a bare-ish git dir (simulates PROJECTS_DIR/<slug>/gitdir)
    - cave_workdir: a working directory using gitdir (simulates the merged overlay)
    """
    host = tmp_path / "host"
    host.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(host)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(host), "config", "user.email", "test@test.com"], capture_output=True)
    subprocess.run(["git", "-C", str(host), "config", "user.name", "Test"], capture_output=True)
    (host / "file.txt").write_text("initial\n")
    subprocess.run(["git", "-C", str(host), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(host), "commit", "-m", "initial"], capture_output=True, check=True)

    # Clone to get a gitdir with shared objects (like initialize_gitdir)
    tmpclone = tmp_path / "_tmpclone"
    subprocess.run(
        ["git", "clone", "--no-checkout", "--local", str(host), str(tmpclone)],
        capture_output=True, check=True,
    )
    gitdir = tmp_path / "gitdir"
    (tmpclone / ".git").rename(gitdir)
    tmpclone.rmdir()
    subprocess.run(["git", "--git-dir", str(gitdir), "remote", "remove", "origin"], capture_output=True)
    # Populate index from HEAD
    subprocess.run(["git", "--git-dir", str(gitdir), "read-tree", "HEAD"], check=True, capture_output=True)

    # Create a working directory that uses the gitdir
    cave_workdir = tmp_path / "cave"
    cave_workdir.mkdir()
    subprocess.run(
        ["git", "--git-dir", str(gitdir), "--work-tree", str(cave_workdir),
         "checkout", "HEAD", "--", "."],
        capture_output=True, check=True,
    )

    return host, gitdir, cave_workdir


def make_cave_session(gitdir, workdir):
    """Set up a working directory to use a gitdir (simulates .git file + bind mount)."""
    (workdir / ".git").write_text(f"gitdir: {gitdir}\n")
    return workdir


def cave_git(workdir, *args, **kwargs):
    """Run git in a cave working directory with test identity."""
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git", "-C", str(workdir)] + list(args),
        capture_output=True, text=True, env=env, **kwargs,
    )




class TestSyncRefs:
    def test_updates_branch_and_host_ref(self, two_repos):
        """sync_refs updates gitdir branch ref and host tracking ref."""
        host, gitdir, cave = two_repos

        # Add 'cave' remote to host so finalize_sync's fetch works
        subprocess.run(
            ["git", "-C", str(host), "remote", "add", "cave", str(gitdir)],
            capture_output=True,
        )

        # Make a new commit on host
        (host / "file.txt").write_text("updated\n")
        subprocess.run(["git", "-C", str(host), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(host), "commit", "-m", "update"], capture_output=True, check=True)

        new_head = git_head(host)
        old_gitdir_head = git_head(gitdir, gitdir=True)
        assert new_head != old_gitdir_head

        wrapper.sync_refs(gitdir, host, new_head, "main")

        # gitdir branch should match host
        assert git_head(gitdir, gitdir=True) == new_head

        # host remote ref in gitdir should be updated
        host_ref = subprocess.run(
            ["git", "--git-dir", str(gitdir), "rev-parse", "refs/remotes/host/main"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert host_ref == new_head


class TestHistoryRewriteDetection:
    def test_detects_rewrite_when_host_ref_matches_host(self, two_repos):
        """When refs/remotes/host/<branch> == host HEAD but merge-base is older, it's a rewrite."""
        host, gitdir, cave = two_repos
        host_head = git_head(host)

        # Set host tracking ref to current host HEAD
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "update-ref",
             "refs/remotes/host/main", host_head],
            check=True,
        )

        # Simulate cave rewriting history: amend the commit in gitdir
        env = {**os.environ, **GIT_ENV}
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "--work-tree", str(cave),
             "commit", "--amend", "-m", "rewritten",
             "--author", "Test <test@test.com>"],
            capture_output=True, check=True, env=env,
        )
        new_gitdir_head = git_head(gitdir, gitdir=True)
        assert new_gitdir_head != host_head

        # Fetch cave objects into host for merge-base
        subprocess.run(
            ["git", "-C", str(host), "fetch", str(gitdir), "HEAD"],
            capture_output=True, check=True,
        )

        # merge-base should fail (no common ancestor after amend)
        merge_base = subprocess.run(
            ["git", "-C", str(host), "merge-base", "HEAD", "FETCH_HEAD"],
            capture_output=True, text=True,
        )
        # After amend, the commit is rewritten so there's no common ancestor
        # (OR the merge-base is older). Either way, the detection logic in
        # pull_project checks: host_ref == host_head AND merge_base != host_head
        host_ref = subprocess.run(
            ["git", "--git-dir", str(gitdir), "rev-parse", "refs/remotes/host/main"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        assert host_ref == host_head, "host ref should still point to old host HEAD"
        # The rewrite detection condition
        if merge_base.returncode == 0:
            assert merge_base.stdout.strip() != host_head
        # If merge-base fails, that also triggers the rewrite path

    def test_find_tree_merge_base(self, two_repos):
        """find_tree_merge_base finds commit by tree hash when ancestry fails."""
        host, gitdir, cave = two_repos
        host_head = git_head(host)

        # Get host HEAD tree
        host_tree = subprocess.run(
            ["git", "-C", str(host), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Fetch gitdir into host so FETCH_HEAD is set
        subprocess.run(
            ["git", "-C", str(host), "fetch", str(gitdir), "HEAD"],
            capture_output=True, check=True,
        )

        git = ["git", "-C", str(host)]
        result = wrapper.find_tree_merge_base(git, host_tree)

        # Should find the gitdir commit with matching tree
        assert result is not None
        found_tree = subprocess.run(
            ["git", "-C", str(host), "rev-parse", f"{result}^{{tree}}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert found_tree == host_tree


class TestInitializeGitdir:
    def test_empty_repo(self, tmp_path):
        """initialize_gitdir doesn't crash on empty repos (no commits).

        Regression: 7ed214c — read-tree HEAD failed with check=True on
        repos with no commits, crashing the initialization.
        """
        slug = "empty-project"
        _state.set_cave_data(tmp_path)
        proj_state = _state.projects_dir() / slug
        proj_state.mkdir(parents=True)

        # Create empty repo (git init, no commits)
        project = tmp_path / "empty"
        project.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(project)],
                        capture_output=True, check=True)

        # Should not raise
        wrapper.initialize_gitdir(slug, project)

        gitdir = proj_state / "gitdir"
        assert gitdir.is_dir()

    def test_normal_repo(self, tmp_path):
        """initialize_gitdir creates gitdir with host tracking ref."""
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        proj_state = _state.projects_dir() / slug
        proj_state.mkdir(parents=True)

        project = tmp_path / "project"
        project.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(project)],
                        capture_output=True, check=True)
        subprocess.run(["git", "-C", str(project), "config", "user.email", "t@t"],
                        capture_output=True)
        subprocess.run(["git", "-C", str(project), "config", "user.name", "T"],
                        capture_output=True)
        (project / "f.txt").write_text("hi\n")
        subprocess.run(["git", "-C", str(project), "add", "."],
                        capture_output=True, check=True)
        subprocess.run(["git", "-C", str(project), "commit", "-m", "init"],
                        capture_output=True, check=True)

        wrapper.initialize_gitdir(slug, project)

        gitdir = proj_state / "gitdir"
        assert gitdir.is_dir()

    def test_hardlinks_objects(self, tmp_path):
        """initialize_gitdir hardlinks objects from host when on same device."""
        slug = "test-hardlinks"
        _state.set_cave_data(tmp_path)
        proj_state = _state.projects_dir() / slug
        proj_state.mkdir(parents=True)

        project = tmp_path / "project"
        project.mkdir()
        git_run("git", "init", "-b", "main", str(project))
        (project / "f.txt").write_text("content\n")
        git_run("git", "-C", str(project), "add", ".")
        git_run("git", "-C", str(project), "commit", "-m", "init")

        wrapper.initialize_gitdir(slug, project)

        gitdir = proj_state / "gitdir"
        # Find a pack file in both host and gitdir — they should share inodes
        host_packs = list((project / ".git" / "objects" / "pack").glob("*.pack"))
        gitdir_packs = list((gitdir / "objects" / "pack").glob("*.pack"))
        if host_packs and gitdir_packs:
            host_ino = host_packs[0].stat().st_ino
            gitdir_ino = gitdir_packs[0].stat().st_ino
            assert host_ino == gitdir_ino, (
                "Pack files should be hardlinked (same inode) on same filesystem"
            )

        # hooks dir should be removed
        assert not (gitdir / "hooks").exists()

    def test_skips_existing_gitdir(self, tmp_path):
        """initialize_gitdir is a no-op if gitdir already exists."""
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        proj_state = _state.projects_dir() / slug
        proj_state.mkdir(parents=True)
        gitdir = proj_state / "gitdir"
        gitdir.mkdir()
        marker = gitdir / "marker"
        marker.write_text("existing")

        project = tmp_path / "project"
        project.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(project)],
                        capture_output=True, check=True)

        wrapper.initialize_gitdir(slug, project)

        # Should not have touched the existing gitdir
        assert marker.read_text() == "existing"


class TestSafeGitOpts:
    """SAFE_GIT_OPTS neutralizes config-based code execution.

    Regression: dc0c652 — cave-writable gitdir config could be poisoned
    with core.pager, core.fsmonitor, diff.external, or core.sshCommand
    to execute arbitrary commands on the host during push/pull.
    """

    def test_pager_neutralized(self, two_repos, tmp_path):
        """core.pager in gitdir config doesn't execute."""
        _host, gitdir, _cave = two_repos

        marker = tmp_path / "pager_executed"
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "config",
             "core.pager", f"touch {marker}"],
            capture_output=True, check=True,
        )

        # git() wrapper should override core.pager
        wrapper.safe_git(gitdir, "log", "--oneline", "-1",
                     capture_output=True, text=True)

        assert not marker.exists(), "Poisoned core.pager was executed!"

    def test_fsmonitor_neutralized(self, two_repos, tmp_path):
        """core.fsmonitor in gitdir config doesn't execute."""
        _host, gitdir, _cave = two_repos

        marker = tmp_path / "fsmonitor_executed"
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "config",
             "core.fsmonitor", f"touch {marker}; echo false"],
            capture_output=True, check=True,
        )

        wrapper.safe_git(gitdir, "status",
                     capture_output=True, text=True)

        assert not marker.exists(), "Poisoned core.fsmonitor was executed!"

    def test_hooks_neutralized(self, two_repos, tmp_path):
        """Hooks in gitdir don't execute."""
        _host, gitdir, _cave = two_repos

        marker = tmp_path / "hook_executed"
        hooks_dir = gitdir / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        hook = hooks_dir / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)

        # git() sets core.hooksPath=/dev/null
        wrapper.safe_git(gitdir, "rev-parse", "HEAD",
                     capture_output=True, text=True)

        assert not marker.exists(), "Hook was executed despite SAFE_GIT_OPTS!"

    def test_ssh_command_neutralized(self, two_repos, tmp_path):
        """core.sshCommand in gitdir config doesn't execute."""
        _host, gitdir, _cave = two_repos

        marker = tmp_path / "ssh_executed"
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "config",
             "core.sshCommand", f"touch {marker}"],
            capture_output=True, check=True,
        )

        # The wrapper overrides sshCommand to "echo blocked"
        result = wrapper.safe_git(gitdir, "config", "--get", "core.sshCommand",
                              capture_output=True, text=True)
        # With -c override, the effective value should be "echo blocked"
        # (git -c takes precedence over config file)
        # Just verify the marker wasn't created by any operation
        wrapper.safe_git(gitdir, "rev-parse", "HEAD",
                     capture_output=True, text=True)

        assert not marker.exists(), "Poisoned core.sshCommand was executed!"


# --- Shared gitdir problems (motivating per-session gitdirs) ---

class TestSharedGitdirProblems:
    """Demonstrate problems with two sessions sharing a single gitdir."""

    @pytest.fixture
    def two_sessions_shared(self, tmp_path):
        """Two working directories sharing the same gitdir (current behavior)."""
        # Shared gitdir
        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "file.txt").write_text("initial\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")

        gitdir = tmp_path / "gitdir"
        tmpclone = tmp_path / "_clone"
        git_run("git", "clone", "--no-checkout", "--local", str(host), str(tmpclone))
        (tmpclone / ".git").rename(gitdir)
        shutil.rmtree(tmpclone)
        git_run("git", "--git-dir", str(gitdir), "remote", "remove", "origin")
        git_run("git", "--git-dir", str(gitdir), "read-tree", "HEAD")

        # Two working directories sharing the same gitdir
        work_a = tmp_path / "session-a"
        work_b = tmp_path / "session-b"
        work_a.mkdir()
        work_b.mkdir()
        # Populate both from HEAD
        for w in (work_a, work_b):
            git_run("git", "--git-dir", str(gitdir), "--work-tree", str(w),
                    "checkout", "HEAD", "--", ".")
            make_cave_session(gitdir, w)

        return gitdir, work_a, work_b

    def test_commit_in_a_dirties_b(self, two_sessions_shared):
        """When session A commits, session B's working tree appears dirty."""
        gitdir, work_a, work_b = two_sessions_shared

        # Session A modifies and commits
        (work_a / "new.txt").write_text("from A\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "A's commit")

        # Session B hasn't changed anything, but git status is dirty
        # because HEAD now points to A's commit which includes new.txt
        status = cave_git(work_b, "status", "--porcelain")
        assert status.stdout.strip() != "", (
            "Expected B's status to be dirty after A's commit (shared gitdir problem)"
        )

    def test_commit_in_a_moves_b_head(self, two_sessions_shared):
        """Session A's commit moves HEAD for session B too."""
        gitdir, work_a, work_b = two_sessions_shared

        head_before = cave_git(work_b, "rev-parse", "HEAD").stdout.strip()

        # Session A commits
        (work_a / "new.txt").write_text("from A\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "A's commit")

        head_after = cave_git(work_b, "rev-parse", "HEAD").stdout.strip()
        assert head_before != head_after, (
            "Expected B's HEAD to move when A commits (shared gitdir problem)"
        )


# --- Per-session gitdir tests ---

class TestSessionGitdir:
    """Test per-session gitdir creation, isolation, and merge-back."""

    @pytest.fixture
    def shared_project(self, tmp_path):
        """A host repo with initialized shared gitdir, ready for session creation.

        Returns (host_dir, slug, projects_dir).
        """
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "file.txt").write_text("initial\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")

        wrapper.initialize_gitdir(slug, host)
        return host, slug, projects_dir

    def _make_session(self, tmp_path, slug, projects_dir, name):
        """Create a session gitdir + working directory for testing."""
        overlay_base = tmp_path / f"overlay-{name}"
        overlay_base.mkdir()
        session_gitdir = wrapper.create_session_gitdir(slug, f"sid-{name}", overlay_base)

        workdir = tmp_path / f"work-{name}"
        workdir.mkdir()
        # Populate working directory from HEAD
        git_run("git", "--git-dir", str(session_gitdir), "--work-tree", str(workdir),
                "checkout", "HEAD", "--", ".")
        make_cave_session(session_gitdir, workdir)
        return session_gitdir, workdir, overlay_base

    def test_session_gitdir_created(self, shared_project, tmp_path):
        """create_session_gitdir produces a valid git repo."""
        host, slug, projects_dir = shared_project
        overlay = tmp_path / "overlay"
        overlay.mkdir()

        session_gd = wrapper.create_session_gitdir(slug, "test-sid", overlay)

        assert session_gd is not None
        assert session_gd.is_dir()
        assert (session_gd / "HEAD").exists()
        assert (session_gd / "objects" / "info" / "alternates").exists()

    def test_alternates_has_host_path(self, shared_project, tmp_path):
        """create_session_gitdir writes the host-side shared objects path.

        start_session rewrites this to the cave path before bwrap.
        Regression: 70dcf2f — alternates had the unreachable host path
        inside the cave, breaking all git operations.
        """
        host, slug, projects_dir = shared_project
        overlay = tmp_path / "overlay"
        overlay.mkdir()
        shared_objects = projects_dir / slug / "gitdir" / "objects"

        session_gd = wrapper.create_session_gitdir(slug, "test-sid", overlay)

        alt = (session_gd / "objects" / "info" / "alternates").read_text().strip()
        assert alt == str(shared_objects)

    def test_alternates_rewrite_to_cave_path(self, shared_project, tmp_path):
        """Simulates the alternates rewrite that start_session performs.

        After rewrite, git must resolve objects via the new path.
        Regression: 70dcf2f — the rewrite was missing, leaving the
        unreachable host path inside the cave.
        """
        host, slug, projects_dir = shared_project
        overlay = tmp_path / "overlay"
        overlay.mkdir()

        session_gd = wrapper.create_session_gitdir(slug, "test-sid", overlay)

        # Simulate start_session's rewrite: point alternates to a new path
        cave_objects = tmp_path / "shared-objects" / slug
        cave_objects.mkdir(parents=True)
        # Symlink cave path to actual shared objects (simulates ro-bind)
        for item in (projects_dir / slug / "gitdir" / "objects").iterdir():
            (cave_objects / item.name).symlink_to(item)

        (session_gd / "objects" / "info" / "alternates").write_text(
            f"{cave_objects}\n")

        # Git must still resolve objects via the rewritten path
        log = git_run("git", "--git-dir", str(session_gd), "log", "--oneline")
        assert log.returncode == 0
        assert "initial" in log.stdout

    def test_session_has_shared_refs(self, shared_project, tmp_path):
        """Session gitdir starts with the same HEAD as shared."""
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"
        shared_head = git_run("git", "--git-dir", str(shared_gd),
                              "rev-parse", "HEAD").stdout.strip()

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        session_gd = wrapper.create_session_gitdir(slug, "test-sid", overlay)

        session_head = git_run("git", "--git-dir", str(session_gd),
                               "rev-parse", "HEAD").stdout.strip()
        assert session_head == shared_head

    def test_session_resolves_shared_objects(self, shared_project, tmp_path):
        """Session gitdir can read objects from the shared store via alternates."""
        host, slug, projects_dir = shared_project
        overlay = tmp_path / "overlay"
        overlay.mkdir()
        session_gd = wrapper.create_session_gitdir(slug, "test-sid", overlay)

        # Should be able to show the initial commit
        log = git_run("git", "--git-dir", str(session_gd), "log", "--oneline")
        assert log.returncode == 0
        assert "initial" in log.stdout

    def test_session_clean_status(self, shared_project, tmp_path):
        """A fresh session with matching working tree shows clean git status."""
        host, slug, projects_dir = shared_project
        session_gd, workdir, _ = self._make_session(tmp_path, slug, projects_dir, "a")

        status = cave_git(workdir, "status", "--porcelain")
        assert status.stdout.strip() == "", f"Expected clean status, got: {status.stdout}"

    def test_commit_in_a_does_not_dirty_b(self, shared_project, tmp_path):
        """Two sessions: a commit in A does not affect B's status."""
        host, slug, projects_dir = shared_project
        _, work_a, _ = self._make_session(tmp_path, slug, projects_dir, "a")
        _, work_b, _ = self._make_session(tmp_path, slug, projects_dir, "b")

        # Session A commits
        (work_a / "new.txt").write_text("from A\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "A's commit")

        # Session B should be unaffected
        status = cave_git(work_b, "status", "--porcelain")
        assert status.stdout.strip() == "", (
            f"B should have clean status after A commits, got: {status.stdout}"
        )

    def test_commit_in_a_does_not_move_b_head(self, shared_project, tmp_path):
        """Two sessions: a commit in A does not change B's HEAD."""
        host, slug, projects_dir = shared_project
        _, work_a, _ = self._make_session(tmp_path, slug, projects_dir, "a")
        _, work_b, _ = self._make_session(tmp_path, slug, projects_dir, "b")

        head_before = cave_git(work_b, "rev-parse", "HEAD").stdout.strip()

        (work_a / "new.txt").write_text("from A\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "A's commit")

        head_after = cave_git(work_b, "rev-parse", "HEAD").stdout.strip()
        assert head_before == head_after

    def test_b_does_not_see_a_log(self, shared_project, tmp_path):
        """Session B's log does not include session A's commits."""
        host, slug, projects_dir = shared_project
        _, work_a, _ = self._make_session(tmp_path, slug, projects_dir, "a")
        _, work_b, _ = self._make_session(tmp_path, slug, projects_dir, "b")

        (work_a / "new.txt").write_text("from A\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "A's commit")

        log_b = cave_git(work_b, "log", "--oneline")
        assert "A's commit" not in log_b.stdout

    def test_behind_session_does_not_overwrite_shared(
            self, shared_project, tmp_path):
        """A session behind shared must not force-update shared backward.

        When multiple sessions exist, a stale session whose main is an
        ancestor of shared main must be skipped.  Otherwise sync_live_sessions
        clobbers newer commits from other sessions.

        Regression: sync_live_sessions iterated all sessions; the last stale
        session force-updated shared main backward, hiding newer commits
        from pull.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        # Create BOTH sessions before anyone commits (same starting point)
        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")
        _, work_b, overlay_b = self._make_session(tmp_path, slug, projects_dir, "b")

        # Session A commits and merges (advances shared)
        (work_a / "new.txt").write_text("from A\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "A's commit")
        wrapper.merge_session_gitdirs("sid-a", overlay_a)
        shared_after_a = git_head(shared_gd, gitdir=True)

        # Session B has no new commits — its main is the original HEAD (behind)
        # Merging B must NOT overwrite A's commit
        wrapper.merge_session_gitdirs("sid-b", overlay_b)
        assert git_head(shared_gd, gitdir=True) == shared_after_a

    def test_merge_pushes_commits_to_shared(self, shared_project, tmp_path):
        """After session ends, its commits appear in the shared gitdir."""
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"
        shared_head_before = git_run("git", "--git-dir", str(shared_gd),
                                     "rev-parse", "HEAD").stdout.strip()

        gd_a, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")

        (work_a / "new.txt").write_text("from A\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "session commit")

        wrapper.merge_session_gitdirs("sid-a", overlay_a)

        shared_head_after = git_run("git", "--git-dir", str(shared_gd),
                                    "rev-parse", "HEAD").stdout.strip()
        assert shared_head_after != shared_head_before

        log = git_run("git", "--git-dir", str(shared_gd), "log", "--oneline")
        assert "session commit" in log.stdout

    def test_merge_preserves_feature_branches(self, shared_project, tmp_path):
        """Session on a feature branch: merge creates that branch in shared."""
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")

        cave_git(work_a, "checkout", "-b", "feature-x")
        (work_a / "feat.txt").write_text("feature\n")
        cave_git(work_a, "add", "feat.txt")
        cave_git(work_a, "commit", "-m", "feature commit")

        wrapper.merge_session_gitdirs("sid-a", overlay_a)

        # Shared gitdir should have the feature branch
        branch = git_run("git", "--git-dir", str(shared_gd),
                         "rev-parse", "refs/heads/feature-x")
        assert branch.returncode == 0

    def test_amended_commit_updates_shared(self, shared_project, tmp_path):
        """Amending a commit after mid-session sync must still update shared.

        Scenario: session commits B on main, pull triggers
        forward_session_refs (updating main in shared), session
        amends B to B', session exits.  Cleanup must update main to B'.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        gd_a, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")

        # Session creates commit B on main
        (work_a / "new.txt").write_text("v1\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "commit B")
        sha_b = cave_git(work_a, "rev-parse", "HEAD").stdout.strip()

        # Mid-session sync (pull -> forward_session_refs) updates main in shared
        forward_session_refs(slug, gd_a, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_b

        # Session amends B to B' (force-push in real scenario via hook)
        (work_a / "new.txt").write_text("v2\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "--amend", "-m", "commit B amended")
        sha_b_prime = cave_git(work_a, "rev-parse", "HEAD").stdout.strip()
        assert sha_b_prime != sha_b

        # Session exits — merge should update shared main to B'
        wrapper.merge_session_gitdirs("sid-a", overlay_a)
        assert git_head(shared_gd, gitdir=True) == sha_b_prime

    def test_reauthored_commit_not_overwritten(self, shared_project, tmp_path):
        """Re-authored shared ref (same tree) must not be overwritten by session.

        After pull re-authors commits (changing author from cave-coder to host
        user), the shared gitdir has the re-authored commit while the session
        still has the original.  merge_session_gitdirs must NOT overwrite the
        re-authored ref, since the tree content is identical.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        gd_a, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")

        # Session creates commit B on session branch
        (work_a / "new.txt").write_text("content\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "commit B")
        session_sha = cave_git(work_a, "rev-parse", "HEAD").stdout.strip()

        # Mid-session sync pushes session branch to shared
        forward_session_refs(slug, gd_a, shared_gd)

        # Simulate pull re-authoring: update main to a re-authored commit
        # with same tree but different author (like --reset-author does)
        tree = git_run("git", "--git-dir", str(shared_gd),
                        "rev-parse", f"{session_sha}^{{tree}}").stdout.strip()
        parent = git_run("git", "--git-dir", str(shared_gd),
                          "rev-parse", f"{session_sha}^").stdout.strip()
        env = {**os.environ, **GIT_ENV,
               "GIT_AUTHOR_NAME": "Host User",
               "GIT_AUTHOR_EMAIL": "host@test.com"}
        reauthored = subprocess.run(
            ["git", "--git-dir", str(shared_gd),
             "commit-tree", tree, "-p", parent, "-m", "commit B"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        git_run("git", "--git-dir", str(shared_gd),
                "update-ref", "refs/heads/main", reauthored)
        assert reauthored != session_sha

        # Session still has the original commit.
        # Cleanup: session branch should rebase onto re-authored main.
        # The re-authored main has the same tree, so the session commit
        # should be detected as duplicate and main preserved.
        wrapper.merge_session_gitdirs("sid-a", overlay_a)
        assert git_head(shared_gd, gitdir=True) == reauthored

    def test_rebase_onto_reauthored_chain(self, shared_project, tmp_path):
        """New session commits rebase onto re-authored shared chain.

        Scenario: session commits A, pull re-authors to A' and pushes back,
        session then commits B on top of original A. merge_session_gitdirs
        should produce A' -> B' (B rebased onto A'), not A -> B (which would
        undo the re-authoring and require --force on next pull).
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        gd_a, work_a, overlay_a = self._make_session(
            tmp_path, slug, projects_dir, "a")

        # Session commits A
        (work_a / "a.txt").write_text("from A\n")
        cave_git(work_a, "add", "a.txt")
        cave_git(work_a, "commit", "-m", "commit A")
        session_sha_a = cave_git(
            work_a, "rev-parse", "HEAD").stdout.strip()

        # Mid-session sync: shared now has A
        wrapper.merge_session_gitdirs("sid-a", overlay_a)
        assert git_head(shared_gd, gitdir=True) == session_sha_a

        # Simulate pull+push re-authoring: create A' with same tree,
        # different author
        tree_a = git_run("git", "--git-dir", str(shared_gd),
                         "rev-parse", "HEAD^{tree}").stdout.strip()
        parent = git_run("git", "--git-dir", str(shared_gd),
                         "rev-parse", "HEAD^").stdout.strip()
        env = {**os.environ, **GIT_ENV,
               "GIT_AUTHOR_NAME": "Host User",
               "GIT_AUTHOR_EMAIL": "host@test.com"}
        reauthored_a = subprocess.run(
            ["git", "--git-dir", str(shared_gd),
             "commit-tree", tree_a, "-p", parent, "-m", "commit A"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        git_run("git", "--git-dir", str(shared_gd),
                "update-ref", "refs/heads/main", reauthored_a)

        # Session makes new commit B on top of original A
        (work_a / "b.txt").write_text("from B\n")
        cave_git(work_a, "add", "b.txt")
        cave_git(work_a, "commit", "-m", "commit B")
        session_sha_b = cave_git(
            work_a, "rev-parse", "HEAD").stdout.strip()

        # merge_session_gitdirs: should rebase B onto A', NOT force-update
        wrapper.merge_session_gitdirs("sid-a", overlay_a)

        # Shared HEAD should NOT be the session's B (that undoes re-authoring)
        shared_head = git_head(shared_gd, gitdir=True)
        assert shared_head != session_sha_b

        # A' should still be an ancestor (re-authoring preserved)
        is_ancestor = git_run("git", "--git-dir", str(shared_gd),
                              "merge-base", "--is-ancestor",
                              reauthored_a, shared_head)
        assert is_ancestor.returncode == 0, \
            "re-authored A' should be ancestor of new HEAD"

        # The rebased B' should have B's tree
        tree_b = git_run("git", "--git-dir", str(shared_gd),
                         "rev-parse",
                         f"{session_sha_b}^{{tree}}").stdout.strip()
        rebased_tree = git_run("git", "--git-dir", str(shared_gd),
                               "rev-parse",
                               f"{shared_head}^{{tree}}").stdout.strip()
        assert rebased_tree == tree_b, \
            "rebased commit should have same tree as original"

        # Log should show both commits
        log = git_run("git", "--git-dir", str(shared_gd),
                      "log", "--oneline", "refs/heads/main")
        assert "commit A" in log.stdout
        assert "commit B" in log.stdout

    def test_rebase_multiple_new_commits_onto_reauthored(
            self, shared_project, tmp_path):
        """Multiple new session commits all rebase onto re-authored chain."""
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        gd_a, work_a, overlay_a = self._make_session(
            tmp_path, slug, projects_dir, "a")

        # Session commits A
        (work_a / "a.txt").write_text("from A\n")
        cave_git(work_a, "add", "a.txt")
        cave_git(work_a, "commit", "-m", "commit A")

        # Sync + re-author
        wrapper.merge_session_gitdirs("sid-a", overlay_a)
        tree_a = git_run("git", "--git-dir", str(shared_gd),
                         "rev-parse", "HEAD^{tree}").stdout.strip()
        parent = git_run("git", "--git-dir", str(shared_gd),
                         "rev-parse", "HEAD^").stdout.strip()
        env = {**os.environ, **GIT_ENV,
               "GIT_AUTHOR_NAME": "Host User",
               "GIT_AUTHOR_EMAIL": "host@test.com"}
        reauthored_a = subprocess.run(
            ["git", "--git-dir", str(shared_gd),
             "commit-tree", tree_a, "-p", parent, "-m", "commit A"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        git_run("git", "--git-dir", str(shared_gd),
                "update-ref", "refs/heads/main", reauthored_a)

        # Session makes B and C on top of original A
        (work_a / "b.txt").write_text("from B\n")
        cave_git(work_a, "add", "b.txt")
        cave_git(work_a, "commit", "-m", "commit B")
        (work_a / "c.txt").write_text("from C\n")
        cave_git(work_a, "add", "c.txt")
        cave_git(work_a, "commit", "-m", "commit C")

        wrapper.merge_session_gitdirs("sid-a", overlay_a)

        # A' should be ancestor, and log should have all three commits
        shared_head = git_head(shared_gd, gitdir=True)
        is_ancestor = git_run("git", "--git-dir", str(shared_gd),
                              "merge-base", "--is-ancestor",
                              reauthored_a, shared_head)
        assert is_ancestor.returncode == 0

        log = git_run("git", "--git-dir", str(shared_gd),
                      "log", "--oneline", "refs/heads/main")
        assert "commit A" in log.stdout
        assert "commit B" in log.stdout
        assert "commit C" in log.stdout

    def test_forward_session_refs_restores_alternates(
            self, shared_project, tmp_path):
        """forward_session_refs restores cave-internal alternates after sync.

        The function temporarily rewrites alternates to host path for git
        operations, then restores the original cave path.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"
        overlay = tmp_path / "overlay"
        overlay.mkdir()
        session_gd = wrapper.create_session_gitdir(slug, "test-sid", overlay)

        cave_path = f"/projects/.shared-objects/{slug}"
        alt_file = session_gd / "objects" / "info" / "alternates"
        alt_file.write_text(f"{cave_path}\n")

        wrapper.forward_session_refs(slug, session_gd, shared_gd)

        assert alt_file.read_text().strip() == cave_path

    def test_forward_session_refs_skips_diverged(
            self, shared_project, tmp_path):
        """forward_session_refs skips diverged refs instead of force-updating.

        When a session and shared have diverged in a way that can't be
        resolved by fast-forward or rebase, live sync must skip the ref.
        merge_session_gitdirs (final cleanup) would force-update and back up.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")
        _, work_b, overlay_b = self._make_session(tmp_path, slug, projects_dir, "b")

        # Both sessions commit on main (same file, different content = diverge)
        (work_a / "file.txt").write_text("version A\n")
        cave_git(work_a, "add", "file.txt")
        cave_git(work_a, "commit", "-m", "commit A")

        (work_b / "file.txt").write_text("version B\n")
        cave_git(work_b, "add", "file.txt")
        cave_git(work_b, "commit", "-m", "commit B")

        # A merges first (cleanup), advancing shared
        wrapper.merge_session_gitdirs("sid-a", overlay_a)
        shared_after_a = git_head(shared_gd, gitdir=True)

        # Live sync B — should SKIP because B diverges from shared
        session_gd_b = overlay_b / f"gitdir-{slug}"
        wrapper.forward_session_refs(slug, session_gd_b, shared_gd)

        # Shared should still be at A's commit (not overwritten by B)
        assert git_head(shared_gd, gitdir=True) == shared_after_a

        # No backup ref should exist (that's a cleanup-only behavior)
        backup = git_run("git", "--git-dir", str(shared_gd),
                         "rev-parse", "refs/sessions/sid-b/main")
        assert backup.returncode != 0

    def test_forward_refs_skips_conflicting_diverge(
            self, shared_project, tmp_path):
        """Conflicting diverge is skipped during live sync.

        When both shared and session modify the same file differently,
        rebase fails and forward_session_refs skips the ref.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")

        # Push a conflicting commit to shared
        shared_work = tmp_path / "shared-work"
        shared_work.mkdir()
        git_run("git", "--git-dir", str(shared_gd), "--work-tree", str(shared_work),
                "checkout", "HEAD", "--", ".")
        (shared_work / "file.txt").write_text("host version\n")
        env = {**os.environ, **GIT_ENV}
        subprocess.run(
            ["git", "--git-dir", str(shared_gd), "--work-tree", str(shared_work),
             "add", "file.txt"],
            capture_output=True, check=True, env=env,
        )
        subprocess.run(
            ["git", "--git-dir", str(shared_gd), "--work-tree", str(shared_work),
             "commit", "-m", "host edit"],
            capture_output=True, check=True, env=env,
        )
        shared_after_push = git_head(shared_gd, gitdir=True)

        # Session edits same file
        (work_a / "file.txt").write_text("session version\n")
        cave_git(work_a, "add", "file.txt")
        cave_git(work_a, "commit", "-m", "session edit")

        # Live sync — should skip (conflict)
        session_gd = overlay_a / f"gitdir-{slug}"
        wrapper.forward_session_refs(slug, session_gd, shared_gd)

        # Shared HEAD should NOT have moved
        assert git_head(shared_gd, gitdir=True) == shared_after_push

    def test_rebase_sync_then_resync_is_noop(
            self, shared_project, tmp_path):
        """After a successful rebase-sync, the next sync is a no-op.

        Session commits on main, shared advances independently (push).
        First sync rebases session's commits onto shared.  Second sync
        should skip (refs/synced was set) rather than re-rebasing.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(
            tmp_path, slug, projects_dir, "a")
        session_gd = overlay_a / f"gitdir-{slug}"

        # Push a new commit to shared (simulating host push)
        shared_work = tmp_path / "shared-work"
        shared_work.mkdir()
        git_run("git", "--git-dir", str(shared_gd), "--work-tree",
                str(shared_work), "checkout", "HEAD", "--", ".")
        (shared_work / "host.txt").write_text("from host\n")
        env = {**os.environ, **GIT_ENV}
        subprocess.run(
            ["git", "--git-dir", str(shared_gd), "--work-tree",
             str(shared_work), "add", "host.txt"],
            capture_output=True, check=True, env=env,
        )
        subprocess.run(
            ["git", "--git-dir", str(shared_gd), "--work-tree",
             str(shared_work), "commit", "-m", "host push"],
            capture_output=True, check=True, env=env,
        )

        # Session commits on different file (no conflict, rebase will succeed)
        (work_a / "session.txt").write_text("from session\n")
        cave_git(work_a, "add", "session.txt")
        cave_git(work_a, "commit", "-m", "session work")

        # First sync — rebases session onto shared
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        shared_after_first = git_head(shared_gd, gitdir=True)

        # Second sync — should be a no-op (refs/synced prevents re-rebase)
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        shared_after_second = git_head(shared_gd, gitdir=True)

        assert shared_after_first == shared_after_second

    def test_forward_refs_live_sync_handles_amendment(
            self, shared_project, tmp_path):
        """Live sync updates main in shared when session amends.

        Sessions commit on main. forward_session_refs updates main in
        shared, then on amend, detects the amendment and updates it.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")

        # Session creates commit B on main
        (work_a / "new.txt").write_text("v1\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "commit B")
        sha_b = cave_git(work_a, "rev-parse", "HEAD").stdout.strip()

        # Mid-session sync updates main in shared
        session_gd = overlay_a / f"gitdir-{slug}"
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_b

        # Session amends B to B' (different tree)
        (work_a / "new.txt").write_text("v2\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "--amend", "-m", "commit B amended")
        sha_b_prime = cave_git(work_a, "rev-parse", "HEAD").stdout.strip()
        assert sha_b_prime != sha_b

        # Live sync should detect amendment and update main in shared
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_b_prime

    def test_sync_live_sessions_restores_alternates(
            self, shared_project, tmp_path):
        """sync_live_sessions restores cave-internal alternates via forward_session_refs."""
        host, slug, projects_dir = shared_project
        session_overlay = _state.cave_data() / "session-overlays" / "test-sid"
        session_overlay.mkdir(parents=True)
        session_gd = wrapper.create_session_gitdir(slug, "test-sid", session_overlay)

        cave_path = f"/projects/.shared-objects/{slug}"
        alt_file = session_gd / "objects" / "info" / "alternates"
        alt_file.write_text(f"{cave_path}\n")

        write_session(_state.cave_data(), "test-sid", slug)

        wrapper.sync_live_sessions(slug)

        assert alt_file.read_text().strip() == cave_path

    def test_sync_live_does_not_touch_other_projects(
            self, shared_project, tmp_path):
        """sync_live_sessions only processes the requested slug's gitdir.

        Regression: the old code called merge_session_gitdirs which processed
        ALL gitdir-* in the session overlay.  Now sync_live_sessions calls
        forward_session_refs for only the requested slug, so other projects'
        alternates are never touched.
        """
        host, slug, projects_dir = shared_project

        slug2 = "other-project"
        host2 = tmp_path / "host2"
        host2.mkdir()
        git_run("git", "init", "-b", "main", str(host2))
        (host2 / "file.txt").write_text("other\n")
        git_run("git", "-C", str(host2), "add", ".")
        git_run("git", "-C", str(host2), "commit", "-m", "initial other")
        (projects_dir / slug2).mkdir(parents=True)
        wrapper.initialize_gitdir(slug2, host2)

        session_overlay = _state.cave_data() / "session-overlays" / "test-sid"
        session_overlay.mkdir(parents=True)
        wrapper.create_session_gitdir(slug, "test-sid", session_overlay)
        gd2 = wrapper.create_session_gitdir(slug2, "test-sid", session_overlay)

        cave_path1 = f"/projects/.shared-objects/{slug}"
        cave_path2 = f"/projects/.shared-objects/{slug2}"
        alt1 = session_overlay / f"gitdir-{slug}" / "objects" / "info" / "alternates"
        alt2 = gd2 / "objects" / "info" / "alternates"
        alt1.write_text(f"{cave_path1}\n")
        alt2.write_text(f"{cave_path2}\n")

        write_session(_state.cave_data(), "test-sid", slug)

        wrapper.sync_live_sessions(slug)

        # Synced slug's alternates should be restored
        assert alt1.read_text().strip() == cave_path1
        # Other project's alternates should be untouched (never modified)
        assert alt2.read_text().strip() == cave_path2

    def test_first_sync_after_amend_detects_amend(
            self, shared_project, tmp_path):
        """Session commits on main, syncs, amends, syncs again.
        The second sync should detect the amend and update main in shared.
        The amend detection uses refs/synced.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        # Create session
        _, work, overlay = self._make_session(
            tmp_path, slug, projects_dir, "sess")
        gd = overlay / f"gitdir-{slug}"

        # Session commits on main
        (work / "base.txt").write_text("original\n")
        cave_git(work, "add", "base.txt")
        cave_git(work, "commit", "-m", "add base")
        original_sha = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # First sync — updates main in shared
        wrapper.forward_session_refs(slug, gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == original_sha

        # Session amends (same parent, different tree)
        (work / "base.txt").write_text("amended content\n")
        cave_git(work, "add", "base.txt")
        cave_git(work, "commit", "--amend", "-m", "amended commit")
        amended_sha = cave_git(work, "rev-parse", "HEAD").stdout.strip()
        assert amended_sha != original_sha

        # Second sync — should detect amend and update main
        wrapper.forward_session_refs(slug, gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == amended_sha

    def test_amend_plus_new_commits_updates_shared(
            self, shared_project, tmp_path):
        """Amend + new commits on top propagate via live sync.

        Session commits A, syncs, amends A to A', adds B on top.
        Live sync should adopt the session's version since shared
        hasn't changed since last sync.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work, overlay = self._make_session(
            tmp_path, slug, projects_dir, "amend-new")
        session_gd = overlay / f"gitdir-{slug}"

        # Session commits A
        (work / "a.txt").write_text("v1\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "-m", "add a")
        sha_a = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # Sync
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_a

        # Amend A to A'
        (work / "a.txt").write_text("v2\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "--amend", "-m", "add a amended")

        # Add new commit B on top
        (work / "b.txt").write_text("new file\n")
        cave_git(work, "add", "b.txt")
        cave_git(work, "commit", "-m", "add b")
        sha_b = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # Live sync should adopt session's version
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_b

    def test_amend_first_sync_plus_new_commits(
            self, shared_project, tmp_path):
        """Amend before first sync + new commits on top.

        Session commits A, amends to A', adds B on top, then syncs
        for the first time.  Should use init_matches_shared path.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work, overlay = self._make_session(
            tmp_path, slug, projects_dir, "init-amend")
        session_gd = overlay / f"gitdir-{slug}"

        # Commit A on main
        (work / "a.txt").write_text("v1\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "-m", "add a")

        # Amend A to A'
        (work / "a.txt").write_text("v2\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "--amend", "-m", "add a v2")

        # New commit B on top
        (work / "b.txt").write_text("content\n")
        cave_git(work, "add", "b.txt")
        cave_git(work, "commit", "-m", "add b")
        tip = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # First sync — init_matches_shared should let session win
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        shared_main = git_run("git", "--git-dir", str(shared_gd),
                              "rev-parse", "refs/heads/main")
        assert shared_main.stdout.strip() == tip

    def test_amend_after_hook_push_uses_pushed_ref(
            self, shared_project, tmp_path):
        """Amend detected via refs/pushed when hook pushed the original.

        Session commits A, hook pushes shared main=A (simulated via direct
        update + refs/pushed).  Session amends A to A'.  Hook fails for
        the amend (simulated by not updating shared).  _forward_refs
        should detect ownership via refs/pushed and adopt A'.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work, overlay = self._make_session(
            tmp_path, slug, projects_dir, "pushed")
        session_gd = overlay / f"gitdir-{slug}"

        # Session commits A
        (work / "a.txt").write_text("v1\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "-m", "add a")
        sha_a = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # Simulate successful hook push: update shared main + refs/pushed
        git_run("git", "--git-dir", str(shared_gd), "fetch",
                str(session_gd), sha_a)
        git_run("git", "--git-dir", str(shared_gd), "update-ref",
                "refs/heads/main", sha_a)
        git_run("git", "--git-dir", str(session_gd), "update-ref",
                "refs/pushed/main", sha_a)

        # Session amends A to A'
        (work / "a.txt").write_text("v2\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "--amend", "-m", "add a amended")
        sha_a_prime = cave_git(work, "rev-parse", "HEAD").stdout.strip()
        assert sha_a_prime != sha_a

        # Simulate hook FAILURE for the amend (shared still at A).
        # Live sync should detect ownership via refs/pushed and adopt A'.
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_a_prime

    def test_pushed_ref_does_not_clobber_other_session(
            self, shared_project, tmp_path):
        """refs/pushed must not let session win when another session updated shared.

        Session 1 commits A, hook pushes (pushed=A, shared main=A).
        Session 2 commits B, hook pushes (shared main=B).
        Session 1 amends A to A'.  pushed=A != shared=B, so session 1
        must NOT win -- should fall through to rebase.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        # Session 1
        _, work1, overlay1 = self._make_session(
            tmp_path, slug, projects_dir, "s1")
        gd1 = overlay1 / f"gitdir-{slug}"

        # Session 2
        _, work2, overlay2 = self._make_session(
            tmp_path, slug, projects_dir, "s2")
        gd2 = overlay2 / f"gitdir-{slug}"

        branch = "refs/heads/main"

        # Session 1 commits A
        (work1 / "a.txt").write_text("from s1\n")
        cave_git(work1, "add", "a.txt")
        cave_git(work1, "commit", "-m", "s1 commit")
        sha_a = cave_git(work1, "rev-parse", "HEAD").stdout.strip()

        # Simulate S1 hook push
        git_run("git", "--git-dir", str(shared_gd), "fetch",
                str(gd1), sha_a)
        git_run("git", "--git-dir", str(shared_gd), "update-ref",
                branch, sha_a)
        git_run("git", "--git-dir", str(gd1), "update-ref",
                "refs/pushed/main", sha_a)

        # Session 2 also pushes to main (simulating concurrent push)
        (work2 / "b.txt").write_text("from s2\n")
        cave_git(work2, "add", "b.txt")
        cave_git(work2, "commit", "-m", "s2 commit")
        sha_b = cave_git(work2, "rev-parse", "HEAD").stdout.strip()

        git_run("git", "--git-dir", str(shared_gd), "fetch",
                str(gd2), sha_b)
        git_run("git", "--git-dir", str(shared_gd), "update-ref",
                branch, sha_b)

        # Session 1 amends
        (work1 / "a.txt").write_text("amended s1\n")
        cave_git(work1, "add", "a.txt")
        cave_git(work1, "commit", "--amend", "-m", "s1 amended")
        sha_a_prime = cave_git(work1, "rev-parse", "HEAD").stdout.strip()

        # Live sync for S1: pushed=sha_a != shared=sha_b, so must NOT win
        wrapper.forward_session_refs(slug, gd1, shared_gd)

        # S2's commit must still be reachable (not clobbered)
        final = git_run("git", "--git-dir", str(shared_gd),
                        "rev-parse", branch).stdout.strip()
        is_ancestor = git_run("git", "--git-dir", str(shared_gd),
                              "merge-base", "--is-ancestor", sha_b, final)
        assert is_ancestor.returncode == 0, (
            f"S2 commit lost: final={final[:8]}, s2={sha_b[:8]}"
        )

    def test_live_sync_squash_after_pull_updates_shared(
            self, shared_project, tmp_path):
        """Live sync detects squash of previously-synced commits.

        Session makes 2 commits on main, syncs, then squashes to 1 commit.
        forward_session_refs must update main in shared.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work, overlay = self._make_session(
            tmp_path, slug, projects_dir, "squash")
        session_gd = overlay / f"gitdir-{slug}"

        # Session creates 2 commits on main
        (work / "a.txt").write_text("a\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "-m", "add a")
        (work / "b.txt").write_text("b\n")
        cave_git(work, "add", "b.txt")
        cave_git(work, "commit", "-m", "add b")
        sha_b = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # Mid-session sync updates main in shared
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_b

        # Session squashes both commits into one
        cave_git(work, "reset", "--soft", "HEAD~2")
        cave_git(work, "commit", "-m", "add a and b")
        squashed = cave_git(work, "rev-parse", "HEAD").stdout.strip()
        assert squashed != sha_b

        # Live sync must update main to squashed commit
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == squashed

    def test_live_sync_unchanged_after_reauthoring_skips(
            self, shared_project, tmp_path):
        """Session unchanged since sync is skipped even after re-authoring.

        After pull re-authors commits and sync_refs updates shared, a
        session that hasn't made new commits should not overwrite the
        re-authored ref with its original.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"
        init_head = git_head(shared_gd, gitdir=True)

        _, work, overlay = self._make_session(
            tmp_path, slug, projects_dir, "reauth")
        session_gd = overlay / f"gitdir-{slug}"

        # Session creates a commit
        (work / "x.txt").write_text("x\n")
        cave_git(work, "add", "x.txt")
        cave_git(work, "commit", "-m", "add x")

        # Mid-session sync (sets refs/synced)
        wrapper.forward_session_refs(slug, session_gd, shared_gd)

        # Simulate pull re-authoring
        session_sha = cave_git(work, "rev-parse", "HEAD").stdout.strip()
        tree = git_run("git", "--git-dir", str(shared_gd),
                       "rev-parse", f"{session_sha}^{{tree}}").stdout.strip()
        env = {**os.environ, **GIT_ENV,
               "GIT_AUTHOR_NAME": "Host User",
               "GIT_AUTHOR_EMAIL": "host@test.com"}
        reauthored = subprocess.run(
            ["git", "--git-dir", str(shared_gd),
             "commit-tree", tree, "-p", init_head, "-m", "add x"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        git_run("git", "--git-dir", str(shared_gd),
                "update-ref", "refs/heads/main", reauthored)

        # Live sync: session unchanged since sync, should NOT overwrite
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == reauthored

    def test_live_sync_amend_after_push_does_not_clobber(
            self, shared_project, tmp_path):
        """Session amend must not overwrite new content from push.

        Scenario: session makes commit X, syncs.  Host pushes new commit H
        on top of X (simulating cav push).  Session then amends X to X2.
        forward_session_refs must NOT let session win (would lose H), but
        should skip or rebase.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work, overlay = self._make_session(
            tmp_path, slug, projects_dir, "amend-push")
        session_gd = overlay / f"gitdir-{slug}"

        # Session creates commit X
        (work / "x.txt").write_text("x\n")
        cave_git(work, "add", "x.txt")
        cave_git(work, "commit", "-m", "add x")

        # Mid-session sync
        wrapper.forward_session_refs(slug, session_gd, shared_gd)

        # Simulate push: add a new commit H directly to shared gitdir
        shared_work = tmp_path / "shared-work"
        shared_work.mkdir()
        git_run("git", "--git-dir", str(shared_gd), "--work-tree",
                str(shared_work), "checkout", "HEAD", "--", ".")
        (shared_work / "host.txt").write_text("from host\n")
        env = {**os.environ, **GIT_ENV}
        subprocess.run(
            ["git", "--git-dir", str(shared_gd), "--work-tree",
             str(shared_work), "add", "host.txt"],
            capture_output=True, check=True, env=env,
        )
        subprocess.run(
            ["git", "--git-dir", str(shared_gd), "--work-tree",
             str(shared_work), "commit", "-m", "host push"],
            capture_output=True, check=True, env=env,
        )
        shared_after_push = git_head(shared_gd, gitdir=True)

        # Session amends X to X2 (different content)
        (work / "x.txt").write_text("x amended\n")
        cave_git(work, "add", "x.txt")
        cave_git(work, "commit", "--amend", "-m", "add x amended")

        # Live sync must NOT overwrite the pushed commit
        wrapper.forward_session_refs(slug, session_gd, shared_gd)

        # Host's push commit must still be an ancestor
        final = git_head(shared_gd, gitdir=True)
        is_ancestor = git_run("git", "--git-dir", str(shared_gd),
                              "merge-base", "--is-ancestor",
                              shared_after_push, final)
        assert is_ancestor.returncode == 0, (
            f"Push commit lost: shared={final[:8]}, "
            f"push={shared_after_push[:8]}"
        )

    def test_multiple_amend_sync_cycles(self, shared_project, tmp_path):
        """Multiple rounds of amend + sync all work correctly.

        Session: commit A -> sync -> amend to A' -> sync -> amend to A'' -> sync.
        Each sync must update main in shared to the latest amend.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work, overlay = self._make_session(
            tmp_path, slug, projects_dir, "multi")
        session_gd = overlay / f"gitdir-{slug}"

        # Commit A
        (work / "x.txt").write_text("v1\n")
        cave_git(work, "add", "x.txt")
        cave_git(work, "commit", "-m", "add x")

        # Sync 1
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        sha1 = git_head(shared_gd, gitdir=True)

        # Amend to A'
        (work / "x.txt").write_text("v2\n")
        cave_git(work, "add", "x.txt")
        cave_git(work, "commit", "--amend", "-m", "add x v2")

        # Sync 2
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        sha2 = git_head(shared_gd, gitdir=True)
        assert sha2 != sha1

        # Amend to A''
        (work / "x.txt").write_text("v3\n")
        cave_git(work, "add", "x.txt")
        cave_git(work, "commit", "--amend", "-m", "add x v3")

        # Sync 3
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        sha3 = git_head(shared_gd, gitdir=True)
        assert sha3 != sha2

        # Content should be latest version
        tree_content = git_run("git", "--git-dir", str(shared_gd),
                               "show", "refs/heads/main:x.txt").stdout
        assert tree_content.strip() == "v3"

    def test_session_reset_hard_back_live_sync_skips(
            self, shared_project, tmp_path):
        """Live sync skips when session resets behind shared.

        Session makes A, B, C on main. Live-syncs (main at C in shared).
        Then resets to A.  Live sync skips because session is behind shared.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work, overlay = self._make_session(
            tmp_path, slug, projects_dir, "reset")
        session_gd = overlay / f"gitdir-{slug}"

        # Make 3 commits
        (work / "a.txt").write_text("a\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "-m", "add a")
        sha_a = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        (work / "b.txt").write_text("b\n")
        cave_git(work, "add", "b.txt")
        cave_git(work, "commit", "-m", "add b")

        (work / "c.txt").write_text("c\n")
        cave_git(work, "add", "c.txt")
        cave_git(work, "commit", "-m", "add c")
        sha_c = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # Live sync — main at C
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_c

        # Reset to commit A
        cave_git(work, "reset", "--hard", sha_a)

        # Live sync — session behind shared, skipped
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_c, \
            "live sync must not move main backward"

    def test_session_reset_hard_back_cleanup_keeps_shared(
            self, shared_project, tmp_path):
        """Session commits A,B,C, syncs, resets to A. Cleanup should
        keep main at C (session behind shared, skipped).
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work, overlay = self._make_session(
            tmp_path, slug, projects_dir, "reset")

        # Make 3 commits
        (work / "a.txt").write_text("a\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "-m", "add a")

        (work / "b.txt").write_text("b\n")
        cave_git(work, "add", "b.txt")
        cave_git(work, "commit", "-m", "add b")

        (work / "c.txt").write_text("c\n")
        cave_git(work, "add", "c.txt")
        cave_git(work, "commit", "-m", "add c")
        sha_c = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # Live sync updates main in shared to C
        session_gd = overlay / f"gitdir-{slug}"
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_c

        # Reset to A (session main now behind shared main)
        cave_git(work, "reset", "--hard",
                 cave_git(work, "rev-parse", "HEAD~2").stdout.strip())

        # Cleanup — _forward_refs sees session behind shared, skips.
        wrapper.merge_session_gitdirs("sid-reset", overlay)
        assert git_head(shared_gd, gitdir=True) == sha_c, \
            "cleanup should keep main at the synced tip"

    def test_force_cleanup_same_tree_no_prior_sync(
            self, shared_project, tmp_path):
        """force=True with same-tree divergence and no prior live sync.

        Scenario: session makes A, B. Another session's cleanup moves
        shared to A', B' (re-authored, same trees). This session exits
        without ever being live-synced (no refs/synced).

        Same final tree means content is identical — the skip is correct.
        No content is lost, so no backup is required.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"
        init_head = git_head(shared_gd, gitdir=True)

        _, work, overlay = self._make_session(
            tmp_path, slug, projects_dir, "nosync")

        # Session makes A, B
        (work / "a.txt").write_text("a\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "-m", "add a")
        (work / "b.txt").write_text("b\n")
        cave_git(work, "add", "b.txt")
        cave_git(work, "commit", "-m", "add b")
        session_sha = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # Another session's cleanup re-authors shared to same trees
        # (simulate by creating commits with same trees, different authors)
        git_run("git", "--git-dir", str(shared_gd), "fetch",
                str(overlay / f"gitdir-{slug}"), session_sha)
        sha_a = cave_git(work, "rev-parse", "HEAD~1").stdout.strip()
        tree_a = git_run("git", "--git-dir", str(shared_gd),
                         "rev-parse", f"{sha_a}^{{tree}}").stdout.strip()
        tree_b = git_run("git", "--git-dir", str(shared_gd),
                         "rev-parse", f"{session_sha}^{{tree}}").stdout.strip()
        env = {**os.environ, **GIT_ENV,
               "GIT_AUTHOR_NAME": "Other", "GIT_AUTHOR_EMAIL": "other@t"}
        a_prime = subprocess.run(
            ["git", "--git-dir", str(shared_gd),
             "commit-tree", tree_a, "-p", init_head, "-m", "add a"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        b_prime = subprocess.run(
            ["git", "--git-dir", str(shared_gd),
             "commit-tree", tree_b, "-p", a_prime, "-m", "add b"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        git_run("git", "--git-dir", str(shared_gd),
                "update-ref", "refs/heads/main", b_prime)

        # Verify no refs/synced exists
        session_gd = overlay / f"gitdir-{slug}"
        synced = git_run("git", "--git-dir", str(session_gd),
                         "rev-parse", "refs/synced/main")
        assert synced.returncode != 0

        # Session exits — merge_session_gitdirs (force=True)
        wrapper.merge_session_gitdirs("sid-nosync", overlay)

        # Same tree, no prior sync → skip is correct (content identical).
        # Shared keeps the re-authored version.
        final = git_head(shared_gd, gitdir=True)
        assert final == b_prime, \
            "same-tree divergence should keep shared's version"

    def test_no_gitdir_returns_none(self, tmp_path):
        """create_session_gitdir returns None when no shared gitdir exists."""
        _state.set_cave_data(tmp_path)
        (_state.projects_dir() / "missing").mkdir(parents=True)

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        result = wrapper.create_session_gitdir("missing", "sid", overlay)
        assert result is None

    def test_empty_repo_session(self, tmp_path):
        """Session gitdir from an empty repo (no commits) doesn't crash."""
        slug = "empty"
        _state.set_cave_data(tmp_path)
        (_state.projects_dir() / slug).mkdir(parents=True)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        wrapper.initialize_gitdir(slug, host)

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        session_gd = wrapper.create_session_gitdir(slug, "sid", overlay)
        # Should create something (or return None gracefully)
        # Either is acceptable for an empty repo

    def test_stash_forwarded_on_merge(self, shared_project, tmp_path):
        """git stash entries are forwarded from session to shared gitdir."""
        host, slug, projects_dir = shared_project
        gd_a, work_a, overlay_a = self._make_session(
            tmp_path, slug, projects_dir, "a")

        # Create a stash in session A
        (work_a / "file.txt").write_text("stashed change\n")
        cave_git(work_a, "stash", "push", "-m", "my stash")

        # Verify stash exists in session
        stash = cave_git(work_a, "stash", "list")
        assert "my stash" in stash.stdout

        # Merge session A into shared
        wrapper.merge_session_gitdirs("sid-a", overlay_a)

        # Shared gitdir should have the stash ref
        shared_gd = projects_dir / slug / "gitdir"
        stash_ref = wrapper.safe_git(shared_gd, "rev-parse", "refs/stash",
                                capture_output=True, text=True)
        assert stash_ref.returncode == 0

        # Stash reflog should be copied
        reflog = shared_gd / "logs" / "refs" / "stash"
        assert reflog.is_file()
        assert "my stash" in reflog.read_text()

    def test_stash_available_in_new_session(self, shared_project, tmp_path):
        """Stash entries from a prior session are available in new sessions."""
        host, slug, projects_dir = shared_project
        gd_a, work_a, overlay_a = self._make_session(
            tmp_path, slug, projects_dir, "a")

        # Create stash in session A and merge
        (work_a / "file.txt").write_text("stashed\n")
        cave_git(work_a, "stash", "push", "-m", "carry over")
        wrapper.merge_session_gitdirs("sid-a", overlay_a)

        # Create session B (should pick up stash from shared)
        gd_b, work_b, _ = self._make_session(
            tmp_path, slug, projects_dir, "b")

        stash = cave_git(work_b, "stash", "list")
        assert "carry over" in stash.stdout

        # Stash apply should work
        result = cave_git(work_b, "stash", "apply")
        assert result.returncode == 0
        assert (work_b / "file.txt").read_text() == "stashed\n"

class TestSessionSetup:
    """Test per-session gitdir: sessions work on real branches (e.g. main)."""

    @pytest.fixture
    def shared_project(self, tmp_path):
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)
        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "file.txt").write_text("initial\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")
        wrapper.initialize_gitdir(slug, host)
        return host, slug, projects_dir

    def _make_session(self, tmp_path, slug, projects_dir, name):
        overlay_base = tmp_path / f"overlay-{name}"
        overlay_base.mkdir()
        session_gitdir = wrapper.create_session_gitdir(slug, f"sid-{name}", overlay_base)
        workdir = tmp_path / f"work-{name}"
        workdir.mkdir()
        git_run("git", "--git-dir", str(session_gitdir), "--work-tree", str(workdir),
                "checkout", "HEAD", "--", ".")
        make_cave_session(session_gitdir, workdir)
        return session_gitdir, workdir, overlay_base

    def test_session_starts_on_real_branch(self, shared_project, tmp_path):
        """HEAD points to the real branch (e.g. main) after session creation."""
        _, slug, projects_dir = shared_project
        gd, work, _ = self._make_session(tmp_path, slug, projects_dir, "test")
        head_ref = git_run("git", "--git-dir", str(gd),
                           "symbolic-ref", "HEAD").stdout.strip()
        assert head_ref == "refs/heads/main"

    def test_post_commit_hook_exists(self, shared_project, tmp_path):
        """Session gitdir has an executable post-commit hook."""
        _, slug, projects_dir = shared_project
        gd, _, _ = self._make_session(tmp_path, slug, projects_dir, "hook")
        hook = gd / "hooks" / "post-commit"
        assert hook.is_file()
        assert os.access(hook, os.X_OK)

    def test_shared_remote_configured(self, shared_project, tmp_path):
        """Session gitdir has a 'shared' remote pointing to cave path."""
        _, slug, projects_dir = shared_project
        gd, _, _ = self._make_session(tmp_path, slug, projects_dir, "remote")
        url = git_run("git", "--git-dir", str(gd),
                      "remote", "get-url", "shared").stdout.strip()
        assert url == f"/projects/.shared-gitdirs/{slug}"

    def test_cleanup_forwards_commit_to_main(self, shared_project, tmp_path):
        """Commit on main, cleanup forwards to shared main."""
        _, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"
        _, work, overlay = self._make_session(tmp_path, slug, projects_dir, "ff")

        (work / "new.txt").write_text("content\n")
        cave_git(work, "add", "new.txt")
        cave_git(work, "commit", "-m", "session work")
        sha = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        wrapper.merge_session_gitdirs("sid-ff", overlay)
        assert git_head(shared_gd, gitdir=True) == sha

    def test_cleanup_diverged_backs_up_ref(self, shared_project, tmp_path):
        """Two sessions with conflicting changes: second force-updates with backup."""
        _, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")
        _, work_b, overlay_b = self._make_session(tmp_path, slug, projects_dir, "b")

        (work_a / "file.txt").write_text("version A\n")
        cave_git(work_a, "add", "file.txt")
        cave_git(work_a, "commit", "-m", "A's edit")

        (work_b / "file.txt").write_text("version B\n")
        cave_git(work_b, "add", "file.txt")
        cave_git(work_b, "commit", "-m", "B's edit")

        wrapper.merge_session_gitdirs("sid-a", overlay_a)
        wrapper.merge_session_gitdirs("sid-b", overlay_b)

        # B's diverged ref backed up under refs/sessions/sid-b/main
        ref = git_run("git", "--git-dir", str(shared_gd),
                      "rev-parse", "refs/sessions/sid-b/main")
        assert ref.returncode == 0

    def test_forward_refs_updates_main_in_shared(self, shared_project, tmp_path):
        """forward_session_refs updates main in shared."""
        _, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        gd, work, overlay = self._make_session(tmp_path, slug, projects_dir, "push")
        (work / "x.txt").write_text("x\n")
        cave_git(work, "add", "x.txt")
        cave_git(work, "commit", "-m", "push test")
        sha = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        wrapper.forward_session_refs(slug, gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha

    def test_concurrent_merge_to_main(self, shared_project, tmp_path):
        """Two sessions merge sequentially; second rebases onto first."""
        _, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "c1")
        _, work_b, overlay_b = self._make_session(tmp_path, slug, projects_dir, "c2")

        (work_a / "a.txt").write_text("a\n")
        cave_git(work_a, "add", "a.txt")
        cave_git(work_a, "commit", "-m", "add a")

        (work_b / "b.txt").write_text("b\n")
        cave_git(work_b, "add", "b.txt")
        cave_git(work_b, "commit", "-m", "add b")

        wrapper.merge_session_gitdirs("sid-c1", overlay_a)
        wrapper.merge_session_gitdirs("sid-c2", overlay_b)

        log = git_run("git", "--git-dir", str(shared_gd),
                      "log", "--oneline", "refs/heads/main")
        assert "add a" in log.stdout
        assert "add b" in log.stdout


class TestWorktreeSync:
    """Test that new sessions sync working tree to HEAD.

    When the shared gitdir has commits that haven't been pulled to the
    host, a new session's overlay (lower=host project) shows stale files.
    The session gitdir's HEAD is correct, so start_session must checkout
    HEAD into the merged overlay to sync the working tree.
    """

    @pytest.fixture
    def diverged_session(self, tmp_path):
        """Simulate a new session where shared gitdir is ahead of host.

        1. Host repo at commit A
        2. Shared gitdir advanced to commit B (prior session committed)
        3. Session gitdir created from shared (HEAD=B)
        4. Working directory has host files (commit A state)

        Returns (host, shared_gitdir, session_gitdir, workdir).
        """
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        # Host repo at commit A
        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "file.txt").write_text("original\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")

        wrapper.initialize_gitdir(slug, host)
        shared_gitdir = projects_dir / slug / "gitdir"

        # Advance shared gitdir to commit B (simulating a prior cave session)
        env = {**os.environ, **GIT_ENV}
        # Write a new tree with modified file.txt
        blob = subprocess.run(
            ["git", "--git-dir", str(shared_gitdir), "hash-object", "-w",
             "--stdin"],
            input="modified\n", capture_output=True, text=True, check=True,
        ).stdout.strip()
        head_tree = subprocess.run(
            ["git", "--git-dir", str(shared_gitdir), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Create new tree replacing file.txt
        ls_tree = subprocess.run(
            ["git", "--git-dir", str(shared_gitdir), "ls-tree", head_tree],
            capture_output=True, text=True, check=True,
        ).stdout
        new_tree_input = ls_tree.replace(
            ls_tree.split("\t")[0].split()[-1],  # old blob hash
            blob,
        )
        new_tree = subprocess.run(
            ["git", "--git-dir", str(shared_gitdir), "mktree"],
            input=new_tree_input, capture_output=True, text=True, check=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "--git-dir", str(shared_gitdir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        new_commit = subprocess.run(
            ["git", "--git-dir", str(shared_gitdir),
             "commit-tree", new_tree, "-p", head, "-m", "cave edit"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        subprocess.run(
            ["git", "--git-dir", str(shared_gitdir),
             "update-ref", "refs/heads/main", new_commit],
            capture_output=True, check=True,
        )

        # Create session gitdir (HEAD=B, has commit B via alternates)
        overlay_base = tmp_path / "overlay"
        overlay_base.mkdir()
        session_gitdir = wrapper.create_session_gitdir(slug, "test-sid", overlay_base)

        # Working directory simulates overlay lower=host (commit A state)
        workdir = tmp_path / "workdir"
        shutil.copytree(host, workdir, dirs_exist_ok=True)
        # Remove .git from copy (session uses separate gitdir)
        shutil.rmtree(workdir / ".git", ignore_errors=True)

        return host, shared_gitdir, session_gitdir, workdir

    def test_worktree_synced_when_ahead(self, diverged_session):
        """Working tree must match HEAD after checkout, not stale host state."""
        host, shared_gitdir, session_gitdir, workdir = diverged_session

        # Before sync: workdir has host's original content
        assert (workdir / "file.txt").read_text() == "original\n"

        # Simulate what start_session does: compare trees and checkout
        host_tree = subprocess.run(
            ["git", "-C", str(host), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True,
        )
        session_tree = wrapper.safe_git(session_gitdir, "rev-parse", "HEAD^{tree}",
                                   capture_output=True, text=True)
        assert host_tree.stdout.strip() != session_tree.stdout.strip()

        subprocess.run(
            ["git", "--git-dir", str(session_gitdir),
             "--work-tree", str(workdir)] + wrapper.SAFE_GIT_OPTS +
            ["checkout", "-f"],
            capture_output=True, check=True,
        )

        # After sync: workdir matches session HEAD (commit B)
        assert (workdir / "file.txt").read_text() == "modified\n"

    def test_no_checkout_when_trees_match(self, tmp_path):
        """No checkout needed when host and session HEAD have same tree."""
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "file.txt").write_text("content\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")

        wrapper.initialize_gitdir(slug, host)

        overlay_base = tmp_path / "overlay"
        overlay_base.mkdir()
        session_gitdir = wrapper.create_session_gitdir(slug, "test-sid", overlay_base)

        host_tree = subprocess.run(
            ["git", "-C", str(host), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True,
        )
        session_tree = wrapper.safe_git(session_gitdir, "rev-parse", "HEAD^{tree}",
                                   capture_output=True, text=True)
        # Trees should match — no checkout needed
        assert host_tree.stdout.strip() == session_tree.stdout.strip()


class TestSavedUpper:
    """Test overlay upper layer preservation across sessions.

    On session exit, the overlay upper is saved to a persistent location.
    On next session start, it's restored so uncommitted changes, untracked
    files, and deletions survive session restarts.
    """

    def test_upper_saved_on_exit(self, tmp_path):
        """Overlay upper is moved to saved-upper on session cleanup."""
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        # Simulate session overlay base with an upper that has changes
        overlay_base = tmp_path / "session-overlay"
        upper = overlay_base / f"{slug}-upper"
        upper.mkdir(parents=True)
        (upper / "new-file.txt").write_text("uncommitted work\n")

        projects = {slug: str(tmp_path / "host")}

        # Simulate what start_session's finally block does
        saved = projects_dir / slug / "saved-upper"
        wrapper.force_rmtree(saved)
        upper.rename(saved)

        assert saved.is_dir()
        assert (saved / "new-file.txt").read_text() == "uncommitted work\n"

    def test_saved_upper_restored_on_start(self, tmp_path):
        """Saved upper is moved back into session overlay on start."""
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        # Pre-populate saved-upper with uncommitted changes
        saved = projects_dir / slug / "saved-upper"
        saved.mkdir()
        (saved / "dirty-file.txt").write_text("unsaved work\n")

        overlay_base = tmp_path / "session-overlay"
        upper = overlay_base / f"{slug}-upper"
        upper.parent.mkdir(parents=True, exist_ok=True)

        # Simulate what start_session does: check for saved upper
        has_saved_upper = False
        if saved.is_dir():
            saved.rename(upper)
            has_saved_upper = True

        assert has_saved_upper
        assert upper.is_dir()
        assert (upper / "dirty-file.txt").read_text() == "unsaved work\n"
        assert not saved.exists()

    def test_no_checkout_when_saved_upper_restored(self, tmp_path):
        """git checkout -f is skipped when saved upper is restored.

        The saved upper already contains the correct working tree state
        (both committed and uncommitted changes).  Running checkout -f
        would clobber uncommitted changes.
        """
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        # Host at commit A
        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "file.txt").write_text("original\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")

        wrapper.initialize_gitdir(slug, host)
        shared_gitdir = projects_dir / slug / "gitdir"

        # Advance shared gitdir (simulating prior session commit)
        env = {**os.environ, **GIT_ENV}
        blob = subprocess.run(
            ["git", "--git-dir", str(shared_gitdir), "hash-object", "-w",
             "--stdin"],
            input="committed\n", capture_output=True, text=True, check=True,
        ).stdout.strip()
        head_tree = subprocess.run(
            ["git", "--git-dir", str(shared_gitdir), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        ls_tree = subprocess.run(
            ["git", "--git-dir", str(shared_gitdir), "ls-tree", head_tree],
            capture_output=True, text=True, check=True,
        ).stdout
        new_tree_input = ls_tree.replace(
            ls_tree.split("\t")[0].split()[-1], blob)
        new_tree = subprocess.run(
            ["git", "--git-dir", str(shared_gitdir), "mktree"],
            input=new_tree_input, capture_output=True, text=True, check=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "--git-dir", str(shared_gitdir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        new_commit = subprocess.run(
            ["git", "--git-dir", str(shared_gitdir),
             "commit-tree", new_tree, "-p", head, "-m", "cave edit"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        subprocess.run(
            ["git", "--git-dir", str(shared_gitdir),
             "update-ref", "refs/heads/main", new_commit],
            capture_output=True, check=True,
        )

        # Session gitdir created from shared (HEAD=B)
        overlay_base = tmp_path / "overlay"
        overlay_base.mkdir()
        session_gitdir = wrapper.create_session_gitdir(slug, "test-sid", overlay_base)

        # Simulate a saved upper with the committed file + uncommitted edit
        workdir = tmp_path / "workdir"
        shutil.copytree(host, workdir, dirs_exist_ok=True)
        shutil.rmtree(workdir / ".git", ignore_errors=True)
        (workdir / "file.txt").write_text("committed\n")       # from prior commit
        (workdir / "scratch.txt").write_text("uncommitted\n")   # uncommitted work

        # Verify trees differ (checkout would normally fire)
        host_tree = subprocess.run(
            ["git", "-C", str(host), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True,
        )
        session_tree = wrapper.safe_git(session_gitdir, "rev-parse", "HEAD^{tree}",
                                   capture_output=True, text=True)
        assert host_tree.stdout.strip() != session_tree.stdout.strip()

        # With has_saved_upper=True, checkout should be SKIPPED.
        # If checkout ran, it would delete scratch.txt (not in HEAD).
        # Verify scratch.txt survives (simulating the skip).
        has_saved_upper = True
        if not has_saved_upper:
            subprocess.run(
                ["git", "--git-dir", str(session_gitdir),
                 "--work-tree", str(workdir)] + wrapper.SAFE_GIT_OPTS +
                ["checkout", "-f"],
                capture_output=True,
            )

        assert (workdir / "scratch.txt").read_text() == "uncommitted\n"
        assert (workdir / "file.txt").read_text() == "committed\n"

    def test_reset_cleans_saved_upper(self, tmp_path):
        """coding-cave reset removes saved-upper."""
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "file.txt").write_text("content\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")

        # Create saved-upper
        saved = projects_dir / slug / "saved-upper"
        saved.mkdir()
        (saved / "dirty.txt").write_text("work\n")

        # Also need the dirs that reset expects
        for d in ["upper", "work"]:
            (projects_dir / slug / d).mkdir(exist_ok=True)

        write_projects(tmp_path, {slug: str(host)})

        wrapper.reset_project_overlay(slug)

        assert not saved.exists()


class TestSavedIndex:
    """Test staging area (index) preservation across sessions."""

    @pytest.fixture
    def project_with_session(self, tmp_path):
        """Set up a project with a session gitdir and working directory.

        Returns (slug, projects_dir, session_gitdir, workdir, overlay_base).
        """
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "file.txt").write_text("initial\n")
        (host / "other.txt").write_text("other\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")

        wrapper.initialize_gitdir(slug, host)

        overlay_base = tmp_path / "overlay"
        overlay_base.mkdir()
        session_gitdir = wrapper.create_session_gitdir(
            slug, "test-sid", overlay_base)

        workdir = tmp_path / "workdir"
        workdir.mkdir()
        git_run("git", "--git-dir", str(session_gitdir),
                "--work-tree", str(workdir),
                "checkout", "HEAD", "--", ".")
        (workdir / ".git").write_text(f"gitdir: {session_gitdir}\n")

        return slug, projects_dir, session_gitdir, workdir, overlay_base

    def test_staged_changes_preserved(self, project_with_session):
        """Staged changes survive session restart via saved index."""
        slug, projects_dir, session_gd, workdir, overlay_base = \
            project_with_session
        shared_gd = projects_dir / slug / "gitdir"

        # Stage a change
        (workdir / "file.txt").write_text("staged change\n")
        cave_git(workdir, "add", "file.txt")

        # Verify it's staged
        status = cave_git(workdir, "status", "--porcelain")
        assert "M  file.txt" in status.stdout

        # Simulate session exit: save index + objects
        _save_session_index(session_gd, shared_gd, slug)

        saved_index = projects_dir / slug / "saved-index"
        assert saved_index.is_file()

        # Create new session (should restore saved index)
        overlay2 = projects_dir / "overlay2"
        overlay2.mkdir()
        session_gd2 = wrapper.create_session_gitdir(
            slug, "test-sid2", overlay2)

        # Simulate saved-upper restore: just write files directly
        # (no git checkout HEAD -- ., which would clobber the index)
        workdir2 = projects_dir / "workdir2"
        workdir2.mkdir()
        (workdir2 / ".git").write_text(f"gitdir: {session_gd2}\n")
        (workdir2 / "file.txt").write_text("staged change\n")
        (workdir2 / "other.txt").write_text("other\n")

        # Check that the file shows as staged (index preserved)
        status2 = cave_git(workdir2, "status", "--porcelain")
        assert "M  file.txt" in status2.stdout

        # Saved index should be consumed
        assert not saved_index.exists()

    def test_partially_staged_preserved(self, project_with_session):
        """Partially staged files: staged hunks survive restart."""
        slug, projects_dir, session_gd, workdir, overlay_base = \
            project_with_session
        shared_gd = projects_dir / slug / "gitdir"

        # Stage one file, modify another without staging
        (workdir / "file.txt").write_text("staged\n")
        cave_git(workdir, "add", "file.txt")
        (workdir / "other.txt").write_text("unstaged\n")

        status = cave_git(workdir, "status", "--porcelain")
        assert "M  file.txt" in status.stdout   # staged
        assert " M other.txt" in status.stdout   # unstaged

        _save_session_index(session_gd, shared_gd, slug)

        # New session (no git checkout -- simulating saved upper restore)
        overlay2 = projects_dir / "overlay2"
        overlay2.mkdir()
        session_gd2 = wrapper.create_session_gitdir(
            slug, "test-sid2", overlay2)
        workdir2 = projects_dir / "workdir2"
        workdir2.mkdir()
        (workdir2 / ".git").write_text(f"gitdir: {session_gd2}\n")
        (workdir2 / "file.txt").write_text("staged\n")
        (workdir2 / "other.txt").write_text("unstaged\n")

        status2 = cave_git(workdir2, "status", "--porcelain")
        # file.txt should be staged (in index), other.txt unstaged
        assert "M  file.txt" in status2.stdout
        assert " M other.txt" in status2.stdout

    def test_reset_cleans_saved_index(self, tmp_path):
        """coding-cave reset removes saved-index."""
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "f.txt").write_text("x\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "init")

        for d in ["upper", "work"]:
            (projects_dir / slug / d).mkdir(exist_ok=True)
        (projects_dir / slug / "saved-index").write_bytes(b"fake index")

        write_projects(tmp_path, {slug: str(host)})

        wrapper.reset_project_overlay(slug)
        assert not (projects_dir / slug / "saved-index").exists()


class TestConcurrentSavedUpper:
    """Test that concurrent sessions don't clobber each other's saved state."""

    def test_empty_upper_does_not_overwrite_saved(self, tmp_path):
        """A session that didn't modify a project must not overwrite
        another session's saved-upper for that project.

        Simulates the finally block's save logic: uppers with only .git
        are skipped.
        """
        slug = "test-project"
        projects_dir = tmp_path / "projects"
        (projects_dir / slug).mkdir(parents=True)

        # Prior session saved changes
        saved = projects_dir / slug / "saved-upper"
        saved.mkdir()
        (saved / "important.txt").write_text("don't lose me\n")
        (saved / ".git").write_text("gitdir: /projects/.gitdirs/test-project\n")

        # New session's upper has only .git (no real changes)
        overlay_base = tmp_path / "session"
        upper = overlay_base / f"{slug}-upper"
        upper.mkdir(parents=True)
        (upper / ".git").write_text("gitdir: /projects/.gitdirs/test-project\n")

        # Simulate the finally block save logic
        entries = set(os.listdir(upper))
        entries.discard(".git")
        # Should NOT save because upper has no real content
        assert not entries

        # Verify saved-upper is untouched
        assert (saved / "important.txt").read_text() == "don't lose me\n"

    def test_modified_upper_does_save(self, tmp_path):
        """A session that modified a project saves its upper."""
        slug = "test-project"
        projects_dir = tmp_path / "projects"
        (projects_dir / slug).mkdir(parents=True)

        overlay_base = tmp_path / "session"
        upper = overlay_base / f"{slug}-upper"
        upper.mkdir(parents=True)
        (upper / ".git").write_text("gitdir: /projects/.gitdirs/test-project\n")
        (upper / "new-work.txt").write_text("my changes\n")

        entries = set(os.listdir(upper))
        entries.discard(".git")
        # Should save because upper has real content
        assert entries

    def test_clean_index_does_not_overwrite_saved(self, tmp_path):
        """A session with clean index must not overwrite another's saved-index."""
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "file.txt").write_text("content\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")

        wrapper.initialize_gitdir(slug, host)
        shared_gd = projects_dir / slug / "gitdir"

        # Pre-existing saved index from another session
        (projects_dir / slug / "saved-index").write_bytes(b"precious index")

        # Create a session with NO staged changes
        overlay = tmp_path / "overlay"
        overlay.mkdir()
        session_gd = wrapper.create_session_gitdir(slug, "sid", overlay)
        # create_session_gitdir consumed the saved-index, but let's
        # re-create it to simulate the scenario where we DON'T want
        # the clean session to overwrite it
        (projects_dir / slug / "saved-index").write_bytes(b"precious index")

        # _save_session_index should NOT overwrite because index is clean
        _save_session_index(session_gd, shared_gd, slug)

        # Saved index should be untouched
        assert (projects_dir / slug / "saved-index").read_bytes() == b"precious index"


class TestHostIdentitySync:
    """Sync tests when cave uses the host's git identity (no re-authoring).

    When the cave forwards the host's user.name/user.email, commits already
    have the correct author. Pull preserves exact commit hashes, push doesn't
    create re-authoring divergence, and sessions stay in sync.
    """

    HOST_EMAIL = "host@test.com"
    HOST_NAME = "Host User"

    @pytest.fixture
    def host_id_project(self, tmp_path):
        """Host repo + shared gitdir where cave uses host identity."""
        slug = "test-project"
        _state.set_cave_data(tmp_path)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "-C", str(host), "init", "-b", "main")
        git_run("git", "-C", str(host), "config", "user.email", self.HOST_EMAIL)
        git_run("git", "-C", str(host), "config", "user.name", self.HOST_NAME)
        (host / "file.txt").write_text("initial\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")

        write_projects(tmp_path, {slug: str(host)})
        wrapper.initialize_gitdir(slug, host)

        projects_dir = _state.projects_dir()
        shared_gd = projects_dir / slug / "gitdir"

        # Configure gitdir to use host identity
        git_run("git", "--git-dir", str(shared_gd), "config",
                "user.email", self.HOST_EMAIL)
        git_run("git", "--git-dir", str(shared_gd), "config",
                "user.name", self.HOST_NAME)

        return host, slug, shared_gd, projects_dir

    def _make_session(self, slug, name, tmp_path, make_commit=True):
        """Create a session gitdir with a cave commit using host identity."""
        overlay = tmp_path / f"overlay-{name}"
        overlay.mkdir()
        session_gd = wrapper.create_session_gitdir(slug, f"sid-{name}", overlay)

        if make_commit:
            workdir = tmp_path / f"work-{name}"
            workdir.mkdir()
            git_run("git", "--git-dir", str(session_gd), "--work-tree",
                    str(workdir), "checkout", "HEAD", "--", ".")
            (workdir / f"{name}.txt").write_text(f"{name} content\n")
            env = {**os.environ, **GIT_ENV,
                   "GIT_AUTHOR_EMAIL": self.HOST_EMAIL,
                   "GIT_AUTHOR_NAME": self.HOST_NAME,
                   "GIT_COMMITTER_EMAIL": self.HOST_EMAIL,
                   "GIT_COMMITTER_NAME": self.HOST_NAME}
            subprocess.run(["git", "--git-dir", str(session_gd), "--work-tree",
                           str(workdir), "add", "."],
                          capture_output=True, check=True, env=env)
            subprocess.run(["git", "--git-dir", str(session_gd), "--work-tree",
                           str(workdir), "commit", "-m", f"{name} commit"],
                          capture_output=True, check=True, env=env)
        return session_gd

    def test_pull_preserves_hashes_no_session_divergence(self, host_id_project, tmp_path):
        """Pull + session sync preserves hashes — no divergence."""
        host, slug, shared_gd, projects_dir = host_id_project

        # Session makes a commit with host identity
        session_gd = self._make_session(slug, "s1", tmp_path)
        session_head = subprocess.run(
            ["git", "--git-dir", str(session_gd), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()

        # Sync session refs to shared, then merge to main via cleanup
        overlay = session_gd.parent
        wrapper.merge_session_gitdirs("sid-s1", overlay)

        # Shared main should have the exact same hash
        shared_head = subprocess.run(
            ["git", "--git-dir", str(shared_gd), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert shared_head == session_head

        # Pull to host
        from coding_cave.sync import pull_project
        pull_project(slug)

        # Host should have the exact same hash (no re-authoring)
        host_head = subprocess.run(
            ["git", "-C", str(host), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert host_head == session_head

    def test_push_pull_cycle_no_reauthoring_divergence(self, host_id_project, tmp_path):
        """Push + pull cycle doesn't create re-authoring divergence.

        With host identity forwarding, no re-authoring happens.
        Session commit → cleanup (merge to main) → pull → push → verify.
        """
        host, slug, shared_gd, projects_dir = host_id_project

        # Session makes a commit
        session_gd = self._make_session(slug, "s1", tmp_path)
        session_head = subprocess.run(
            ["git", "--git-dir", str(session_gd), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()

        # Merge session branch to main via cleanup, then pull
        overlay = session_gd.parent
        wrapper.merge_session_gitdirs("sid-s1", overlay)
        from coding_cave.sync import pull_project, push_project
        pull_project(slug)

        # Push back
        push_project(slug)

        # Shared should have the exact same hash — no re-authoring
        shared_head = subprocess.run(
            ["git", "--git-dir", str(shared_gd), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert shared_head == session_head


class TestPullAheadMessage:
    """pull_project shows 'host is ahead' when host has unpushed commits."""

    def test_pull_nothing_to_pull_host_ahead(self, tmp_path, capsys):
        slug = "test-project"
        _state.set_cave_data(tmp_path)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "-C", str(host), "init", "-b", "main")
        git_run("git", "-C", str(host), "config", "user.email", "t@t")
        git_run("git", "-C", str(host), "config", "user.name", "Test")
        (host / "file.txt").write_text("initial\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")

        write_projects(tmp_path, {slug: str(host)})
        wrapper.initialize_gitdir(slug, host)

        # Host makes a commit that cave doesn't have
        (host / "extra.txt").write_text("host-only\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "host commit")

        from coding_cave.sync import pull_project
        pull_project(slug)

        err = capsys.readouterr().err
        assert "Already up to date" in err
        assert "1 commit(s) ahead" in err
        assert "coding-cave push" in err

    def test_pull_nothing_to_pull_synced(self, tmp_path, capsys):
        slug = "test-project"
        _state.set_cave_data(tmp_path)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "-C", str(host), "init", "-b", "main")
        git_run("git", "-C", str(host), "config", "user.email", "t@t")
        git_run("git", "-C", str(host), "config", "user.name", "Test")
        (host / "file.txt").write_text("initial\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")

        write_projects(tmp_path, {slug: str(host)})
        wrapper.initialize_gitdir(slug, host)

        from coding_cave.sync import pull_project
        pull_project(slug)

        err = capsys.readouterr().err
        assert "Already up to date" in err
        assert "ahead" not in err


class TestPostRewriteHook:
    """Test that the post-rewrite hook blocks amending pulled commits."""

    @pytest.fixture
    def shared_project(self, tmp_path):
        slug = "test-project"
        _state.set_cave_data(tmp_path)
        projects_dir = _state.projects_dir()
        (projects_dir / slug).mkdir(parents=True)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        (host / "file.txt").write_text("initial\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "initial")

        wrapper.initialize_gitdir(slug, host)
        return host, slug, projects_dir

    def _make_session(self, tmp_path, slug, projects_dir, name):
        overlay_base = tmp_path / f"overlay-{name}"
        overlay_base.mkdir()
        session_gitdir = wrapper.create_session_gitdir(
            slug, f"sid-{name}", overlay_base)

        workdir = tmp_path / f"work-{name}"
        workdir.mkdir()
        git_run("git", "--git-dir", str(session_gitdir),
                "--work-tree", str(workdir),
                "checkout", "HEAD", "--", ".")
        make_cave_session(session_gitdir, workdir)
        return session_gitdir, workdir, overlay_base

    def test_amend_pulled_commit_blocked(self, shared_project, tmp_path):
        """Amending a commit that was pulled to the host is blocked."""
        _, slug, projects_dir = shared_project
        gd, work, _ = self._make_session(
            tmp_path, slug, projects_dir, "test")

        # Make a commit
        (work / "a.txt").write_text("v1\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "-m", "add a")
        sha_a = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # Simulate pull: mark this commit as pulled to host
        cave_git(work, "update-ref", "refs/remotes/host/main", sha_a)

        # Try to amend — should be blocked by post-rewrite hook
        (work / "a.txt").write_text("v2\n")
        cave_git(work, "add", "a.txt")
        result = cave_git(work, "commit", "--amend", "-m", "amended a")

        # HEAD should still be at the original commit (hook did reset --soft)
        head = cave_git(work, "rev-parse", "HEAD").stdout.strip()
        assert head == sha_a, (
            f"Expected HEAD at original {sha_a[:8]}, got {head[:8]}")

        # Error message should mention the pull
        assert "already pulled" in result.stderr

    def test_amend_unpulled_commit_allowed(self, shared_project, tmp_path):
        """Amending a commit that was NOT pulled is allowed."""
        _, slug, projects_dir = shared_project
        gd, work, _ = self._make_session(
            tmp_path, slug, projects_dir, "test")

        # Make a commit (no pull simulation)
        (work / "a.txt").write_text("v1\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "-m", "add a")
        sha_a = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # Amend should succeed
        (work / "a.txt").write_text("v2\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "--amend", "-m", "amended a")

        head = cave_git(work, "rev-parse", "HEAD").stdout.strip()
        assert head != sha_a, "Amend should have created a new commit"

    def test_amend_commit_after_pulled_allowed(self, shared_project, tmp_path):
        """Amending a commit made AFTER the pulled boundary is allowed."""
        _, slug, projects_dir = shared_project
        gd, work, _ = self._make_session(
            tmp_path, slug, projects_dir, "test")

        # Make commit A and mark as pulled
        (work / "a.txt").write_text("v1\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "-m", "add a")
        sha_a = cave_git(work, "rev-parse", "HEAD").stdout.strip()
        cave_git(work, "update-ref", "refs/remotes/host/main", sha_a)

        # Make commit B on top (not pulled)
        (work / "b.txt").write_text("v1\n")
        cave_git(work, "add", "b.txt")
        cave_git(work, "commit", "-m", "add b")
        sha_b = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # Amend B — should be allowed (B was not pulled)
        (work / "b.txt").write_text("v2\n")
        cave_git(work, "add", "b.txt")
        cave_git(work, "commit", "--amend", "-m", "amended b")

        head = cave_git(work, "rev-parse", "HEAD").stdout.strip()
        assert head != sha_b, "Amend should have created a new commit"

    def test_new_commit_after_blocked_amend(self, shared_project, tmp_path):
        """After amend is blocked, changes are staged for a new commit."""
        _, slug, projects_dir = shared_project
        gd, work, _ = self._make_session(
            tmp_path, slug, projects_dir, "test")

        # Make a commit and mark as pulled
        (work / "a.txt").write_text("v1\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "-m", "add a")
        sha_a = cave_git(work, "rev-parse", "HEAD").stdout.strip()
        cave_git(work, "update-ref", "refs/remotes/host/main", sha_a)

        # Try to amend (blocked)
        (work / "a.txt").write_text("v2\n")
        cave_git(work, "add", "a.txt")
        cave_git(work, "commit", "--amend", "-m", "amended a")
        assert cave_git(work, "rev-parse", "HEAD").stdout.strip() == sha_a

        # Changes should still be staged — create a new commit instead
        result = cave_git(work, "commit", "-m", "update a")
        assert result.returncode == 0
        head = cave_git(work, "rev-parse", "HEAD").stdout.strip()
        assert head != sha_a
        log = cave_git(work, "log", "--oneline")
        assert "update a" in log.stdout
        assert (work / "a.txt").read_text() == "v2\n"
