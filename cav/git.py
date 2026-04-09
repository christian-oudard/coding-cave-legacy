"""Thin wrappers around git subprocess calls."""

import shlex
import subprocess
import sys


def git(*args, cwd=None, check=True, capture=False, echo=True):
    """Run a git command. Returns CompletedProcess."""
    cmd = ["git"] + list(args)
    if echo:
        print(f"+ {shlex.join(cmd)}", file=sys.stderr)
    r = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=True,
    )
    if check and r.returncode != 0:
        if capture and r.stderr:
            print(r.stderr, end="", file=sys.stderr)
        raise subprocess.CalledProcessError(
            r.returncode, cmd, output=r.stdout, stderr=r.stderr)
    return r


def git_output(*args, cwd=None) -> str:
    """Run a git command and return stdout, stripped."""
    r = git(*args, cwd=cwd, capture=True, echo=False)
    return r.stdout.strip()


def current_branch(cwd=None) -> str:
    """Return current branch name. Works on orphan branches (no commits yet)."""
    try:
        return git_output("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    except subprocess.CalledProcessError:
        # Orphan branch: HEAD is a symbolic ref but has no commit yet
        return git_output("symbolic-ref", "--short", "HEAD", cwd=cwd)


def repo_root(cwd=None) -> str:
    return git_output("rev-parse", "--show-toplevel", cwd=cwd)


def remote_branches(remote: str, cwd=None) -> list[str]:
    """List branches on a remote (after fetching). Returns short names."""
    lines = git_output("branch", "-r", "--list", f"{remote}/*", cwd=cwd)
    if not lines:
        return []
    branches = []
    for line in lines.splitlines():
        ref = line.strip()
        if " -> " in ref:
            continue
        # Strip remote prefix: "cave/develop" -> "develop"
        prefix = f"{remote}/"
        if ref.startswith(prefix):
            branches.append(ref[len(prefix):])
    return branches


def commits_ahead(branch_a: str, branch_b: str, exclude: list[str] | None = None, cwd=None) -> int:
    """Number of commits in branch_a not in branch_b (and not in any exclude refs)."""
    args = ["rev-list", "--count", branch_a, f"^{branch_b}"]
    if exclude:
        args += [f"^{r}" for r in exclude]
    out = git_output(*args, cwd=cwd)
    return int(out)



def has_merge_base(ref_a: str, ref_b: str, cwd=None) -> bool:
    """Check if two refs share a common ancestor."""
    r = git("merge-base", ref_a, ref_b, cwd=cwd, check=False, capture=True, echo=False)
    return r.returncode == 0


def cherry_undelivered(upstream: str, head: str, cwd=None) -> list[str]:
    """Return oneline list of commits in head not yet in upstream (by patch-id).

    Uses git cherry to filter out commits whose patches are already applied,
    even under different SHAs (e.g. after rebase/cherry-pick).
    """
    out = git_output("cherry", "-v", upstream, head, cwd=cwd)
    if not out:
        return []
    lines = []
    for line in out.splitlines():
        if line.startswith("+ "):
            # Format: "+ <sha> <subject>" -> "<short_sha> <subject>"
            parts = line[2:].split(" ", 1)
            sha_short = parts[0][:7]
            subject = parts[1] if len(parts) > 1 else ""
            lines.append(f"{sha_short} {subject}")
    return list(reversed(lines))



def commit_timestamp(ref: str, cwd=None) -> float | None:
    """Return the committer timestamp (unix epoch) of a ref, or None."""
    try:
        out = git_output("log", "-1", "--format=%ct", ref, cwd=cwd)
        return float(out) if out else None
    except Exception:
        return None


def update_agent_tip(shared, branch: str):
    """Record agent-tip so the pre-receive hook can protect user commits."""
    sha = git_output("rev-parse", f"refs/heads/{branch}", cwd=shared)
    git("update-ref", f"refs/cav/agent-tip/{branch}", sha,
        cwd=shared, echo=False, check=False, capture=True)


def commit_oneline(range_spec: str, exclude: list[str] | None = None, cwd=None) -> list[str]:
    """Return commit lines in --oneline format for a range (newest first)."""
    args = ["log", "--oneline", range_spec]
    if exclude:
        args += [f"^{r}" for r in exclude]
    out = git_output(*args, cwd=cwd)
    if not out:
        return []
    return out.splitlines()
