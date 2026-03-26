"""Thin wrappers around git subprocess calls."""

import shlex
import subprocess
import sys


def git(*args, cwd=None, check=True, capture=False, echo=True):
    """Run a git command. Returns CompletedProcess."""
    cmd = ["git"] + list(args)
    if echo:
        print(f"\n+ {shlex.join(cmd)}", file=sys.stderr)
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=capture,
        text=True,
    )


def git_output(*args, cwd=None) -> str:
    """Run a git command and return stdout, stripped."""
    r = git(*args, cwd=cwd, capture=True, echo=False)
    return r.stdout.strip()


def current_branch(cwd=None) -> str:
    return git_output("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)


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


def short_sha(ref: str, cwd=None) -> str:
    return git_output("rev-parse", "--short", ref, cwd=cwd)


def commit_oneline(range_spec: str, exclude: list[str] | None = None, cwd=None) -> list[str]:
    """Return commit lines in --oneline format for a range (newest first)."""
    args = ["log", "--oneline", range_spec]
    if exclude:
        args += [f"^{r}" for r in exclude]
    out = git_output(*args, cwd=cwd)
    if not out:
        return []
    return out.splitlines()
