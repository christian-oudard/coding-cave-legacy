"""Pull/push synchronization between host and cave."""
import os
import subprocess
import sys
from pathlib import Path

from . import _state, projects, sessions, git as git_mod, gitdir


def _needs_reauthoring(gitdir_path, project_dir):
    """Check if cave commits need re-authoring on pull.

    Returns False when the cave is already using the host's git identity
    (GPG agent forwarding mode), True when the cave uses the throwaway
    cave-coder identity and commits need to be rewritten.
    """
    # Get the author of the latest cave commit
    cave_author = git_mod.git(gitdir_path, "log", "-1", "--format=%ae",
        capture_output=True, text=True)
    if cave_author.returncode != 0 or not cave_author.stdout.strip():
        return True  # can't determine, default to re-authoring
    cave_email = cave_author.stdout.strip()

    # Get the host's configured email (local config, then global)
    host_email = subprocess.run(
        ["git", "-C", str(project_dir), "config", "user.email"],
        capture_output=True, text=True,
    )
    if host_email.returncode != 0 or not host_email.stdout.strip():
        # Try global config
        host_email = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True, text=True,
        )
    if host_email.returncode != 0 or not host_email.stdout.strip():
        return True  # can't determine host identity

    return cave_email != host_email.stdout.strip()


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
    # FF-merge session branches to main so pull/push can see them
    gitdir.ff_session_branches(shared_gitdir)


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


def _get_branches(gitdir_path, *, bare=False):
    """Return {branch_name: sha} for all branches in a git repo.

    If bare=True, uses --git-dir; otherwise uses -C.
    """
    if bare:
        result = git_mod.git(gitdir_path, "for-each-ref",
                     "--format=%(refname:short) %(objectname)", "refs/heads/",
                     capture_output=True, text=True)
    else:
        result = subprocess.run(
            ["git", "-C", str(gitdir_path), "for-each-ref",
             "--format=%(refname:short) %(objectname)", "refs/heads/"],
            capture_output=True, text=True,
        )
    if result.returncode != 0:
        return {}
    branches = {}
    for line in result.stdout.strip().splitlines():
        if line:
            name, sha = line.split()
            branches[name] = sha
    return branches


def _get_cave_branches(gitdir_path):
    """Return {branch_name: sha} for all branches in cave gitdir."""
    return _get_branches(gitdir_path, bare=True)


