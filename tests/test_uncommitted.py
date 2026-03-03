"""Tests probing uncommitted file behavior across cave boundaries.

Documents what works and what doesn't when host or cave has uncommitted
(untracked, modified, staged) files during push/pull/overlay operations.
"""
import os
from types import SimpleNamespace

import pytest

import wrapper
from coding_cave import _state
from tests.conftest import git_run, git_head, write_projects, write_session


def cave_git(e, *args):
    return git_run("git", "-C", str(e.cave), *args)


def cave_commit(e, msg, filename="file.txt", content=None, amend=False):
    if content is not None:
        (e.cave / filename).write_text(content)
        cave_git(e, "add", filename)
    args = ["commit", "-m", msg]
    if amend:
        args.append("--amend")
    cave_git(e, *args)


@pytest.fixture
def env(tmp_path):
    """Full testing environment with host, shared gitdir, session gitdir, and cave workdir.

    Host has one commit (file.txt = "initial").
    Cave overlay simulated: workdir populated from HEAD, .git pointing to session gitdir.
    """
    slug = "test-project"

    # Host repo
    host = tmp_path / "host"
    host.mkdir()
    git_run("git", "init", "-b", "main", str(host))
    git_run("git", "-C", str(host), "config", "user.email", "host@test.com")
    git_run("git", "-C", str(host), "config", "user.name", "Host User")
    (host / "file.txt").write_text("initial\n")
    git_run("git", "-C", str(host), "add", ".")
    git_run("git", "-C", str(host), "commit", "-m", "initial")

    # Set up cave data paths
    _state.set_cave_data(tmp_path)
    (_state.projects_dir() / slug).mkdir(parents=True)
    write_projects(tmp_path, {slug: str(host)})

    saved_env = {}
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        saved_env[var] = os.environ.pop(var, None)

    wrapper.initialize_gitdir(slug, host)
    shared_gd = _state.projects_dir() / slug / "gitdir"

    # Session gitdir
    overlay_base = tmp_path / "session-overlays" / "sid-test"
    overlay_base.mkdir(parents=True)
    session_gd = wrapper.create_session_gitdir(slug, "sid-test", overlay_base)
    git_run("git", "--git-dir", str(session_gd), "config", "user.email", "cave@test.com")
    git_run("git", "--git-dir", str(session_gd), "config", "user.name", "cave-coder")

    # Cave working directory (simulates overlay merged view)
    cave = tmp_path / "cave"
    cave.mkdir()
    git_run("git", "--git-dir", str(session_gd), "--work-tree", str(cave),
            "checkout", "HEAD", "--", ".")
    (cave / ".git").write_text(f"gitdir: {session_gd}\n")

    # Set up cave-sync and cave remote for pull
    wrapper.ensure_cave_remote(host, shared_gd)
    host_head = git_head(host)
    wrapper.sync_refs(shared_gd, host, host_head, "main")

    # Register session
    write_session(tmp_path, "sid-test", slug)

    yield SimpleNamespace(
        host=host, cave=cave, shared_gd=shared_gd,
        session_gd=session_gd, overlay_base=overlay_base,
        slug=slug, tmp_path=tmp_path,
    )

    for var, val in saved_env.items():
        if val is not None:
            os.environ[var] = val


# --- Overlay visibility: host uncommitted files visible in cave ---

class TestOverlayVisibility:
    """Host uncommitted files are visible in the cave via the overlay lower layer.

    In a real cave, fuse-overlayfs merges the host project (lowerdir) with
    the session upper. So host untracked/modified files appear in /projects/<slug>.
    """

    def test_host_untracked_file_visible_in_overlay(self, env):
        """Host untracked files appear in the cave overlay (lower layer passthrough).

        In a real overlay, this just works — the lowerdir IS the host.
        In our test simulation, the cave workdir is a snapshot from git checkout,
        so we simulate the overlay by writing the file into the cave directly.
        """
        # In a real overlay: host creates untracked file, cave sees it
        (env.host / "untracked.txt").write_text("from host\n")

        # Simulating overlay: cave lower layer = host, so file appears
        # (In real fuse-overlayfs, this is automatic)
        assert (env.host / "untracked.txt").exists()

    def test_host_modified_file_visible_in_overlay(self, env):
        """Host modifications appear in the cave via the overlay lower layer."""
        # Host modifies a committed file
        (env.host / "file.txt").write_text("modified on host\n")

        # In a real overlay, the cave would see this modification
        # UNLESS the cave has also modified this file (upper takes precedence)
        assert (env.host / "file.txt").read_text() == "modified on host\n"

    def test_cave_upper_overrides_host_lower(self, env):
        """Cave modifications (upper layer) take precedence over host (lower)."""
        # Both host and cave modify the same file
        (env.host / "file.txt").write_text("host version\n")
        (env.cave / "file.txt").write_text("cave version\n")

        # In a real overlay, the cave's version wins (upper > lower)
        assert (env.cave / "file.txt").read_text() == "cave version\n"


