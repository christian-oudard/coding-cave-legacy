"""Pull/push synchronization between host and cave."""
import subprocess
import sys
from pathlib import Path

from . import _state, projects, sessions, git as git_mod, gitdir


def sync_live_sessions(slug):
    """Forward refs from running sessions into the shared gitdir.

    Uses forward_session_refs which handles alternates rewrite/restore
    internally and never force-updates refs.
    """
    shared_gitdir = _state.projects_dir() / slug / "gitdir"
    if not shared_gitdir.is_dir():
        return
    for session in sessions.list_sessions():
        sid = session["sid"]
        overlay_base = _state.cave_data() / "session-overlays" / sid
        session_gitdir = overlay_base / f"gitdir-{slug}"
        if session_gitdir.is_dir():
            gitdir.forward_session_refs(slug, session_gitdir, shared_gitdir)


def _parse_log(git_cmd, range_spec):
    """Parse 'git log' output into [(hash, tree, subject), ...]."""
    result = subprocess.run(
        git_cmd + ["log", "--format=%h %T %s", range_spec],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    entries = []
    for line in result.stdout.rstrip().splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2:
            entries.append((parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return entries


def _print_rewrite_summary(old_entries, new_entries):
    """Print summary of amended vs new commits."""
    old_by_subject = {}
    old_by_tree = {}
    for h, tree, subj in old_entries:
        old_by_subject[subj] = h
        old_by_tree[tree] = (h, subj)
    for h, tree, subj in new_entries:
        old_h = old_by_subject.pop(subj, None)
        if old_h and old_h != h:
            print(f"  amend {old_h} -> {h} {subj}", file=sys.stderr)
            old_by_tree.pop(tree, None)
        elif old_h:
            print(f"  {h} {subj}", file=sys.stderr)
            old_by_tree.pop(tree, None)
        elif tree in old_by_tree:
            old_h, old_subj = old_by_tree.pop(tree)
            print(f"  amend {old_h} -> {h} {subj}", file=sys.stderr)
        else:
            print(f"  + {h} {subj}", file=sys.stderr)


def pull_project(slug, force=False):
    """Pull committed changes from cave gitdir into the host repo."""
    loaded_projects = projects.load_projects()
    if slug not in loaded_projects:
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    proj_state = _state.projects_dir() / slug
    project_dir = Path(loaded_projects[slug])
    gitdir_path = proj_state / "gitdir"

    if not gitdir_path.exists():
        print(f"No gitdir for {slug} -- run 'coding-cave reset' first.", file=sys.stderr)
        sys.exit(1)

    sync_live_sessions(slug)
    git_mod.ensure_cave_remote(project_dir, gitdir_path)

    host_git = ["git", "-C", str(project_dir)] + git_mod.SAFE_GIT_OPTS

    # Clean up stale rebase state
    host_git_dir = project_dir / ".git"
    if (host_git_dir / "rebase-merge").is_dir() or (host_git_dir / "rebase-apply").is_dir():
        print(f"Aborting stale rebase in {slug}.", file=sys.stderr)
        subprocess.run(host_git + ["rebase", "--abort"], capture_output=True)
    cur_branch = subprocess.run(
        host_git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    if cur_branch.returncode == 0 and cur_branch.stdout.strip() == "_cave_pull":
        branches = subprocess.run(
            host_git + ["branch", "--format=%(refname:short)"],
            capture_output=True, text=True,
        )
        for b in branches.stdout.strip().splitlines():
            if b != "_cave_pull":
                subprocess.run(host_git + ["checkout", b], capture_output=True)
                break
    subprocess.run(host_git + ["branch", "-D", "_cave_pull"], capture_output=True)

    branch_result = subprocess.run(
        host_git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    gitdir_head_result = git_mod.git(gitdir_path, "rev-parse", "HEAD",
        capture_output=True, text=True)

    if gitdir_head_result.returncode != 0:
        print(f"No commits in cave gitdir for {slug} -- nothing to pull.", file=sys.stderr)
        return

    gitdir_head = gitdir_head_result.stdout.strip()

    # Host repo has no commits yet -- fetch and create branch from cave
    if branch_result.returncode != 0:
        branch = git_mod.git(gitdir_path, "rev-parse", "--abbrev-ref", "HEAD",
            capture_output=True, text=True).stdout.strip()
        if not branch or branch == "HEAD":
            branch = "main"
        subprocess.run(host_git + ["fetch", str(gitdir_path), "HEAD"], check=True, capture_output=True)
        subprocess.run(host_git + ["update-ref", f"refs/heads/{branch}", gitdir_head], check=True)
        subprocess.run(host_git + ["symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
        subprocess.run(host_git + ["checkout", branch], capture_output=True)
        # Re-author all commits with host identity
        subprocess.run(host_git + [
            "rebase", "--root",
            "--exec", "git -c core.hooksPath=/dev/null -c core.pager=cat -c core.fsmonitor= commit --amend --reset-author --allow-empty --no-edit --quiet",
        ], capture_output=True)
        print(f"Pulled {slug} (initialized from cave):", file=sys.stderr)
        subprocess.run(host_git + ["log", "--oneline"], check=False)
        host_head = subprocess.run(
            host_git + ["rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        git_mod.sync_refs(gitdir_path, project_dir, host_head, branch)
        return

    branch = branch_result.stdout.strip()
    host_head = subprocess.run(
        host_git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if host_head == gitdir_head:
        print(f"No new commits to pull from {slug}.", file=sys.stderr)
        git_mod.update_cave_sync(gitdir_path, project_dir, host_head, branch=branch)
        return

    # Force with matching trees: adopt cave's history
    if force:
        gitdir_tree = git_mod.git(gitdir_path, "rev-parse", "HEAD^{tree}",
            capture_output=True, text=True)
        host_tree = subprocess.run(
            host_git + ["rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True,
        )
        if (gitdir_tree.returncode == 0 and host_tree.returncode == 0 and
                gitdir_tree.stdout.strip() == host_tree.stdout.strip()):
            subprocess.run(host_git + ["fetch", str(gitdir_path), "HEAD"], capture_output=True)
            subprocess.run(
                host_git + ["update-ref", f"refs/heads/{branch}", gitdir_head],
                check=True,
            )
            print(f"Force-updated {slug} (history rewritten, tree unchanged):", file=sys.stderr)
            print(f"  {host_head[:8]} -> {gitdir_head[:8]}", file=sys.stderr)
            git_mod.update_cave_sync(gitdir_path, project_dir, gitdir_head, branch=branch)
            return

    fetch = subprocess.run(host_git + ["fetch", str(gitdir_path), "HEAD"], capture_output=True)
    if fetch.returncode != 0:
        print(f"Fetch from gitdir failed for {slug}.", file=sys.stderr)
        sys.exit(1)

    cave_sync = git_mod.git(gitdir_path, "rev-parse", "refs/cave-sync",
        capture_output=True, text=True)
    cave_sync_hash = cave_sync.stdout.strip() if cave_sync.returncode == 0 else None

    merge_base_result = subprocess.run(
        host_git + ["merge-base", "HEAD", "FETCH_HEAD"],
        capture_output=True, text=True,
    )
    if merge_base_result.returncode != 0:
        host_tree = subprocess.run(
            host_git + ["rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        merge_base = git_mod.find_tree_merge_base(host_git, host_tree)
        if merge_base is None:
            print(f"No common ancestor for {slug} -- skipping.", file=sys.stderr)
            return
    else:
        merge_base = merge_base_result.stdout.strip()

    count = subprocess.run(
        host_git + ["rev-list", "--count", f"{merge_base}..FETCH_HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if count == "0":
        print(f"No commits to pull from {slug}.", file=sys.stderr)
        return

    # Detect history rewrite: cave amended/squashed commits since last sync.
    if (cave_sync_hash and cave_sync_hash == host_head
            and merge_base != host_head):
        if not force:
            host_count = subprocess.run(
                host_git + ["rev-list", "--count", f"{merge_base}..HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            print(f"Cave rewrote history for {slug} ({host_count} -> {count} commit(s)).", file=sys.stderr)
            subprocess.run(host_git + ["fetch", str(gitdir_path), "HEAD"], capture_output=True)
            old = _parse_log(host_git, f"{merge_base}..HEAD")
            new = _parse_log(host_git, f"{merge_base}..FETCH_HEAD")
            _print_rewrite_summary(old, new)
            print(f"Run 'coding-cave pull --force' to adopt.", file=sys.stderr)
            return

        host_count = subprocess.run(
            host_git + ["rev-list", "--count", f"{merge_base}..HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        stash_result = subprocess.run(
            host_git + ["stash", "--include-untracked"],
            capture_output=True, text=True,
        )
        stashed = stash_result.returncode == 0 and "No local changes" not in stash_result.stdout

        old_entries = _parse_log(host_git, f"{merge_base}..HEAD")

        subprocess.run(host_git + ["reset", "--hard", merge_base], check=True, capture_output=True)
        subprocess.run(host_git + ["branch", "-f", "_cave_pull", "FETCH_HEAD"], check=True, capture_output=True)
        subprocess.run(host_git + [
            "rebase", "--onto", branch, merge_base, "_cave_pull",
            "--no-reapply-cherry-picks",
            "--exec", "git -c core.hooksPath=/dev/null -c core.pager=cat -c core.fsmonitor= commit --amend --reset-author --allow-empty --no-edit --quiet",
        ], check=True, capture_output=True)
        subprocess.run(host_git + ["checkout", branch], check=True, capture_output=True)
        subprocess.run(host_git + ["merge", "--ff-only", "_cave_pull"], check=True, capture_output=True)
        subprocess.run(host_git + ["branch", "-D", "_cave_pull"], capture_output=True)

        if stashed:
            subprocess.run(host_git + ["stash", "pop"], capture_output=True)

        print(f"Pulled {slug} ({host_count} -> {count} commit(s)):", file=sys.stderr)
        new_entries = _parse_log(host_git, f"{merge_base}..HEAD")
        _print_rewrite_summary(old_entries, new_entries)

        host_head = subprocess.run(
            host_git + ["rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        git_mod.sync_refs(gitdir_path, project_dir, host_head, branch)
        return

    pre_head = subprocess.run(
        host_git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    stash_result = subprocess.run(
        host_git + ["stash", "--include-untracked"],
        capture_output=True, text=True,
    )
    stashed = stash_result.returncode == 0 and "No local changes" not in stash_result.stdout

    subprocess.run(host_git + ["branch", "-f", "_cave_pull", "FETCH_HEAD"], check=True, capture_output=True)
    result = subprocess.run(host_git + [
        "rebase", "--onto", branch, merge_base, "_cave_pull",
        "--no-reapply-cherry-picks",
        "--exec", "git -c core.hooksPath=/dev/null -c core.pager=cat -c core.fsmonitor= commit --amend --reset-author --allow-empty --no-edit --quiet",
    ], capture_output=True)
    if result.returncode != 0:
        rebase_in_progress = ((host_git_dir / "rebase-merge").is_dir() or
                              (host_git_dir / "rebase-apply").is_dir())
        if rebase_in_progress:
            print(f"Rebase conflicts for {slug}. Resolve, then:", file=sys.stderr)
            print(f"  git -C {project_dir} rebase --continue", file=sys.stderr)
            print(f"  git -C {project_dir} checkout {branch}", file=sys.stderr)
            print(f"  git -C {project_dir} merge --ff-only _cave_pull", file=sys.stderr)
            if stashed:
                print(f"  git -C {project_dir} stash pop", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Rebase failed for {slug}.", file=sys.stderr)
            if result.stderr:
                print(result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr, file=sys.stderr, end="")
            if result.stdout:
                print(result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout, file=sys.stderr, end="")
            subprocess.run(host_git + ["rebase", "--abort"], capture_output=True)
            subprocess.run(host_git + ["branch", "-D", "_cave_pull"], capture_output=True)
            if stashed:
                subprocess.run(host_git + ["stash", "pop"], capture_output=True)
            sys.exit(1)

    subprocess.run(host_git + ["checkout", branch], capture_output=True, check=True)
    subprocess.run(host_git + ["merge", "--ff-only", "_cave_pull"], capture_output=True, check=True)
    subprocess.run(host_git + ["branch", "-D", "_cave_pull"], capture_output=True)

    if stashed:
        subprocess.run(host_git + ["stash", "pop"], capture_output=True)

    actual = subprocess.run(
        host_git + ["rev-list", "--count", f"{pre_head}..HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if actual != "0":
        print(f"Pulled {actual} commit(s) from {slug}:", file=sys.stderr)
        log = subprocess.run(
            host_git + ["log", "--oneline", f"{pre_head}..HEAD"],
            capture_output=True, text=True,
        )
        if log.returncode == 0 and log.stdout.strip():
            print(log.stdout.rstrip(), file=sys.stderr)

    host_head = subprocess.run(
        host_git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    git_mod.sync_refs(gitdir_path, project_dir, host_head, branch)


def push_project(slug, force=False):
    """Push host commits into the cave gitdir."""
    loaded_projects = projects.load_projects()
    if slug not in loaded_projects:
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    proj_state = _state.projects_dir() / slug
    project_dir = Path(loaded_projects[slug])
    gitdir_path = proj_state / "gitdir"

    if not gitdir_path.exists():
        print(f"No gitdir for {slug} -- run 'coding-cave reset' first.", file=sys.stderr)
        sys.exit(1)

    sync_live_sessions(slug)
    git_mod.ensure_cave_remote(project_dir, gitdir_path)

    branch_result = subprocess.run(
        ["git", "-C", str(project_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"

    host_head = subprocess.run(
        ["git", "-C", str(project_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    cave_head = git_mod.git(gitdir_path, "rev-parse", "HEAD",
        capture_output=True, text=True)
    if cave_head.returncode == 0 and cave_head.stdout.strip() == host_head:
        print(f"No commits to push to {slug}.", file=sys.stderr)
        git_mod.update_cave_sync(gitdir_path, project_dir, host_head, branch=branch)
        return

    # Check for unpulled cave commits
    if not force and cave_head.returncode == 0:
        cave_head_hash = cave_head.stdout.strip()
        git_mod.git(gitdir_path, "fetch", str(project_dir), "HEAD",
            capture_output=True)
        merge_base = git_mod.git(gitdir_path, "merge-base",
             cave_head_hash, host_head,
            capture_output=True, text=True)
        if merge_base.returncode == 0:
            unpulled = git_mod.git(gitdir_path, "rev-list", "--count",
                 f"{merge_base.stdout.strip()}..{cave_head_hash}",
                capture_output=True, text=True)
            if unpulled.returncode == 0 and unpulled.stdout.strip() != "0":
                count = unpulled.stdout.strip()
                print(f"Cave has {count} unpulled commit(s) that would be lost:", file=sys.stderr)
                git_mod.git(gitdir_path, "log", "--oneline",
                     f"{merge_base.stdout.strip()}..{cave_head_hash}")
                print(f"Run 'coding-cave pull' first, or use 'coding-cave push --force'.", file=sys.stderr)
                sys.exit(1)

    git_mod.sync_refs(gitdir_path, project_dir, host_head, branch)
    print(f"Pushed to {slug} ({host_head[:8]}).", file=sys.stderr)


def find_unsynced_project(slug):
    """Check if a cave-originated project exists in cave home."""
    cave_repo = _state.cave_home() / "unsynced-projects" / slug
    if cave_repo.is_dir() and (cave_repo / ".git").is_dir():
        return cave_repo
    return None


def pull_cave_project(slug):
    """Pull a cave-originated project to the host for the first time."""
    cave_repo = find_unsynced_project(slug)
    if cave_repo is None:
        print(f"No cave-originated project named {slug}.", file=sys.stderr)
        sys.exit(1)

    host_dir = Path.cwd().resolve() / slug
    if host_dir.exists():
        print(f"Directory already exists: {host_dir}", file=sys.stderr)
        sys.exit(1)

    try:
        answer = input(f"Clone cave project to {host_dir}? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "y":
        print("Aborted.", file=sys.stderr)
        sys.exit(0)

    subprocess.run(
        ["git", "clone", str(cave_repo), str(host_dir)],
        check=True,
    )
    # Remove the clone's origin (points to cave home, not useful)
    subprocess.run(
        ["git", "-C", str(host_dir), "remote", "remove", "origin"],
        capture_output=True,
    )

    # Register as a normal project with gitdir + overlay
    projects.ensure_project(host_dir)
    print(f"Pulled cave project {slug} to {host_dir}", file=sys.stderr)
    print(f"Next cave session will mount it at /projects/{slug}.", file=sys.stderr)


def project_sync_status(slug, source):
    project_dir = Path(source)
    proj_state = _state.projects_dir() / slug
    gitdir_path = proj_state / "gitdir"

    if not project_dir.is_dir():
        return "missing"

    if not gitdir_path.exists():
        return "no gitdir"

    host_head = subprocess.run(
        ["git", "-C", str(project_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    cave_head = git_mod.git(gitdir_path, "rev-parse", "HEAD",
        capture_output=True, text=True)
    if host_head.returncode != 0 and cave_head.returncode != 0:
        return None  # both empty, nothing to compare
    if host_head.returncode != 0 or cave_head.returncode != 0:
        return "?"

    host_h = host_head.stdout.strip()
    cave_h = cave_head.stdout.strip()

    tags = []

    if host_h != cave_h:
        git_mod.git(gitdir_path, "fetch", str(project_dir), "HEAD",
            capture_output=True)
        mb = git_mod.git(gitdir_path, "merge-base", cave_h, host_h,
            capture_output=True, text=True)
        if mb.returncode != 0:
            tags.append("diverged")
        else:
            base = mb.stdout.strip()
            to_pull = git_mod.git(gitdir_path, "rev-list", "--count",
                 f"{base}..{cave_h}",
                capture_output=True, text=True)
            to_push = git_mod.git(gitdir_path, "rev-list", "--count",
                 f"{base}..{host_h}",
                capture_output=True, text=True)
            n_pull = to_pull.stdout.strip() if to_pull.returncode == 0 else "0"
            n_push = to_push.stdout.strip() if to_push.returncode == 0 else "0"
            if n_pull != "0" and n_push != "0":
                tags.append(f"{n_pull} to pull, {n_push} to push")
            elif n_pull != "0":
                tags.append(f"{n_pull} to pull")
            elif n_push != "0":
                tags.append(f"{n_push} to push")

    return ", ".join(tags) if tags else ""