def _rebase_plumbing(host_git, onto_sha, merge_base, cave_sha,
                     gitdir_path, project_dir):
    """Rebase commits (merge_base..cave_sha) onto onto_sha using plumbing.

    Uses merge-tree + commit-tree so it never touches the working tree.
    Re-authors commits if the cave used a throwaway identity.
    Returns new tip SHA, or None on conflict/error.
    """
    commits_result = subprocess.run(
        host_git + ["rev-list", "--reverse", f"{merge_base}..{cave_sha}"],
        capture_output=True, text=True,
    )
    if commits_result.returncode != 0:
        return None
    commits = [c for c in commits_result.stdout.strip().splitlines() if c]
    if not commits:
        return None

    reauthor = _needs_reauthoring(gitdir_path, project_dir)
    host_name = host_email = None
    if reauthor:
        host_name = subprocess.run(
            ["git", "-C", str(project_dir), "config", "user.name"],
            capture_output=True, text=True,
        ).stdout.strip()
        host_email = subprocess.run(
            ["git", "-C", str(project_dir), "config", "user.email"],
            capture_output=True, text=True,
        ).stdout.strip()

    parent = onto_sha
    for commit in commits:
        cp = subprocess.run(
            host_git + ["rev-parse", f"{commit}^"],
            capture_output=True, text=True,
        )
        if cp.returncode != 0:
            return None
        commit_parent = cp.stdout.strip()

        merge = subprocess.run(
            host_git + ["merge-tree", "--write-tree",
                f"--merge-base={commit_parent}", parent, commit],
            capture_output=True, text=True,
        )
        if merge.returncode != 0:
            return None
        merged_tree = merge.stdout.strip().splitlines()[0]

        info = subprocess.run(
            host_git + ["log", "-1",
                "--format=%an%n%ae%n%aI%n%cn%n%ce%n%cI%n%B", commit],
            capture_output=True, text=True,
        )
        if info.returncode != 0:
            return None
        lines = info.stdout.rstrip("\n").split("\n", 6)
        if reauthor:
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": host_name or lines[0],
                "GIT_AUTHOR_EMAIL": host_email or lines[1],
                "GIT_AUTHOR_DATE": lines[2],
                "GIT_COMMITTER_NAME": host_name or lines[3],
                "GIT_COMMITTER_EMAIL": host_email or lines[4],
                "GIT_COMMITTER_DATE": lines[5],
            }
        else:
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
        result = subprocess.run(
            host_git + ["commit-tree", merged_tree, "-p", parent,
                "-m", message],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            return None
        parent = result.stdout.strip()

    return parent


def _sync_branch(gitdir_path, project_dir, host_sha, branch):
    """Sync a single branch ref back to the cave gitdir."""
    git_mod.git(gitdir_path, "fetch", str(project_dir), host_sha,
        capture_output=True)
    git_mod.git(gitdir_path, "update-ref",
        f"refs/heads/{branch}", host_sha, capture_output=True)
    git_mod.git(gitdir_path, "update-ref",
        f"refs/remotes/host/{branch}", host_sha, capture_output=True)


def _last_synced_sha(gitdir_path, branch):
    """Get the last-synced host SHA for a branch."""
    result = git_mod.git(gitdir_path, "rev-parse",
        f"refs/remotes/host/{branch}",
        capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _pull_branch_current(slug, project_dir, gitdir_path, branch, cave_sha,
                         host_git, force):
    """Pull cave commits to the host's currently checked-out branch.

    Uses checkout-based rebase with stash.  Returns new host HEAD SHA,
    or None if no commits were pulled.  Calls sys.exit(1) on conflict.
    """
    host_git_dir = project_dir / ".git"

    host_head = subprocess.run(
        host_git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if host_head == cave_sha:
        return None

    # Force with matching trees: adopt cave's history
    if force:
        gitdir_tree = git_mod.git(gitdir_path, "rev-parse",
            f"{cave_sha}^{{tree}}", capture_output=True, text=True)
        host_tree = subprocess.run(
            host_git + ["rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True,
        )
        if (gitdir_tree.returncode == 0 and host_tree.returncode == 0 and
                gitdir_tree.stdout.strip() == host_tree.stdout.strip()):
            subprocess.run(host_git + ["fetch", str(gitdir_path), cave_sha],
                           capture_output=True)
            subprocess.run(
                host_git + ["update-ref", f"refs/heads/{branch}", cave_sha],
                check=True,
            )
            print(f"Force-updated {slug} (history rewritten, tree unchanged):",
                  file=sys.stderr)
            print(f"  {host_head[:8]} -> {cave_sha[:8]}", file=sys.stderr)
            return cave_sha

    fetch = subprocess.run(
        host_git + ["fetch", str(gitdir_path), cave_sha], capture_output=True)
    if fetch.returncode != 0:
        print(f"Fetch from gitdir failed for {slug}.", file=sys.stderr)
        return None

    last_synced = _last_synced_sha(gitdir_path, branch)

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
            print(f"No common ancestor for {slug}/{branch} -- skipping.",
                  file=sys.stderr)
            return None
    else:
        merge_base = merge_base_result.stdout.strip()

    count = subprocess.run(
        host_git + ["rev-list", "--count", f"{merge_base}..FETCH_HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if count == "0":
        return None

    # Detect history rewrite: cave amended/squashed commits since last sync.
    if (last_synced and last_synced == host_head
            and merge_base != host_head):
        if not force:
            host_count = subprocess.run(
                host_git + ["rev-list", "--count", f"{merge_base}..HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            print(f"Cave rewrote history for {slug} ({host_count} -> {count} commit(s)).", file=sys.stderr)
            subprocess.run(host_git + ["fetch", str(gitdir_path), cave_sha],
                           capture_output=True)
            old = _parse_log(host_git, f"{merge_base}..HEAD")
            new = _parse_log(host_git, f"{merge_base}..FETCH_HEAD")
            _print_rewrite_summary(old, new)
            print(f"Run 'coding-cave pull --force' to adopt.", file=sys.stderr)
            return None

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
        rebase_cmd = host_git + [
            "rebase", "--onto", branch, merge_base, "_cave_pull",
            "--no-reapply-cherry-picks",
        ]
        if _needs_reauthoring(gitdir_path, project_dir):
            rebase_cmd += [
                "--exec", "git -c core.hooksPath=/dev/null -c core.pager=cat -c core.fsmonitor= commit --amend --reset-author --allow-empty --no-edit --quiet",
            ]
        subprocess.run(rebase_cmd, check=True, capture_output=True)
        subprocess.run(host_git + ["checkout", branch], check=True, capture_output=True)
        subprocess.run(host_git + ["merge", "--ff-only", "_cave_pull"], check=True, capture_output=True)
        subprocess.run(host_git + ["branch", "-D", "_cave_pull"], capture_output=True)

        if stashed:
            subprocess.run(host_git + ["stash", "pop"], capture_output=True)

        print(f"Pulled {slug} ({host_count} -> {count} commit(s)):", file=sys.stderr)
        new_entries = _parse_log(host_git, f"{merge_base}..HEAD")
        _print_rewrite_summary(old_entries, new_entries)

        return subprocess.run(
            host_git + ["rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    # Normal pull
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
    rebase_cmd = host_git + [
        "rebase", "--onto", branch, merge_base, "_cave_pull",
        "--no-reapply-cherry-picks",
    ]
    if _needs_reauthoring(gitdir_path, project_dir):
        rebase_cmd += [
            "--exec", "git -c core.hooksPath=/dev/null -c core.pager=cat -c core.fsmonitor= commit --amend --reset-author --allow-empty --no-edit --quiet",
        ]
    result = subprocess.run(rebase_cmd, capture_output=True)
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

    new_head = subprocess.run(
        host_git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return new_head if new_head != pre_head else None


def _pull_branch_other(slug, project_dir, gitdir_path, branch, cave_sha,
                       host_git):
    """Pull a non-current branch using plumbing (no working tree changes).

    Never force-updates — history rewrites on non-current branches are
    skipped with a warning.  Use --force only for the current branch.

    Returns new host SHA for the branch, or None if no pull happened.
    """
    host_sha_result = subprocess.run(
        host_git + ["rev-parse", f"refs/heads/{branch}"],
        capture_output=True, text=True,
    )
    if host_sha_result.returncode != 0:
        return None  # branch doesn't exist on host
    host_sha = host_sha_result.stdout.strip()

    if host_sha == cave_sha:
        return None

    # Fetch cave commits into host object store
    fetch = subprocess.run(
        host_git + ["fetch", str(gitdir_path), cave_sha],
        capture_output=True,
    )
    if fetch.returncode != 0:
        return None

    merge_base_result = subprocess.run(
        host_git + ["merge-base", host_sha, cave_sha],
        capture_output=True, text=True,
    )
    if merge_base_result.returncode != 0:
        return None
    merge_base = merge_base_result.stdout.strip()

    count_result = subprocess.run(
        host_git + ["rev-list", "--count", f"{merge_base}..{cave_sha}"],
        capture_output=True, text=True,
    )
    if count_result.returncode != 0 or count_result.stdout.strip() == "0":
        return None

    # History rewrite detection — never force non-current branches
    last_synced = _last_synced_sha(gitdir_path, branch)
    if (last_synced and last_synced == host_sha
            and merge_base != host_sha):
        count = count_result.stdout.strip()
        host_count = subprocess.run(
            host_git + ["rev-list", "--count",
                f"{merge_base}..{host_sha}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        print(f"Cave rewrote history for {slug}/{branch} "
              f"({host_count} -> {count} commit(s)). "
              f"Check out {branch} and use --force to adopt.",
              file=sys.stderr)
        return None

    # Normal pull
    if (merge_base == host_sha
            and not _needs_reauthoring(gitdir_path, project_dir)):
        # True fast-forward, no re-authoring needed
        new_sha = cave_sha
    else:
        # Rebase (handles both ff-with-reauthor and diverged cases)
        new_sha = _rebase_plumbing(
            host_git, host_sha, merge_base, cave_sha,
            gitdir_path, project_dir,
        )
        if new_sha is None:
            print(f"Rebase conflicts for {slug}/{branch} -- skipping.",
                  file=sys.stderr)
            return None

    subprocess.run(
        host_git + ["update-ref", f"refs/heads/{branch}", new_sha],
        check=True, capture_output=True,
    )
    count = subprocess.run(
        host_git + ["rev-list", "--count", f"{host_sha}..{new_sha}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if count != "0":
        print(f"Pulled {count} commit(s) to {slug}/{branch}.", file=sys.stderr)
    return new_sha


def pull_project(slug, force=False):
    """Pull committed changes from cave gitdir into the host repo.

    Iterates all branches in the cave gitdir and pulls each one that has
    new commits to the corresponding host branch.
    """
    loaded_projects = projects.load_projects()
    if slug not in loaded_projects:
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    proj_state = _state.projects_dir() / slug
    project_dir = Path(loaded_projects[slug])
    gitdir_path = proj_state / "gitdir"

    if not gitdir_path.exists():
        print(f"No gitdir for {slug} -- run 'coding-cave reset' first.",
              file=sys.stderr)
        sys.exit(1)

    sync_live_sessions(slug)
    git_mod.ensure_cave_remote(project_dir, gitdir_path)

    host_git = ["git", "-C", str(project_dir)] + git_mod.SAFE_GIT_OPTS

    # Clean up stale rebase state
    host_git_dir = project_dir / ".git"
    if ((host_git_dir / "rebase-merge").is_dir()
            or (host_git_dir / "rebase-apply").is_dir()):
        print(f"Aborting stale rebase in {slug}.", file=sys.stderr)
        subprocess.run(host_git + ["rebase", "--abort"], capture_output=True)
    cur_branch = subprocess.run(
        host_git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    if (cur_branch.returncode == 0
            and cur_branch.stdout.strip() == "_cave_pull"):
        branches = subprocess.run(
            host_git + ["branch", "--format=%(refname:short)"],
            capture_output=True, text=True,
        )
        for b in branches.stdout.strip().splitlines():
            if b != "_cave_pull":
                subprocess.run(host_git + ["checkout", b],
                               capture_output=True)
                break
    subprocess.run(host_git + ["branch", "-D", "_cave_pull"],
                   capture_output=True)

    branch_result = subprocess.run(
        host_git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )

    # Host repo has no commits yet -- initialize from cave HEAD
    if branch_result.returncode != 0:
        gitdir_head_result = git_mod.git(gitdir_path, "rev-parse", "HEAD",
            capture_output=True, text=True)
        if gitdir_head_result.returncode != 0:
            print(f"No commits in cave gitdir for {slug} -- nothing to pull.",
                  file=sys.stderr)
            return
        gitdir_head = gitdir_head_result.stdout.strip()
        branch = git_mod.git(gitdir_path, "rev-parse", "--abbrev-ref", "HEAD",
            capture_output=True, text=True).stdout.strip()
        if not branch or branch == "HEAD":
            branch = "main"
        subprocess.run(host_git + ["fetch", str(gitdir_path), "HEAD"],
                       check=True, capture_output=True)
        subprocess.run(
            host_git + ["update-ref", f"refs/heads/{branch}", gitdir_head],
            check=True)
        subprocess.run(
            host_git + ["symbolic-ref", "HEAD", f"refs/heads/{branch}"],
            check=True)
        subprocess.run(host_git + ["checkout", branch], capture_output=True)
        if _needs_reauthoring(gitdir_path, project_dir):
            subprocess.run(host_git + [
                "rebase", "--root",
                "--exec", "git -c core.hooksPath=/dev/null -c core.pager=cat "
                          "-c core.fsmonitor= commit --amend --reset-author "
                          "--allow-empty --no-edit --quiet",
            ], capture_output=True)
        print(f"Pulled {slug} (initialized from cave):", file=sys.stderr)
        subprocess.run(host_git + ["log", "--oneline"], check=False)
        host_head = subprocess.run(
            host_git + ["rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        git_mod.sync_refs(gitdir_path, project_dir, host_head, branch)
        return

    current_branch = branch_result.stdout.strip()

    # Get all cave branches
    cave_branches = _get_cave_branches(gitdir_path)
    if not cave_branches:
        print(f"No commits in cave gitdir for {slug} -- nothing to pull.",
              file=sys.stderr)
        return

    # Pull non-current branches first (plumbing, no working tree impact).
    # Sync each immediately so cave gitdir stays consistent even if the
    # current-branch pull below hits a conflict and sys.exit()s.
    any_pulled = False
    for branch in sorted(cave_branches):
        if branch == current_branch:
            continue
        new_sha = _pull_branch_other(
            slug, project_dir, gitdir_path, branch,
            cave_branches[branch], host_git,
        )
        if new_sha:
            _sync_branch(gitdir_path, project_dir, new_sha, branch)
            any_pulled = True

    # Pull current branch (checkout-based, with stash).
    # May call sys.exit(1) on rebase conflict.
    if current_branch in cave_branches:
        current_pulled = _pull_branch_current(
            slug, project_dir, gitdir_path, current_branch,
            cave_branches[current_branch], host_git, force,
        )
        if current_pulled:
            _sync_branch(gitdir_path, project_dir,
                         current_pulled, current_branch)
            any_pulled = True

    # Final bookkeeping
    current_head = subprocess.run(
        host_git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    git_mod.finalize_sync(gitdir_path, project_dir)

    if not any_pulled:
        print(f"No new commits to pull from {slug}.", file=sys.stderr)


def _check_unpulled(gitdir_path, project_dir, cave_sha, host_sha, branch):
    """Check if a cave branch has unpulled commits that a push would lose.

    Returns the count (str) of unpulled commits, or None if clean.
    """
    git_mod.git(gitdir_path, "fetch", str(project_dir), host_sha,
        capture_output=True)
    merge_base = git_mod.git(gitdir_path, "merge-base",
         cave_sha, host_sha,
        capture_output=True, text=True)
    if merge_base.returncode != 0:
        return None
    unpulled = git_mod.git(gitdir_path, "rev-list", "--count",
         f"{merge_base.stdout.strip()}..{cave_sha}",
        capture_output=True, text=True)
    if unpulled.returncode == 0 and unpulled.stdout.strip() != "0":
        return unpulled.stdout.strip()
    return None


def push_project(slug, force=False):
    """Push host commits into the cave gitdir.

    Iterates all host branches and pushes each to the corresponding
    cave gitdir branch.  --force only applies to the current branch.
    """
    loaded_projects = projects.load_projects()
    if slug not in loaded_projects:
        print(f"Project not found: {slug}", file=sys.stderr)
        sys.exit(1)

    proj_state = _state.projects_dir() / slug
    project_dir = Path(loaded_projects[slug])
    gitdir_path = proj_state / "gitdir"

    if not gitdir_path.exists():
        print(f"No gitdir for {slug} -- run 'coding-cave reset' first.",
              file=sys.stderr)
        sys.exit(1)

    sync_live_sessions(slug)
    git_mod.ensure_cave_remote(project_dir, gitdir_path)

    branch_result = subprocess.run(
        ["git", "-C", str(project_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    if branch_result.returncode != 0:
        print(f"Host repo has no commits for {slug} -- nothing to push.",
              file=sys.stderr)
        return
    current_branch = branch_result.stdout.strip()

    host_branches = _get_branches(project_dir)
    if not host_branches:
        print(f"Host repo has no commits for {slug} -- nothing to push.",
              file=sys.stderr)
        return

    cave_branches = _get_cave_branches(gitdir_path)

    if force:
        # --force: only push the current branch, skip unpulled check
        host_sha = host_branches.get(current_branch)
        if not host_sha:
            return
        _sync_branch(gitdir_path, project_dir, host_sha, current_branch)
        git_mod.finalize_sync(gitdir_path, project_dir)
        print(f"Force-pushed to {slug}/{current_branch} ({host_sha[:8]}).",
              file=sys.stderr)
        return

    # Push all branches, checking each for unpulled cave commits
    any_pushed = False
    any_blocked = False
    for branch in sorted(host_branches):
        host_sha = host_branches[branch]
        cave_sha = cave_branches.get(branch)

        if cave_sha == host_sha:
            continue  # already synced

        # Check for unpulled cave commits
        if cave_sha:
            count = _check_unpulled(
                gitdir_path, project_dir, cave_sha, host_sha, branch)
            if count:
                print(f"Cave has {count} unpulled commit(s) on {branch}:",
                      file=sys.stderr)
                mb = git_mod.git(gitdir_path, "merge-base",
                     cave_sha, host_sha,
                    capture_output=True, text=True)
                if mb.returncode == 0:
                    git_mod.git(gitdir_path, "log", "--oneline",
                         f"{mb.stdout.strip()}..{cave_sha}")
                any_blocked = True
                continue

        _sync_branch(gitdir_path, project_dir, host_sha, branch)
        any_pushed = True

    # Final bookkeeping
    git_mod.finalize_sync(gitdir_path, project_dir)

    if any_pushed:
        print(f"Pushed to {slug}.", file=sys.stderr)
    elif not any_blocked:
        print(f"No commits to push to {slug}.", file=sys.stderr)

    if any_blocked:
        print(f"Run 'coding-cave pull' first, or check out the branch "
              f"and use 'coding-cave push --force'.", file=sys.stderr)
        if not any_pushed:
            sys.exit(1)


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

    sync_live_sessions(slug)

    host_branches = _get_branches(project_dir)
    cave_branches = _get_cave_branches(gitdir_path)

    if not host_branches and not cave_branches:
        return None  # both empty, nothing to compare
    if not host_branches or not cave_branches:
        return "?"

    total_pull = 0
    total_push = 0
    diverged = []

    all_branches = set(host_branches) | set(cave_branches)
    for branch in sorted(all_branches):
        host_sha = host_branches.get(branch)
        cave_sha = cave_branches.get(branch)
        if not host_sha or not cave_sha or host_sha == cave_sha:
            continue
        git_mod.git(gitdir_path, "fetch", str(project_dir), host_sha,
            capture_output=True)
        mb = git_mod.git(gitdir_path, "merge-base", cave_sha, host_sha,
            capture_output=True, text=True)
        if mb.returncode != 0:
            diverged.append(branch)
            continue
        base = mb.stdout.strip()
        to_pull = git_mod.git(gitdir_path, "rev-list", "--count",
             f"{base}..{cave_sha}",
            capture_output=True, text=True)
        to_push = git_mod.git(gitdir_path, "rev-list", "--count",
             f"{base}..{host_sha}",
            capture_output=True, text=True)
        n_pull = int(to_pull.stdout.strip()) if to_pull.returncode == 0 else 0
        n_push = int(to_push.stdout.strip()) if to_push.returncode == 0 else 0
        total_pull += n_pull
        total_push += n_push

    tags = []
    if total_pull and total_push:
        tags.append(f"{total_pull} to pull, {total_push} to push")
    elif total_pull:
        tags.append(f"{total_pull} to pull")
    elif total_push:
        tags.append(f"{total_push} to push")
    if diverged:
        tags.append(f"diverged: {', '.join(diverged)}")

    return ", ".join(tags) if tags else ""