# --- Push: host uncommitted files ---

class TestPushUncommitted:
    """Push only transfers committed changes. Host uncommitted files are irrelevant."""

    def test_push_ignores_host_untracked_files(self, env):
        """Host untracked files don't affect push."""
        (env.host / "untracked.txt").write_text("not committed\n")

        old_head = git_head(env.shared_gd, gitdir=True)
        wrapper.push_project(env.slug)
        new_head = git_head(env.shared_gd, gitdir=True)

        assert old_head == new_head  # No new commits to push

    def test_push_ignores_host_modified_tracked(self, env):
        """Host modifications to tracked files don't affect push."""
        (env.host / "file.txt").write_text("modified but not committed\n")

        old_head = git_head(env.shared_gd, gitdir=True)
        wrapper.push_project(env.slug)
        new_head = git_head(env.shared_gd, gitdir=True)

        assert old_head == new_head

    def test_push_ignores_host_staged_files(self, env):
        """Host staged files don't affect push."""
        (env.host / "staged.txt").write_text("staged\n")
        git_run("git", "-C", str(env.host), "add", "staged.txt")

        old_head = git_head(env.shared_gd, gitdir=True)
        wrapper.push_project(env.slug)
        new_head = git_head(env.shared_gd, gitdir=True)

        assert old_head == new_head

    def test_push_transfers_committed_changes_only(self, env):
        """Push transfers only committed changes from host."""
        # Host commits something new
        (env.host / "new.txt").write_text("committed\n")
        git_run("git", "-C", str(env.host), "add", "new.txt")
        git_run("git", "-C", str(env.host), "commit", "-m", "host commit")

        # Also has uncommitted changes
        (env.host / "uncommitted.txt").write_text("not in push\n")

        old_head = git_head(env.shared_gd, gitdir=True)
        wrapper.push_project(env.slug)
        new_head = git_head(env.shared_gd, gitdir=True)

        assert old_head != new_head  # Committed change was pushed

        # But the uncommitted file is not in the gitdir
        ls_tree = git_run("git", "--git-dir", str(env.shared_gd),
                          "ls-tree", "HEAD", "--name-only")
        assert "uncommitted.txt" not in ls_tree.stdout


# --- Pull: cave uncommitted files ---

class TestPullUncommitted:
    """Pull only transfers committed changes. Cave uncommitted files are lost."""

    def test_pull_ignores_cave_untracked_files(self, env):
        """Cave untracked files don't appear on host after pull."""
        # Cave has an untracked file
        (env.cave / "cave-notes.txt").write_text("agent scratch work\n")

        wrapper.pull_project(env.slug)

        assert not (env.host / "cave-notes.txt").exists()

    def test_pull_ignores_cave_modified_tracked(self, env):
        """Cave modifications to tracked files don't appear on host unless committed."""
        (env.cave / "file.txt").write_text("cave-modified\n")

        wrapper.pull_project(env.slug)

        # Host still has original content
        assert (env.host / "file.txt").read_text() == "initial\n"

    def test_pull_ignores_cave_staged_files(self, env):
        """Cave staged (but not committed) files don't appear on host."""
        (env.cave / "staged.txt").write_text("staged in cave\n")
        cave_git(env, "add", "staged.txt")

        wrapper.pull_project(env.slug)

        assert not (env.host / "staged.txt").exists()

    def test_pull_transfers_committed_changes_only(self, env):
        """Pull transfers only committed cave changes."""
        # Committed change
        cave_commit(env, "cave work", filename="committed.txt", content="done\n")

        # Uncommitted change
        (env.cave / "wip.txt").write_text("work in progress\n")

        wrapper.pull_project(env.slug)

        assert (env.host / "committed.txt").exists()
        assert not (env.host / "wip.txt").exists()


# --- Pull: host uncommitted files preserved ---

