"""Tests for sync helpers: sync_refs, history rewrite detection."""
import subprocess
from pathlib import Path

import pytest

import wrapper


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
