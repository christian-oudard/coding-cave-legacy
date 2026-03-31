"""Prune orphaned local agent branches."""

from pathlib import Path

from . import config, git


def prune_branches(base: Path):
    git.git("fetch", "--prune", config.SHARED_REMOTE, cwd=base, **config.v())

    local_out = git.git_output(
        "for-each-ref",
        "--format=%(refname:short)\t%(upstream:short)\t%(upstream:track)",
        "refs/heads/", cwd=base)
    if not local_out:
        print("No orphaned agent branches.")
        return

    cave_prefix = f"{config.SHARED_REMOTE}/"
    orphans = []
    for line in local_out.splitlines():
        parts = line.split("\t")
        branch = parts[0]
        upstream = parts[1] if len(parts) > 1 else ""
        track = parts[2] if len(parts) > 2 else ""
        if not config.parse_agent_branch(branch):
            continue
        if upstream and not upstream.startswith(cave_prefix):
            raise SystemExit(
                f"Branch '{branch}' tracks '{upstream}', not cave. "
                "Refusing to prune. Remove it manually if intended."
            )
        if upstream and "[gone]" not in track:
            continue
        orphans.append(branch)

    if not orphans:
        print("No orphaned agent branches.")
        return

    current = git.current_branch(cwd=base)
    if current in orphans:
        raise SystemExit(
            f"Currently on orphaned branch '{current}'. Switch to main first."
        )

    print("Orphaned local agent branches:")
    for b in orphans:
        print(f"  {b}")
    try:
        answer = input(f"\nDelete {len(orphans)} branches? [Y/n] ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\nAborted.")
    if answer.strip().lower() in ("n", "no"):
        raise SystemExit("Aborted.")

    for b in orphans:
        git.git("branch", "-D", b, cwd=base, **config.v())