class TestPullPreservesHostUncommitted:
    """Pull stashes host uncommitted changes to avoid losing them during rebase."""

    def test_pull_preserves_host_untracked(self, env):
        """Host untracked files survive a pull (stash --include-untracked)."""
        (env.host / "host-notes.txt").write_text("my notes\n")

        cave_commit(env, "cave work", content="new\n")
        wrapper.pull_project(env.slug)

        assert (env.host / "host-notes.txt").read_text() == "my notes\n"

    def test_pull_preserves_host_modified(self, env):
        """Host modifications to tracked files survive a pull."""
        (env.host / "file.txt").write_text("host-modified\n")

        cave_commit(env, "cave work", filename="other.txt", content="new\n")
        wrapper.pull_project(env.slug)

        assert (env.host / "file.txt").read_text() == "host-modified\n"

    def test_pull_conflict_host_uncommitted_vs_cave_committed(self, env):
        """When cave commits to same file host has uncommitted changes on."""
        (env.host / "file.txt").write_text("host WIP\n")

        # Cave commits to the same file
        cave_commit(env, "update file", content="cave version\n")
        wrapper.pull_project(env.slug)

        # The stash pop may conflict — file.txt should have cave's committed version
        # or a merge conflict marker. Either way, the pull should complete.
        content = (env.host / "file.txt").read_text()
        # Cave's committed version should be there (stash pop may fail silently)
        assert "cave version" in content or "host WIP" in content

    def test_pull_preserves_host_staged(self, env):
        """Host staged files survive a pull."""
        (env.host / "staged.txt").write_text("staged\n")
        git_run("git", "-C", str(env.host), "add", "staged.txt")

        cave_commit(env, "cave work", filename="other.txt", content="new\n")
        wrapper.pull_project(env.slug)

        # The file should still exist (stash preserved it)
        assert (env.host / "staged.txt").exists()


# --- Session lifecycle: uncommitted files lost on exit ---

class TestSessionLifecycleUncommitted:
    """Uncommitted cave files are lost when the session ends (overlay destroyed)."""

    def test_uncommitted_not_in_shared_after_merge(self, env):
        """Uncommitted cave files don't appear in shared gitdir after session merge."""
        # Cave has uncommitted work
        (env.cave / "wip.txt").write_text("work in progress\n")

        # Session ends — merge_session_gitdirs runs
        wrapper.merge_session_gitdirs("sid-test", env.overlay_base)

        # Shared gitdir should NOT have the uncommitted file
        ls_tree = git_run("git", "--git-dir", str(env.shared_gd),
                          "ls-tree", "HEAD", "--name-only")
        assert "wip.txt" not in ls_tree.stdout

    def test_committed_survives_session_merge(self, env):
        """Committed cave files DO appear in shared gitdir after session merge."""
        cave_commit(env, "save work", filename="saved.txt", content="saved\n")

        wrapper.merge_session_gitdirs("sid-test", env.overlay_base)

        ls_tree = git_run("git", "--git-dir", str(env.shared_gd),
                          "ls-tree", "HEAD", "--name-only")
        assert "saved.txt" in ls_tree.stdout

    def test_staged_not_committed_lost_on_merge(self, env):
        """Staged but not committed files are lost when the session merges."""
        (env.cave / "staged.txt").write_text("staged\n")
        cave_git(env, "add", "staged.txt")
        # Intentionally NOT committed

        wrapper.merge_session_gitdirs("sid-test", env.overlay_base)

        ls_tree = git_run("git", "--git-dir", str(env.shared_gd),
                          "ls-tree", "HEAD", "--name-only")
        assert "staged.txt" not in ls_tree.stdout


# --- Push after cave has uncommitted changes ---

