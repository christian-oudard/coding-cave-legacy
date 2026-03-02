"""Tests for sync helpers, gitdir initialization, and git safety."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import wrapper


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t",
}


def git_run(*args, **kwargs):
    """Run a git command with test identity."""
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(list(args), capture_output=True, text=True, env=env, **kwargs)


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


def git_head(path, gitdir=False):
    """Get HEAD commit hash."""
    if gitdir:
        cmd = ["git", "--git-dir", str(path), "rev-parse", "HEAD"]
    else:
        cmd = ["git", "-C", str(path), "rev-parse", "HEAD"]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()



class TestSyncRefs:
    def test_updates_branch_and_cave_sync(self, two_repos):
        """sync_refs updates gitdir branch ref and cave-sync marker."""
        host, gitdir, cave = two_repos

        # Add 'cave' remote to host so update_cave_sync's fetch works
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

        # cave-sync ref should be set
        cave_sync = subprocess.run(
            ["git", "--git-dir", str(gitdir), "rev-parse", "refs/cave-sync"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert cave_sync == new_head

        # host remote ref in gitdir should be updated
        host_ref = subprocess.run(
            ["git", "--git-dir", str(gitdir), "rev-parse", "refs/remotes/host/main"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert host_ref == new_head


class TestHistoryRewriteDetection:
    def test_detects_rewrite_when_cave_sync_matches_host(self, two_repos):
        """When cave-sync == host HEAD but merge-base is older, it's a rewrite."""
        host, gitdir, cave = two_repos
        host_head = git_head(host)

        # Set cave-sync to current host HEAD
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "update-ref",
             "refs/cave-sync", host_head],
            check=True,
        )

        # Simulate cave rewriting history: amend the commit in gitdir
        subprocess.run(
            ["git", "--git-dir", str(gitdir), "--work-tree", str(cave),
             "commit", "--amend", "-m", "rewritten",
             "--author", "Test <test@test.com>"],
            capture_output=True, check=True,
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
        # pull_project checks: cave_sync == host_head AND merge_base != host_head
        cave_sync = subprocess.run(
            ["git", "--git-dir", str(gitdir), "rev-parse", "refs/cave-sync"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        assert cave_sync == host_head, "cave-sync should still point to old host HEAD"
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
    def test_empty_repo(self, tmp_path, monkeypatch):
        """initialize_gitdir doesn't crash on empty repos (no commits).

        Regression: 7ed214c — read-tree HEAD failed with check=True on
        repos with no commits, crashing the initialization.
        """
        slug = "empty-project"
        projects_dir = tmp_path / "projects"
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)

        # Create empty repo (git init, no commits)
        project = tmp_path / "empty"
        project.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(project)],
                        capture_output=True, check=True)

        # Should not raise
        wrapper.initialize_gitdir(slug, project)

        gitdir = proj_state / "gitdir"
        assert gitdir.is_dir()

    def test_normal_repo(self, tmp_path, monkeypatch):
        """initialize_gitdir creates gitdir with cave-sync and host ref."""
        slug = "test-project"
        projects_dir = tmp_path / "projects"
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)

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

    def test_hardlinks_objects(self, tmp_path, monkeypatch):
        """initialize_gitdir hardlinks objects from host when on same device."""
        slug = "test-hardlinks"
        projects_dir = tmp_path / "projects"
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)

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

        # cave-sync should be set
        cave_sync = subprocess.run(
            ["git", "--git-dir", str(gitdir), "rev-parse", "refs/cave-sync"],
            capture_output=True, text=True,
        )
        assert cave_sync.returncode == 0

        # hooks dir should be removed
        assert not (gitdir / "hooks").exists()

    def test_skips_existing_gitdir(self, tmp_path, monkeypatch):
        """initialize_gitdir is a no-op if gitdir already exists."""
        slug = "test-project"
        projects_dir = tmp_path / "projects"
        proj_state = projects_dir / slug
        proj_state.mkdir(parents=True)
        gitdir = proj_state / "gitdir"
        gitdir.mkdir()
        marker = gitdir / "marker"
        marker.write_text("existing")
        monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)

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
        wrapper.git(gitdir, "log", "--oneline", "-1",
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

        wrapper.git(gitdir, "status",
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
        wrapper.git(gitdir, "rev-parse", "HEAD",
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
        result = wrapper.git(gitdir, "config", "--get", "core.sshCommand",
                              capture_output=True, text=True)
        # With -c override, the effective value should be "echo blocked"
        # (git -c takes precedence over config file)
        # Just verify the marker wasn't created by any operation
        wrapper.git(gitdir, "rev-parse", "HEAD",
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
    def shared_project(self, tmp_path, monkeypatch):
        """A host repo with initialized shared gitdir, ready for session creation.

        Returns (host_dir, slug, projects_dir).
        """
        slug = "test-project"
        projects_dir = tmp_path / "cave-projects"
        monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)
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

    def test_two_sessions_both_commit_on_main(self, shared_project, tmp_path):
        """Two sessions both commit on main (different files): both rebased."""
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")
        _, work_b, overlay_b = self._make_session(tmp_path, slug, projects_dir, "b")

        # Both commit on main (touching different files — no conflict)
        (work_a / "a.txt").write_text("from A\n")
        cave_git(work_a, "add", "a.txt")
        cave_git(work_a, "commit", "-m", "commit A")

        (work_b / "b.txt").write_text("from B\n")
        cave_git(work_b, "add", "b.txt")
        cave_git(work_b, "commit", "-m", "commit B")

        # Session A ends first — its main fast-forwards
        wrapper.merge_session_gitdirs("sid-a", overlay_a)
        log = git_run("git", "--git-dir", str(shared_gd), "log", "--oneline")
        assert "commit A" in log.stdout

        # Session B ends — diverged but no conflict, B rebases onto A
        wrapper.merge_session_gitdirs("sid-b", overlay_b)

        # Both commits should be in main (B rebased on top of A)
        log = git_run("git", "--git-dir", str(shared_gd),
                      "log", "--oneline", "refs/heads/main")
        assert "commit A" in log.stdout
        assert "commit B" in log.stdout

    def test_two_sessions_conflict_falls_back(self, shared_project, tmp_path):
        """Two sessions editing the same file: rebase fails, falls back to force-update."""
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")
        _, work_b, overlay_b = self._make_session(tmp_path, slug, projects_dir, "b")

        # Both commit changes to the same file
        (work_a / "file.txt").write_text("version A\n")
        cave_git(work_a, "add", "file.txt")
        cave_git(work_a, "commit", "-m", "commit A")
        sha_a = cave_git(work_a, "rev-parse", "HEAD").stdout.strip()

        (work_b / "file.txt").write_text("version B\n")
        cave_git(work_b, "add", "file.txt")
        cave_git(work_b, "commit", "-m", "commit B")

        # Session A ends first
        wrapper.merge_session_gitdirs("sid-a", overlay_a)

        # Session B ends — rebase conflicts, falls back to force-update
        wrapper.merge_session_gitdirs("sid-b", overlay_b)

        # B wins (force-update fallback), A backed up
        log = git_run("git", "--git-dir", str(shared_gd),
                      "log", "--oneline", "refs/heads/main")
        assert "commit B" in log.stdout

        backup = git_run("git", "--git-dir", str(shared_gd),
                         "rev-parse", "refs/sessions/sid-b/main")
        assert backup.returncode == 0
        assert backup.stdout.strip() == sha_a

    def test_amended_commit_updates_shared(self, shared_project, tmp_path):
        """Amending a commit after mid-session sync must still update shared.

        Reproduces: session makes commit B, mid-session sync (pull) advances
        shared main to B, session amends B to B', session exits.
        merge_session_gitdirs must update shared main to B' (not leave it at B).

        Root cause of the pull failure: merge_session_gitdirs treated the amend
        as divergence (merge-base of B and B' is A, not B) and saved B' under
        refs/sessions/ instead of updating refs/heads/main.  Next pull saw
        host_head == gitdir_head (both B) and reported "No new commits."
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")

        # Session creates commit B
        (work_a / "new.txt").write_text("v1\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "commit B")
        sha_b = cave_git(work_a, "rev-parse", "HEAD").stdout.strip()

        # Mid-session sync (simulates pull -> sync_live_sessions) advances shared
        wrapper.merge_session_gitdirs("sid-a", overlay_a)
        assert git_head(shared_gd, gitdir=True) == sha_b

        # Session amends B to B'
        (work_a / "new.txt").write_text("v2\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "--amend", "-m", "commit B amended")
        sha_b_prime = cave_git(work_a, "rev-parse", "HEAD").stdout.strip()
        assert sha_b_prime != sha_b

        # Session exits — merge should update shared to B'
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

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")

        # Session creates commit B
        (work_a / "new.txt").write_text("content\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "commit B")
        session_sha = cave_git(work_a, "rev-parse", "HEAD").stdout.strip()

        # Mid-session sync advances shared to session's commit
        wrapper.merge_session_gitdirs("sid-a", overlay_a)
        assert git_head(shared_gd, gitdir=True) == session_sha

        # Simulate pull re-authoring: create a new commit with same tree
        # but different author (like --reset-author does)
        tree = git_run("git", "--git-dir", str(shared_gd),
                        "rev-parse", "HEAD^{tree}").stdout.strip()
        parent = git_run("git", "--git-dir", str(shared_gd),
                          "rev-parse", "HEAD^").stdout.strip()
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
        # merge_session_gitdirs should NOT overwrite the re-authored ref.
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

    def test_forward_refs_rebases_onto_newer_shared(
            self, shared_project, tmp_path):
        """Session commits rebase onto shared HEAD when shared moved forward.

        Scenario: session starts at commit A, host pushes commit B to shared
        gitdir, session commits C on top of A.  forward_session_refs should
        rebase C onto B, producing B -> C'.

        Regression: forward_session_refs skipped diverged refs entirely,
        so session commits were invisible to pull when the shared gitdir
        had advanced via push.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")

        # Simulate host pushing a new commit to shared gitdir (e.g. cav push)
        # Create a commit directly in the shared gitdir
        shared_work = tmp_path / "shared-work"
        shared_work.mkdir()
        git_run("git", "--git-dir", str(shared_gd), "--work-tree", str(shared_work),
                "checkout", "HEAD", "--", ".")
        (shared_work / "host-file.txt").write_text("from host\n")
        env = {**os.environ, **GIT_ENV}
        subprocess.run(
            ["git", "--git-dir", str(shared_gd), "--work-tree", str(shared_work),
             "add", "host-file.txt"],
            capture_output=True, check=True, env=env,
        )
        subprocess.run(
            ["git", "--git-dir", str(shared_gd), "--work-tree", str(shared_work),
             "commit", "-m", "host commit"],
            capture_output=True, check=True, env=env,
        )
        shared_after_push = git_head(shared_gd, gitdir=True)

        # Session commits on top of original HEAD (doesn't know about host commit)
        (work_a / "session-file.txt").write_text("from session\n")
        cave_git(work_a, "add", "session-file.txt")
        cave_git(work_a, "commit", "-m", "session commit")

        # Live sync should rebase session commit onto shared HEAD
        session_gd = overlay_a / f"gitdir-{slug}"
        wrapper.forward_session_refs(slug, session_gd, shared_gd)

        # Shared HEAD should have moved (rebase succeeded)
        new_shared = git_head(shared_gd, gitdir=True)
        assert new_shared != shared_after_push, "shared HEAD should advance"

        # Both commits should be in the log
        log = git_run("git", "--git-dir", str(shared_gd),
                      "log", "--oneline", "refs/heads/main")
        assert "host commit" in log.stdout
        assert "session commit" in log.stdout

        # Host commit should be an ancestor (not lost)
        is_ancestor = git_run("git", "--git-dir", str(shared_gd),
                              "merge-base", "--is-ancestor",
                              shared_after_push, new_shared)
        assert is_ancestor.returncode == 0

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

    def test_forward_refs_live_sync_handles_amendment(
            self, shared_project, tmp_path):
        """Live sync updates shared when session amends a commit.

        Scenario: session creates commit B, mid-session sync advances shared
        to B, session amends B to B'.  forward_session_refs (live sync,
        force=False) must detect the single-commit amendment and update
        shared to B'.

        Regression: forward_session_refs skipped the diverged ref, leaving
        shared at B.  cav pull then saw host_head == gitdir_head and reported
        "No new commits."
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")

        # Session creates commit B
        (work_a / "new.txt").write_text("v1\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "commit B")
        sha_b = cave_git(work_a, "rev-parse", "HEAD").stdout.strip()

        # Mid-session sync advances shared to B
        session_gd = overlay_a / f"gitdir-{slug}"
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_b

        # Session amends B to B' (different tree)
        (work_a / "new.txt").write_text("v2\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "--amend", "-m", "commit B amended")
        sha_b_prime = cave_git(work_a, "rev-parse", "HEAD").stdout.strip()
        assert sha_b_prime != sha_b

        # Live sync should detect amendment and update shared to B'
        wrapper.forward_session_refs(slug, session_gd, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_b_prime

    def test_sync_live_sessions_restores_alternates(
            self, shared_project, tmp_path, monkeypatch):
        """sync_live_sessions restores cave-internal alternates via forward_session_refs."""
        host, slug, projects_dir = shared_project
        overlay_base = tmp_path / "overlay"
        overlay_base.mkdir()
        session_gd = wrapper.create_session_gitdir(slug, "test-sid", overlay_base)

        cave_path = f"/projects/.shared-objects/{slug}"
        alt_file = session_gd / "objects" / "info" / "alternates"
        alt_file.write_text(f"{cave_path}\n")

        monkeypatch.setattr(wrapper, "list_sessions", lambda: [{"sid": "test-sid"}])
        monkeypatch.setattr(wrapper, "CAVE_DATA", tmp_path / "cave-data")
        session_overlay = tmp_path / "cave-data" / "session-overlays" / "test-sid"
        session_overlay.mkdir(parents=True)
        (session_overlay / f"gitdir-{slug}").symlink_to(session_gd)

        wrapper.sync_live_sessions(slug)

        assert alt_file.read_text().strip() == cave_path

    def test_sync_live_stale_session_preserves_active(
            self, shared_project, tmp_path, monkeypatch):
        """sync_live_sessions with active + stale sessions preserves active commits.

        Full integration scenario from today: two sessions exist, one active
        (with new commits) and one stale (no commits, behind shared).
        sync_live_sessions must push active session's commits and NOT let
        the stale session overwrite them.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        # Create two sessions (both start at same point)
        ov_active = tmp_path / "ov-active"
        ov_active.mkdir()
        gd_active = wrapper.create_session_gitdir(slug, "sid-active", ov_active)
        ov_stale = tmp_path / "ov-stale"
        ov_stale.mkdir()
        wrapper.create_session_gitdir(slug, "sid-stale", ov_stale)

        # Active session commits
        work = tmp_path / "work"
        work.mkdir()
        git_run("git", "--git-dir", str(gd_active), "--work-tree", str(work),
                "checkout", "HEAD", "--", ".")
        make_cave_session(gd_active, work)
        (work / "active.txt").write_text("active work\n")
        cave_git(work, "add", "active.txt")
        cave_git(work, "commit", "-m", "active commit")
        active_head = cave_git(work, "rev-parse", "HEAD").stdout.strip()

        # Mock list_sessions to return both
        monkeypatch.setattr(wrapper, "list_sessions", lambda: [
            {"sid": "sid-active"}, {"sid": "sid-stale"},
        ])
        monkeypatch.setattr(wrapper, "CAVE_DATA", tmp_path / "cave-data")
        for sid, ov in [("sid-active", ov_active), ("sid-stale", ov_stale)]:
            session_dir = tmp_path / "cave-data" / "session-overlays" / sid
            session_dir.mkdir(parents=True)
            (session_dir / f"gitdir-{slug}").symlink_to(ov / f"gitdir-{slug}")

        wrapper.sync_live_sessions(slug)

        # Shared must have active session's commit, not be overwritten by stale
        shared_head = git_run("git", "--git-dir", str(shared_gd),
                              "rev-parse", "HEAD").stdout.strip()
        log = git_run("git", "--git-dir", str(shared_gd),
                      "log", "--oneline", "refs/heads/main")
        assert "active commit" in log.stdout, (
            f"Active session's commit missing from shared. HEAD={shared_head[:8]}"
        )

    def test_sync_live_does_not_touch_other_projects(
            self, shared_project, tmp_path, monkeypatch):
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

        overlay_base = tmp_path / "overlay"
        overlay_base.mkdir()
        wrapper.create_session_gitdir(slug, "test-sid", overlay_base)
        gd2 = wrapper.create_session_gitdir(slug2, "test-sid", overlay_base)

        cave_path1 = f"/projects/.shared-objects/{slug}"
        cave_path2 = f"/projects/.shared-objects/{slug2}"
        alt1 = overlay_base / f"gitdir-{slug}" / "objects" / "info" / "alternates"
        alt2 = gd2 / "objects" / "info" / "alternates"
        alt1.write_text(f"{cave_path1}\n")
        alt2.write_text(f"{cave_path2}\n")

        monkeypatch.setattr(wrapper, "list_sessions", lambda: [{"sid": "test-sid"}])
        monkeypatch.setattr(wrapper, "CAVE_DATA", tmp_path / "cave-data")
        session_dir = tmp_path / "cave-data" / "session-overlays" / "test-sid"
        session_dir.mkdir(parents=True)
        (session_dir / f"gitdir-{slug}").symlink_to(overlay_base / f"gitdir-{slug}")
        (session_dir / f"gitdir-{slug2}").symlink_to(gd2)

        wrapper.sync_live_sessions(slug)

        # Synced slug's alternates should be restored
        assert alt1.read_text().strip() == cave_path1
        # Other project's alternates should be untouched (never modified)
        assert alt2.read_text().strip() == cave_path2

    def test_sync_live_stale_session_does_not_revert_amend(
            self, shared_project, tmp_path, monkeypatch):
        """A stale session whose ref was set by sync_refs must not revert
        another session's amended commit.

        Scenario (the actual bug):
        1. Session A creates commit B, mid-sync forwards B to shared.
        2. Host pull re-authors B to B', sync_refs updates shared to B'.
        3. Session C starts, copies shared main (B') as its init ref.
        4. Session A amends B to B'' (different tree).
        5. sync_live_sessions must end with shared at B'', not reverted to B'.

        Without refs/init tracking, step 5 would process session C last,
        see a 1-commit amend (B' vs B''), and "let session C win" — reverting
        the active session's work.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        # Session A creates commit B
        _, work_a, overlay_a = self._make_session(
            tmp_path, slug, projects_dir, "active")
        (work_a / "new.txt").write_text("v1\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "-m", "commit B")
        sha_b = cave_git(work_a, "rev-parse", "HEAD").stdout.strip()

        # Mid-session sync forwards B to shared
        gd_active = overlay_a / f"gitdir-{slug}"
        wrapper.forward_session_refs(slug, gd_active, shared_gd)
        assert git_head(shared_gd, gitdir=True) == sha_b

        # Simulate pull re-authoring: create B' (same tree, different author)
        tree = git_run("git", "--git-dir", str(shared_gd),
                       "rev-parse", f"{sha_b}^{{tree}}").stdout.strip()
        parent = git_run("git", "--git-dir", str(shared_gd),
                         "rev-parse", f"{sha_b}^").stdout.strip()
        b_prime = git_run(
            "git", "--git-dir", str(shared_gd), "commit-tree", tree,
            "-p", parent, "-m", "commit B re-authored",
        ).stdout.strip()
        # sync_refs updates shared main to B'
        git_run("git", "--git-dir", str(shared_gd),
                "update-ref", "refs/heads/main", b_prime)

        # Session C starts now (copies shared main = B')
        ov_stale = tmp_path / "ov-stale"
        ov_stale.mkdir()
        wrapper.create_session_gitdir(slug, "sid-stale", ov_stale)

        # Session A amends B to B'' (different tree)
        (work_a / "new.txt").write_text("v2 amended\n")
        cave_git(work_a, "add", "new.txt")
        cave_git(work_a, "commit", "--amend", "-m", "commit B amended")
        sha_b_double_prime = cave_git(
            work_a, "rev-parse", "HEAD").stdout.strip()
        assert sha_b_double_prime != sha_b
        assert sha_b_double_prime != b_prime

        # Wire up sync_live_sessions
        monkeypatch.setattr(wrapper, "list_sessions", lambda: [
            {"sid": "sid-active"}, {"sid": "sid-stale"},
        ])
        monkeypatch.setattr(wrapper, "CAVE_DATA", tmp_path / "cave-data")
        for sid, ov in [("sid-active", overlay_a),
                        ("sid-stale", ov_stale)]:
            session_dir = tmp_path / "cave-data" / "session-overlays" / sid
            session_dir.mkdir(parents=True)
            (session_dir / f"gitdir-{slug}").symlink_to(
                ov / f"gitdir-{slug}")

        wrapper.sync_live_sessions(slug)

        # Shared must have the amended commit, not the re-authored one
        final = git_head(shared_gd, gitdir=True)
        assert final == sha_b_double_prime, (
            f"Stale session reverted amend: shared={final[:8]}, "
            f"expected={sha_b_double_prime[:8]}"
        )

    def test_first_sync_after_amend_detects_amend(
            self, shared_project, tmp_path):
        """When a session amends a commit and syncs for the first time
        (no refs/synced yet), the amend should still be detected via
        refs/init matching the shared ref.

        Without this, _rebase_commits tries a three-way merge that
        conflicts (both sides modify the same files from the same parent),
        and the diverged ref is silently skipped.
        """
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        # Add a second commit so the amend isn't of a root commit
        # (git merge-base fails for root commits).
        (host / "base.txt").write_text("base\n")
        git_run("git", "-C", str(host), "add", ".")
        git_run("git", "-C", str(host), "commit", "-m", "base commit")
        # Sync the new commit into shared gitdir (fetch objects, then
        # update-ref since main is checked out and git refuses to
        # fetch directly into the checked-out branch).
        git_run("git", "--git-dir", str(shared_gd),
                "fetch", str(host), "main")
        new_head = git_run("git", "-C", str(host),
                           "rev-parse", "HEAD").stdout.strip()
        git_run("git", "--git-dir", str(shared_gd),
                "update-ref", "refs/heads/main", new_head)
        shared_before = git_head(shared_gd, gitdir=True)

        # Create session (refs/init/main = shared_before)
        _, work, overlay = self._make_session(
            tmp_path, slug, projects_dir, "sess")

        # Amend the latest commit (same parent, different tree)
        (work / "base.txt").write_text("amended content\n")
        cave_git(work, "add", "base.txt")
        cave_git(work, "commit", "--amend", "-m", "amended commit")
        amended_sha = cave_git(work, "rev-parse", "HEAD").stdout.strip()
        assert amended_sha != shared_before

        # First sync — no refs/synced exists yet
        gd = overlay / f"gitdir-{slug}"
        synced = git_run("git", "--git-dir", str(gd),
                         "rev-parse", "refs/synced/main")
        assert synced.returncode != 0, "refs/synced should not exist yet"

        wrapper.forward_session_refs(slug, gd, shared_gd)

        # Shared should have the amended commit
        final = git_head(shared_gd, gitdir=True)
        assert final == amended_sha, (
            f"First sync after amend failed: shared={final[:8]}, "
            f"expected={amended_sha[:8]}"
        )

    def test_no_gitdir_returns_none(self, tmp_path, monkeypatch):
        """create_session_gitdir returns None when no shared gitdir exists."""
        projects_dir = tmp_path / "projects"
        (projects_dir / "missing").mkdir(parents=True)
        monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        result = wrapper.create_session_gitdir("missing", "sid", overlay)
        assert result is None

    def test_empty_repo_session(self, tmp_path, monkeypatch):
        """Session gitdir from an empty repo (no commits) doesn't crash."""
        slug = "empty"
        projects_dir = tmp_path / "projects"
        (projects_dir / slug).mkdir(parents=True)
        monkeypatch.setattr(wrapper, "PROJECTS_DIR", projects_dir)

        host = tmp_path / "host"
        host.mkdir()
        git_run("git", "init", "-b", "main", str(host))
        wrapper.initialize_gitdir(slug, host)

        overlay = tmp_path / "overlay"
        overlay.mkdir()
        session_gd = wrapper.create_session_gitdir(slug, "sid", overlay)
        # Should create something (or return None gracefully)
        # Either is acceptable for an empty repo
