"""Argument parsing and main entry point."""
import argparse
import sys

from . import _state, commands


def build_parser():
    parser = argparse.ArgumentParser(
        prog="coding-cave",
        description="Sandboxed Claude Code agent sessions with Nix environments. Use -- to pass extra arguments to claude.",
    )
    parser.add_argument("--version", action="version", version=f"coding-cave {_state.VERSION}")
    parser.add_argument("--gpu", action="store_true", help="enable GPU access")
    parser.add_argument("-c", "--continue", action="store_true", dest="continue_session", help="continue most recent conversation")
    parser.add_argument("-r", "--resume", nargs="?", const=True, default=None, metavar="SESSION_ID", help="resume a conversation by session ID, or interactive picker")
    parser.add_argument("-p", "--projects", default=None, metavar="SLUGS", help="projects to mount: 'all', or comma-separated slugs (default: CWD project only)")
    sub = parser.add_subparsers(dest="command")

    p_agent = sub.add_parser("agent", help="start a new agent session")
    p_agent.add_argument("-c", "--continue", action="store_true", dest="continue_session", help="continue most recent conversation")
    p_agent.add_argument("-r", "--resume", nargs="?", const=True, default=None, metavar="SESSION_ID", help="resume a conversation by session ID, or interactive picker")
    p_agent.add_argument("-p", "--projects", default=None, metavar="SLUGS", help="projects to mount: 'all', or comma-separated slugs (default: CWD project only)")
    p_shell = sub.add_parser("shell", help="start a shell or join a running session")
    p_shell.add_argument("-p", "--projects", default=None, metavar="SLUGS", help="projects to mount: 'all', or comma-separated slugs (default: CWD project only)")

    p_add = sub.add_parser("add", help="register a project")
    p_add.add_argument("dir", nargs="?", help="directory to add (default: CWD)")

    p_status = sub.add_parser("status", help="show projects and running sessions")
    p_status.add_argument("-a", "--all", action="store_true", help="show all projects")

    p_pull = sub.add_parser("pull", help="pull commits from cave")
    p_pull.add_argument("project", nargs="?", help="project slug (default: CWD project)")
    p_pull.add_argument("--force", action="store_true", help="adopt cave's history")

    p_push = sub.add_parser("push", help="push host commits into cave")
    p_push.add_argument("project", nargs="?", help="project slug (default: CWD project)")
    p_push.add_argument("--force", action="store_true", help="skip unpulled commit check")

    p_reset = sub.add_parser("reset", help="wipe overlay + gitdir to match host")
    p_reset.add_argument("project", nargs="?", help="project slug (default: CWD project)")

    p_remove = sub.add_parser("remove", help="unregister a project and delete data")
    p_remove.add_argument("project", nargs="?", help="project slug (default: CWD project)")

    p_cp = sub.add_parser("cp", help="copy files to/from a running cave session")
    p_cp.add_argument("-R", "-r", "--recursive", action="store_true", help="copy directories recursively")
    p_cp.add_argument("-f", "--force", action="store_true", help="overwrite existing files without prompting")
    p_cp.add_argument("paths", nargs="+", help="source(s) and destination (slug:path, cave:path, slug:~/path, slug:/abs/path)")

    sub.add_parser("update-plugins", help="(deprecated) alias for 'flake update'")

    p_flake = sub.add_parser("flake", help="manage cave.nix flake")
    flake_sub = p_flake.add_subparsers(dest="flake_command")
    p_flake_update = flake_sub.add_parser("update", help="update flake inputs")
    p_flake_update.add_argument("inputs", nargs="*", help="specific inputs to update (default: all)")

    p_move = sub.add_parser("move", help="update a project's source path")
    p_move.add_argument("old_slug", help="current project name")
    p_move.add_argument("new_path", help="new directory path")

    return parser


def main():
    # Split on -- to extract claude passthrough args
    argv = sys.argv[1:]
    claude_args = []
    if "--" in argv:
        idx = argv.index("--")
        claude_args = argv[idx + 1:]
        argv = argv[:idx]

    parser = build_parser()
    parsed = parser.parse_args(argv)

    if parsed.command is None:
        commands.cmd_agent([], gpu=parsed.gpu, continue_session=parsed.continue_session, resume=parsed.resume, claude_args=claude_args, projects_flag=parsed.projects)
    elif parsed.command == "agent":
        commands.cmd_agent([], gpu=parsed.gpu, continue_session=parsed.continue_session, resume=parsed.resume, claude_args=claude_args, projects_flag=parsed.projects)
    elif parsed.command == "shell":
        commands.cmd_shell(claude_args, gpu=parsed.gpu, projects_flag=parsed.projects)
    elif parsed.command == "add":
        commands.cmd_add(directory=parsed.dir)
    elif parsed.command == "status":
        commands.cmd_status(show_all=parsed.all)
    elif parsed.command == "pull":
        commands.cmd_pull(slug=parsed.project, force=parsed.force)
    elif parsed.command == "push":
        commands.cmd_push(slug=parsed.project, force=parsed.force)
    elif parsed.command == "reset":
        commands.cmd_reset(slug=parsed.project)
    elif parsed.command == "remove":
        commands.cmd_remove(slug=parsed.project)
    elif parsed.command == "cp":
        if len(parsed.paths) < 2:
            print("Usage: coding-cave cp <src>... <dest>", file=sys.stderr)
            sys.exit(1)
        commands.cmd_cp(parsed.paths[:-1], parsed.paths[-1], recursive=parsed.recursive, force=parsed.force)
    elif parsed.command == "update-plugins":
        commands.cmd_flake_update([])
    elif parsed.command == "flake":
        if parsed.flake_command == "update":
            commands.cmd_flake_update(parsed.inputs)
        else:
            print("Usage: coding-cave flake update [INPUT...]", file=sys.stderr)
            sys.exit(1)
    elif parsed.command == "move":
        commands.cmd_move(parsed.old_slug, parsed.new_path)
