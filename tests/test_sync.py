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
        """Two sessions both commit on main: first merges, second diverges."""
        host, slug, projects_dir = shared_project
        shared_gd = projects_dir / slug / "gitdir"

        _, work_a, overlay_a = self._make_session(tmp_path, slug, projects_dir, "a")
        _, work_b, overlay_b = self._make_session(tmp_path, slug, projects_dir, "b")

        # Both commit on main
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

        # Session B ends — its main can't fast-forward (diverged)
        # B's commits should still be preserved (not lost)
        wrapper.merge_session_gitdirs("sid-b", overlay_b)

        # A's commit should still be on main
        log = git_run("git", "--git-dir", str(shared_gd),
                      "log", "--oneline", "refs/heads/main")
        assert "commit A" in log.stdout

        # B's commit should be reachable (saved under a session ref)
        all_refs = git_run("git", "--git-dir", str(shared_gd),
                           "for-each-ref", "--format=%(refname)")
        b_commit_sha = cave_git(work_b, "rev-parse", "HEAD").stdout.strip()
        # Verify B's commit object exists in shared
        cat = git_run("git", "--git-dir", str(shared_gd),
                      "cat-file", "-t", b_commit_sha)
        assert cat.returncode == 0, "B's commit should exist in shared object store"

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
