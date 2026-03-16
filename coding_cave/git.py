"""Git helpers for cave gitdir operations."""
import os
import shutil
import subprocess
import sys

from . import _state


SAFE_GIT_OPTS = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "core.fsmonitor=",
    "-c", "diff.external=",
    "-c", "core.sshCommand=echo blocked",
    "-c", "protocol.ext.allow=never",
    "-c", "core.attributesFile=/dev/null",
]


def git(gitdir, *args, **kwargs):
    """Run git against a cave gitdir with config-based code execution neutralized.

    The gitdir is writable from inside the cave, so its .git/config could be
    poisoned with core.pager, core.fsmonitor, diff.external, core.sshCommand,
    or filter drivers. This wrapper overrides all such options.
    """
    cmd = ["git", "--git-dir", str(gitdir)] + SAFE_GIT_OPTS + list(args)
    env = kwargs.pop("env", None) or os.environ.copy()
    env["GIT_ATTR_NOSYSTEM"] = "1"
    return subprocess.run(cmd, env=env, **kwargs)


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


def finalize_sync(gitdir, project_dir):
    """Refresh host's cave remote refs and mirror origin refs into gitdir."""
    host_git = ["git", "-C", str(project_dir)]
    subprocess.run(host_git + ["fetch", "cave", "--prune"], capture_output=True)
    mirror_origin_refs(gitdir, project_dir)


def sync_refs(gitdir, project_dir, host_head, branch):
    git(gitdir, "fetch", str(project_dir), "HEAD",
        check=True, capture_output=True)
    git(gitdir, "update-ref",
         f"refs/heads/{branch}", host_head,
        check=True)
    git(gitdir, "update-ref",
         f"refs/remotes/host/{branch}", host_head)
    finalize_sync(gitdir, project_dir)


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


def rebase_plumbing(git_cmd, onto_sha, merge_base, source_sha, *,
                    on_conflict=None, on_skip=None):
    """Rebase commits (merge_base..source_sha) onto onto_sha using plumbing.

    Uses merge-tree + commit-tree so it never touches the working tree.
    Returns new tip SHA, or None on conflict/error.

    git_cmd: command prefix list, e.g. ["git", "-C", path] or
             ["git", "--git-dir", path] + SAFE_GIT_OPTS.
    on_conflict(commit): called on merge conflict. Return True to skip
        the commit (cherry-pick equivalent already applied), False/None
        to abort the rebase.
    on_skip(commit, subject): called when a commit produces an empty tree
        (already applied). Informational only.
    """
    if not merge_base:
        return None

    result = subprocess.run(
        git_cmd + ["rev-list", "--reverse", f"{merge_base}..{source_sha}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    commits = [c for c in result.stdout.strip().splitlines() if c]
    if not commits:
        return None

    parent = onto_sha
    for commit in commits:
        cp = subprocess.run(
            git_cmd + ["rev-parse", f"{commit}^"],
            capture_output=True, text=True,
        )
        if cp.returncode != 0:
            return None
        commit_parent = cp.stdout.strip()

        merge = subprocess.run(
            git_cmd + ["merge-tree", "--write-tree",
                f"--merge-base={commit_parent}", parent, commit],
            capture_output=True, text=True,
        )
        if merge.returncode != 0:
            if on_conflict and on_conflict(commit):
                continue
            return None
        merged_tree = merge.stdout.strip().splitlines()[0]

        # Skip empty commits (tree unchanged), e.g. after an amend of a
        # previously-applied commit.
        parent_tree = subprocess.run(
            git_cmd + ["rev-parse", f"{parent}^{{tree}}"],
            capture_output=True, text=True,
        )
        if (parent_tree.returncode == 0
                and parent_tree.stdout.strip() == merged_tree):
            if on_skip:
                msg = subprocess.run(
                    git_cmd + ["log", "-1", "--format=%s", commit],
                    capture_output=True, text=True,
                )
                subject = (msg.stdout.strip()
                           if msg.returncode == 0 else commit)
                on_skip(commit, subject)
            continue

        # Preserve original author and committer info
        info = subprocess.run(
            git_cmd + ["log", "-1",
                "--format=%an%n%ae%n%aI%n%cn%n%ce%n%cI%n%B", commit],
            capture_output=True, text=True,
        )
        if info.returncode != 0:
            return None
        lines = info.stdout.rstrip("\n").split("\n", 6)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": lines[0],
            "GIT_AUTHOR_EMAIL": lines[1],
            "GIT_AUTHOR_DATE": lines[2],
            "GIT_COMMITTER_NAME": lines[3],
            "GIT_COMMITTER_EMAIL": lines[4],
            "GIT_COMMITTER_DATE": lines[5],
        }
        message = lines[6] if len(lines) > 6 else ""
        ct = subprocess.run(
            git_cmd + ["commit-tree", merged_tree, "-p", parent,
                "-m", message],
            capture_output=True, text=True, env=env,
        )
        if ct.returncode != 0:
            return None
        parent = ct.stdout.strip()

    return parent