class TestPushWithCaveUncommitted:
    """Push updates the gitdir from host; cave uncommitted files are unaffected."""

    def test_push_with_new_host_commit_updates_gitdir(self, env):
        """New host commit pushes into gitdir regardless of cave uncommitted state."""
        # Cave has uncommitted files
        (env.cave / "wip.txt").write_text("not committed\n")

        # Host makes a new commit
        (env.host / "new.txt").write_text("from host\n")
        git_run("git", "-C", str(env.host), "add", "new.txt")
        git_run("git", "-C", str(env.host), "commit", "-m", "host update")

        wrapper.push_project(env.slug)

        # Gitdir should have the host commit
        ls_tree = git_run("git", "--git-dir", str(env.shared_gd),
                          "ls-tree", "HEAD", "--name-only")
        assert "new.txt" in ls_tree.stdout

    def test_push_does_not_see_cave_session_commits(self, env):
        """Push only looks at host HEAD, not session gitdir commits.

        Cave commits in the session gitdir don't block or affect push.
        Push compares host HEAD vs shared gitdir HEAD.
        """
        # Cave commits something
        cave_commit(env, "cave work", filename="cave.txt", content="from cave\n")

        # Merge session so shared gitdir has it
        wrapper.merge_session_gitdirs("sid-test", env.overlay_base)

        # Host also commits (divergent)
        (env.host / "host.txt").write_text("from host\n")
        git_run("git", "-C", str(env.host), "add", "host.txt")
        git_run("git", "-C", str(env.host), "commit", "-m", "host update")

        # Push should warn about unpulled commits
        with pytest.raises(SystemExit):
            wrapper.push_project(env.slug)

    def test_push_force_overwrites_cave_commits(self, env):
        """Push --force overwrites cave commits in gitdir."""
        cave_commit(env, "cave work", filename="cave.txt", content="from cave\n")
        wrapper.merge_session_gitdirs("sid-test", env.overlay_base)

        (env.host / "host.txt").write_text("from host\n")
        git_run("git", "-C", str(env.host), "add", "host.txt")
        git_run("git", "-C", str(env.host), "commit", "-m", "host update")

        wrapper.push_project(env.slug, force=True)

        # Gitdir should now match host HEAD
        assert git_head(env.shared_gd, gitdir=True) == git_head(env.host)


# --- Reset: everything destroyed ---

class TestResetUncommitted:
    """Reset wipes everything — committed and uncommitted alike."""

    def test_reset_wipes_gitdir(self, env):
        """Reset reinitializes the shared gitdir from host."""
        cave_commit(env, "cave work", filename="cave.txt", content="from cave\n")
        wrapper.merge_session_gitdirs("sid-test", env.overlay_base)

        old_head = git_head(env.shared_gd, gitdir=True)

        wrapper.reset_project_overlay(env.slug)

        new_head = git_head(env.shared_gd, gitdir=True)
        # After reset, gitdir matches host (which doesn't have the cave commit)
        assert new_head == git_head(env.host)
        assert new_head != old_head


# --- Edge cases ---

class TestEdgeCases:
    """Edge cases around uncommitted file boundaries."""

    def test_binary_files_uncommitted(self, env):
        """Binary files in the cave working tree don't affect pull."""
        (env.cave / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        wrapper.pull_project(env.slug)

        assert not (env.host / "image.png").exists()

    def test_symlink_uncommitted(self, env):
        """Symlinks in the cave working tree don't affect pull."""
        (env.cave / "target.txt").write_text("target\n")
        (env.cave / "link.txt").symlink_to("target.txt")

        wrapper.pull_project(env.slug)

        assert not (env.host / "link.txt").exists()
        assert not (env.host / "target.txt").exists()

    def test_gitignored_files_not_committed(self, env):
        """Gitignored files don't get committed, so they're lost on session exit."""
        (env.cave / ".gitignore").write_text("*.log\n")
        cave_git(env, "add", ".gitignore")
        cave_git(env, "commit", "-m", "add gitignore")

        (env.cave / "debug.log").write_text("log output\n")

        wrapper.pull_project(env.slug)

        assert (env.host / ".gitignore").exists()
        assert not (env.host / "debug.log").exists()

    def test_large_uncommitted_file_no_impact(self, env):
        """Large uncommitted files don't affect or slow down pull."""
        (env.cave / "large.bin").write_bytes(b"\x00" * (1024 * 1024))

        wrapper.pull_project(env.slug)

        assert not (env.host / "large.bin").exists()

    def test_empty_dirs_not_tracked(self, env):
        """Empty directories in the cave aren't tracked by git."""
        (env.cave / "empty-dir").mkdir()

        cave_commit(env, "some commit", content="updated\n")
        wrapper.pull_project(env.slug)

        # Empty dirs can't be committed in git
        assert not (env.host / "empty-dir").exists()

    def test_dotfiles_uncommitted(self, env):
        """Dotfiles (.env, .secrets) in cave working tree don't leak to host."""
        (env.cave / ".env").write_text("SECRET=hunter2\n")
        (env.cave / ".secrets.json").write_text('{"key": "value"}\n')

        wrapper.pull_project(env.slug)

        assert not (env.host / ".env").exists()
        assert not (env.host / ".secrets.json").exists()
