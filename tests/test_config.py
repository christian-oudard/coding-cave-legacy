"""Tests for config module: identity forwarding and GPG detection."""
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coding_cave.config import _detect_gpg_agent_socket, _git_config, setup_env
from coding_cave.sync import _needs_reauthoring
from coding_cave import _state


class TestGitConfig:
    def test_reads_existing_config(self, tmp_path):
        """_git_config reads a git global config value."""
        # This test relies on the actual host git config, which may
        # not have user.name set. Use a fake gitconfig instead.
        gitconfig = tmp_path / ".gitconfig"
        gitconfig.write_text("[user]\n\tname = TestName\n\temail = test@test.com\n")
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            assert _git_config("user.name") == "TestName"
            assert _git_config("user.email") == "test@test.com"

    def test_returns_none_for_missing(self, tmp_path):
        """_git_config returns None for unset keys."""
        gitconfig = tmp_path / ".gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n")
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            assert _git_config("user.signingkey") is None


class TestGpgDetection:
    def test_returns_none_when_no_gpg(self, tmp_path):
        """_detect_gpg_agent_socket returns None when no socket exists."""
        with patch.dict(os.environ, {"GNUPGHOME": str(tmp_path)}):
            assert _detect_gpg_agent_socket() is None

    def test_finds_socket_in_gnupghome(self, tmp_path):
        """_detect_gpg_agent_socket finds socket in GNUPGHOME."""
        sock = tmp_path / "S.gpg-agent"
        sock.touch()  # not a real socket, but exists
        with patch.dict(os.environ, {"GNUPGHOME": str(tmp_path)}):
            assert _detect_gpg_agent_socket() == str(sock)


class TestNeedsReauthoring:
    def test_same_email_no_reauthoring(self, tmp_path):
        """Cave commits with host email don't need re-authoring."""
        _state.set_cave_data(tmp_path)

        # Create a host repo
        host = tmp_path / "host"
        host.mkdir()
        subprocess.run(["git", "-C", str(host), "init", "-b", "main"],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", str(host), "config", "user.email", "me@host.com"],
                       capture_output=True)
        subprocess.run(["git", "-C", str(host), "config", "user.name", "Me"],
                       capture_output=True)
        (host / "f.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(host), "add", "."],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", str(host), "commit", "-m", "init"],
                       capture_output=True, check=True)

        # Create gitdir with SAME email as host
        proj = _state.projects_dir() / "test"
        proj.mkdir(parents=True)
        tmpclone = tmp_path / "_clone"
        subprocess.run(["git", "clone", "--no-checkout", "--local",
                        str(host), str(tmpclone)],
                       capture_output=True, check=True)
        gitdir = proj / "gitdir"
        (tmpclone / ".git").rename(gitdir)
        tmpclone.rmdir()
        subprocess.run(["git", "--git-dir", str(gitdir), "remote", "remove",
                        "origin"], capture_output=True)
        subprocess.run(["git", "--git-dir", str(gitdir), "read-tree", "HEAD"],
                       capture_output=True, check=True)

        # Make a cave commit with the HOST email (simulates host identity forwarding)
        workdir = tmp_path / "cave-work"
        workdir.mkdir()
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree",
                        str(workdir), "checkout", "HEAD", "--", "."],
                       capture_output=True, check=True)
        (workdir / "cave.txt").write_text("cave work\n")
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "Me", "GIT_AUTHOR_EMAIL": "me@host.com",
               "GIT_COMMITTER_NAME": "Me", "GIT_COMMITTER_EMAIL": "me@host.com"}
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree",
                        str(workdir), "add", "cave.txt"],
                       capture_output=True, check=True, env=env)
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree",
                        str(workdir), "commit", "-m", "cave commit"],
                       capture_output=True, check=True, env=env)

        assert not _needs_reauthoring(gitdir, host)

    def test_different_email_needs_reauthoring(self, tmp_path):
        """Cave commits with throwaway email need re-authoring."""
        _state.set_cave_data(tmp_path)

        host = tmp_path / "host"
        host.mkdir()
        subprocess.run(["git", "-C", str(host), "init", "-b", "main"],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", str(host), "config", "user.email", "me@host.com"],
                       capture_output=True)
        subprocess.run(["git", "-C", str(host), "config", "user.name", "Me"],
                       capture_output=True)
        (host / "f.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(host), "add", "."],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", str(host), "commit", "-m", "init"],
                       capture_output=True, check=True)

        proj = _state.projects_dir() / "test"
        proj.mkdir(parents=True)
        tmpclone = tmp_path / "_clone"
        subprocess.run(["git", "clone", "--no-checkout", "--local",
                        str(host), str(tmpclone)],
                       capture_output=True, check=True)
        gitdir = proj / "gitdir"
        (tmpclone / ".git").rename(gitdir)
        tmpclone.rmdir()
        subprocess.run(["git", "--git-dir", str(gitdir), "remote", "remove",
                        "origin"], capture_output=True)
        subprocess.run(["git", "--git-dir", str(gitdir), "read-tree", "HEAD"],
                       capture_output=True, check=True)

        # Make a cave commit with THROWAWAY identity
        workdir = tmp_path / "cave-work"
        workdir.mkdir()
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree",
                        str(workdir), "checkout", "HEAD", "--", "."],
                       capture_output=True, check=True)
        (workdir / "cave.txt").write_text("cave work\n")
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "coding-cave",
               "GIT_AUTHOR_EMAIL": "coding-cave@noreply",
               "GIT_COMMITTER_NAME": "coding-cave",
               "GIT_COMMITTER_EMAIL": "coding-cave@noreply"}
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree",
                        str(workdir), "add", "cave.txt"],
                       capture_output=True, check=True, env=env)
        subprocess.run(["git", "--git-dir", str(gitdir), "--work-tree",
                        str(workdir), "commit", "-m", "cave commit"],
                       capture_output=True, check=True, env=env)

        assert _needs_reauthoring(gitdir, host)
