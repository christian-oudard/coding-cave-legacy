#!/usr/bin/env python3
"""Git wrapper: syncs the cave's work to the parent's shared remote.

Installed at $HOME/bin/git inside the cave, shadowing the real git.
Pushes whenever the current branch's tip changes (the necessary and
sufficient condition for needing to sync). On new commits (forward
motion), also fetches and rebases onto the upstream base branch so
agent work stays on top of approved changes.
"""

import os
import shutil
import subprocess
import sys


def _find_real_git():
    """Find the real git by searching PATH with our directory removed."""
    wrapper = os.path.realpath(sys.argv[0])
    wrapper_dir = os.path.dirname(wrapper)
    path = os.environ.get("PATH", "")
    filtered = [p for p in path.split(":") if p != wrapper_dir]
    return shutil.which("git", path=":".join(filtered))


def _git(*args, cwd=None):
    """Run git, return CompletedProcess. Captures output, never echoes."""
    return subprocess.run(
        [REAL_GIT, *args], cwd=cwd,
        capture_output=True, text=True,
    )


def _git_out(*args, cwd=None):
    """Run git and return stripped stdout, or None on failure."""
    r = _git(*args, cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else None


def _branch_tip(cwd=None):
    """Return (branch_name, sha) or (None, None)."""
    branch = _git_out("symbolic-ref", "--short", "HEAD", cwd=cwd)
    if not branch:
        return None, None
    sha = _git_out("rev-parse", f"refs/heads/{branch}", cwd=cwd)
    return branch, sha


def _parse_chdir(args):
    """Extract the -C directory from git global args."""
    prev = None
    for arg in args:
        if prev == "-C":
            return arg
        prev = arg
    return None


def _is_ancestor(old, new, cwd=None):
    """True if old is an ancestor of new (forward motion)."""
    return _git("merge-base", "--is-ancestor", old, new, cwd=cwd).returncode == 0


def _rebase_in_progress(cwd=None):
    git_dir = _git_out("rev-parse", "--git-dir", cwd=cwd)
    if not git_dir:
        return False
    from pathlib import Path
    d = Path(git_dir) if os.path.isabs(git_dir) else Path(cwd or ".") / git_dir
    return (d / "rebase-merge").exists() or (d / "rebase-apply").exists()


def _sync(args):
    agent_name = os.environ.get("CODING_CAVE_AGENT_NAME", "")
    agent_id = agent_name.rsplit("@", 1)[-1] if "@" in agent_name else ""
    if not agent_id or not agent_id.isdigit():
        return

    cwd = _parse_chdir(args)

    # Snapshot branch tip before the command.
    _, sha_before = _branch_tip(cwd)

    # Run the real git command.
    r = subprocess.run([REAL_GIT, *args], env=dict(os.environ))
    if r.returncode != 0:
        sys.exit(r.returncode)

    # Check if the branch tip changed.
    branch, sha_after = _branch_tip(cwd)
    if not branch or branch.startswith("agent-") or sha_before == sha_after:
        sys.exit(0)

    # Prevent recursion in our own git calls.
    os.environ["_CAV_GIT_SYNC"] = "1"

    # New commits (forward motion): fetch + rebase to stay on top of base.
    if sha_before and _is_ancestor(sha_before, sha_after, cwd):
        if not _rebase_in_progress(cwd):
            _git("fetch", "host-cave", cwd=cwd)
            if _git_out("rev-parse", f"host-cave/{branch}", cwd=cwd):
                r = _git("rebase", f"host-cave/{branch}", cwd=cwd)
                if r.returncode != 0:
                    _git("rebase", "--abort", cwd=cwd)
                    print(
                        f"WARNING: rebase onto host-cave/{branch}"
                        f" failed (conflicts).",
                        file=sys.stderr,
                    )
            # Re-read tip after rebase.
            _, sha_after = _branch_tip(cwd)

    # Push the updated branch.
    remote = f"agent-{agent_id}/{branch}"
    r = _git(
        "push", "--force", "host-cave",
        f"{sha_after}:refs/heads/{remote}", cwd=cwd,
    )
    if r.returncode != 0:
        print(f"cav-sync: push {branch} -> host-cave/{remote} failed:",
              file=sys.stderr)
        msg = (r.stderr or r.stdout or "").rstrip()
        for line in msg.splitlines():
            print(f"  {line}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    # Recursion guard.
    if os.environ.get("_CAV_GIT_SYNC"):
        REAL_GIT = os.environ.get("_CAV_REAL_GIT") or _find_real_git()
        os.execvp(REAL_GIT, [REAL_GIT, *sys.argv[1:]])

    REAL_GIT = _find_real_git()
    os.environ["_CAV_REAL_GIT"] = REAL_GIT

    _sync(sys.argv[1:])
