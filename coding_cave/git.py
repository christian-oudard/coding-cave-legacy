"""Git helpers for cave gitdir operations."""
import os
import shutil
import subprocess

from . import _state


SAFE_GIT_OPTS = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "core.fsmonitor=",
    "-c", "diff.external=",
    "-c", "core.sshCommand=echo blocked",
    "-c", "protocol.ext.allow=never",
]


def git(gitdir, *args, **kwargs):
    """Run git against a cave gitdir with config-based code execution neutralized.

    The gitdir is writable from inside the cave, so its .git/config could be
    poisoned with core.pager, core.fsmonitor, diff.external, core.sshCommand,
    or filter drivers. This wrapper overrides all such options.
    """
    cmd = ["git", "--git-dir", str(gitdir)] + SAFE_GIT_OPTS + list(args)
    return subprocess.run(cmd, **kwargs)


def initialize_gitdir(slug, project_dir):
    """Create gitdir from host .git if it doesn't exist."""
    proj_state = _state.projects_dir() / slug
    gitdir = proj_state / "gitdir"
    if gitdir.exists() or not (project_dir / ".git").is_dir():
        return
    tmpclone = proj_state / "_tmpclone"
    subprocess.run(
        ["git", "clone", "--no-checkout", "--local", "--quiet",
         str(project_dir), str(tmpclone)],
        check=True, capture_output=True,
    )
    (tmpclone / ".git").rename(gitdir)
    tmpclone.rmdir()
    subprocess.run(
        ["git", "-C", str(gitdir), "remote", "remove", "origin"],
        capture_output=True,
    )
    # Disable hooks to prevent cross-session code execution
    git(gitdir, "config", "core.hooksPath", "/dev/null",
        capture_output=True)
    hooks_dir = gitdir / "hooks"
    if hooks_dir.exists():
        shutil.rmtree(hooks_dir)
    head = git(gitdir, "rev-parse", "HEAD",
        capture_output=True, text=True)
    if head.returncode == 0:
        git(gitdir, "update-ref",
             "refs/cave-sync", head.stdout.strip())
        branch = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        if branch.returncode == 0:
            git(gitdir, "update-ref",
                 f"refs/remotes/host/{branch.stdout.strip()}", head.stdout.strip())
    ensure_cave_remote(project_dir, gitdir)
    mirror_origin_refs(gitdir, project_dir)


def ensure_cave_remote(project_dir, gitdir):
    host_git = ["git", "-C", str(project_dir)]
    existing = subprocess.run(
        host_git + ["remote", "get-url", "cave"],
        capture_output=True, text=True,
    )
    if existing.returncode == 0:
        if existing.stdout.strip() != str(gitdir):
            subprocess.run(host_git + ["remote", "set-url", "cave", str(gitdir)], capture_output=True)
    else:
        subprocess.run(host_git + ["remote", "add", "cave", str(gitdir)], capture_output=True)


def mirror_origin_refs(gitdir, project_dir):
    """Copy refs/remotes/origin/* from host project into the cave gitdir."""
    result = subprocess.run(
        ["git", "-C", str(project_dir), "for-each-ref",
         "--format=%(refname) %(objectname)", "refs/remotes/origin/"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        ref, sha = line.split()
        git(gitdir, "update-ref", ref, sha,
            capture_output=True)


def update_cave_sync(gitdir, project_dir, commit, branch=None):
    git(gitdir, "update-ref", "refs/cave-sync", commit)
    subprocess.run(["git", "-C", str(project_dir), "fetch", "cave"], capture_output=True)
    if branch:
        git(gitdir, "update-ref",
             f"refs/remotes/host/{branch}", commit)
    mirror_origin_refs(gitdir, project_dir)


def sync_refs(gitdir, project_dir, host_head, branch):
    git(gitdir, "fetch", str(project_dir), "HEAD",
        check=True, capture_output=True)
    git(gitdir, "update-ref",
         f"refs/heads/{branch}", host_head,
        check=True)
    update_cave_sync(gitdir, project_dir, host_head, branch=branch)


def find_tree_merge_base(git_cmd, host_tree):
    """Find the cave commit whose tree matches the host HEAD tree."""
    cave_log = subprocess.run(
        git_cmd + ["log", "--format=%H %T", "FETCH_HEAD"],
        capture_output=True, text=True,
    )
    if cave_log.returncode != 0:
        return None
    for line in cave_log.stdout.strip().splitlines():
        commit_hash, tree_hash = line.split()
        if tree_hash == host_tree:
            return commit_hash
    return None
