#!/usr/bin/env python3
"""
coding-cave wrapper: reads config, sets up git worktrees, launches sandbox.

Subcommands:
    codingcave run <project-dir> [task...]   - create worktree, launch claude with task
    codingcave shell [project-dir]           - interactive shell, optionally in project
    codingcave cleanup <project-dir> <branch> - remove worktree and branch
    codingcave                               - bare sandbox (no workspace)
"""
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

SANDBOX_BIN = "@SANDBOX_BIN@"


def load_global_config():
    config_path = Path.home() / ".config" / "coding-cave" / "config.toml"
    if config_path.exists():
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    return {}


def load_project_config(project_dir):
    config_path = Path(project_dir) / ".claude-sandbox.toml"
    if config_path.exists():
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    return {}


def setup_env(global_config, project_config):
    """Set environment variables from config files."""
    token = global_config.get("credentials", {}).get("claude-oauth-token")
    if token:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token

    git_cfg = global_config.get("git", {})
    if "name" in git_cfg:
        os.environ.setdefault("GIT_AUTHOR_NAME", git_cfg["name"])
        os.environ.setdefault("GIT_COMMITTER_NAME", git_cfg["name"])
    if "email" in git_cfg:
        os.environ.setdefault("GIT_AUTHOR_EMAIL", git_cfg["email"])
        os.environ.setdefault("GIT_COMMITTER_EMAIL", git_cfg["email"])

    for key, val in project_config.get("environment", {}).items():
        os.environ[key] = str(val)


def slugify(text, max_len=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


def create_worktree(project_dir, task=None, branch_prefix="agent"):
    """Create a git worktree for agent work. Returns (worktree_path, branch_name)."""
    project_dir = Path(project_dir).resolve()

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if task:
        branch_name = f"{branch_prefix}/{slugify(task)}-{timestamp}"
    else:
        branch_name = f"{branch_prefix}/{timestamp}"

    worktree_base = Path.home() / ".local" / "share" / "coding-cave" / "worktrees"
    worktree_path = worktree_base / project_dir.name / branch_name.replace("/", "-")
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "-C", str(project_dir), "worktree", "add", "-b", branch_name, str(worktree_path)],
        check=True,
    )
    return worktree_path, branch_name


def remove_worktree(project_dir, branch_name):
    """Remove a git worktree and its branch."""
    project_dir = Path(project_dir).resolve()

    # Find worktree path from branch
    result = subprocess.run(
        ["git", "-C", str(project_dir), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=True,
    )

    worktree_path = None
    current_path = None
    for line in result.stdout.split("\n"):
        if line.startswith("worktree "):
            current_path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/") and line.endswith(branch_name):
            worktree_path = current_path

    if worktree_path:
        subprocess.run(
            ["git", "-C", str(project_dir), "worktree", "remove", "--force", worktree_path],
            check=True,
        )

    subprocess.run(
        ["git", "-C", str(project_dir), "branch", "-D", branch_name],
        check=True,
    )


def launch_sandbox():
    os.execv(SANDBOX_BIN, ["codingcave"])


def cmd_run(args):
    if not args:
        print("Usage: codingcave run <project-dir> [task...]", file=sys.stderr)
        sys.exit(1)

    project_dir = Path(args[0]).resolve()
    task = " ".join(args[1:]) if len(args) > 1 else None

    global_config = load_global_config()
    project_config = load_project_config(project_dir)
    setup_env(global_config, project_config)

    prefix = project_config.get("git", {}).get("branch-prefix", "agent")
    worktree_path, branch_name = create_worktree(project_dir, task, prefix)
    print(f"Worktree: {worktree_path}", file=sys.stderr)
    print(f"Branch:   {branch_name}", file=sys.stderr)

    os.environ["CODING_CAVE_WORKSPACE"] = str(worktree_path)
    if task:
        os.environ["CODING_CAVE_TASK"] = task

    launch_sandbox()


def cmd_shell(args):
    project_dir = Path(args[0]).resolve() if args else None

    global_config = load_global_config()
    project_config = load_project_config(project_dir) if project_dir else {}
    setup_env(global_config, project_config)

    os.environ["CODING_CAVE_SHELL"] = "1"
    if project_dir:
        os.environ["CODING_CAVE_WORKSPACE"] = str(project_dir)

    launch_sandbox()


def cmd_cleanup(args):
    if len(args) < 2:
        print("Usage: codingcave cleanup <project-dir> <branch>", file=sys.stderr)
        sys.exit(1)

    remove_worktree(args[0], args[1])
    print(f"Removed: {args[1]}", file=sys.stderr)


def main():
    if len(sys.argv) < 2:
        global_config = load_global_config()
        setup_env(global_config, {})
        launch_sandbox()

    commands = {"run": cmd_run, "shell": cmd_shell, "cleanup": cmd_cleanup}
    cmd = sys.argv[1]
    if cmd in commands:
        commands[cmd](sys.argv[2:])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Commands: run, shell, cleanup", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
