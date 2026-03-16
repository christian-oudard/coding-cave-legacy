"""Pull/push synchronization between host and cave."""
import os
import socket
import subprocess
import sys
from pathlib import Path

from . import _state, projects, sessions, git as git_mod, gitdir


def _stash_pop_or_warn(host_git, project_dir):
    """Pop the git stash, warning on failure instead of silently losing changes."""
    result = subprocess.run(host_git + ["stash", "pop"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"WARNING: git stash pop failed in {project_dir}.",
              file=sys.stderr)
        print(f"  Your uncommitted changes are still in the stash.",
              file=sys.stderr)
        print(f"  Run: git -C {project_dir} stash pop", file=sys.stderr)
        if result.stderr:
            for line in result.stderr.strip().splitlines()[:3]:
                print(f"  {line}", file=sys.stderr)


def sync_live_sessions(slug, *, force=False):
    """Forward refs from running sessions into the shared gitdir.

    Uses forward_session_refs which handles alternates rewrite/restore
    internally.

    When force=True (used by pull): diverged refs that can't be rebased
    are force-updated with backup.  When force=False (default): diverged
    refs are skipped.

    A shared protected_refs set prevents last-writer-wins races: once a
    branch is force-updated by one session, other sessions in the same
    cycle cannot force-update it (they fall back to skip, same as
    force=False).
    """
    shared_gitdir = _state.projects_dir() / slug / "gitdir"
    if not shared_gitdir.is_dir():
        return
    protected_refs = set()
    for session in sessions.list_sessions():
        sid = session["sid"]
        overlay_base = _state.cave_data() / "session-overlays" / sid
        session_gitdir = overlay_base / f"gitdir-{slug}"
        if session_gitdir.is_dir():
            gitdir.forward_session_refs(slug, session_gitdir, shared_gitdir,
                                        sid=sid, force=force,
                                        protected_refs=protected_refs)


def _clear_session_synced_refs(slug, branch):
    """Clear refs/synced/<branch> in running session gitdirs.

    After push --force overwrites a shared branch, session refs/synced
    may point to SHAs no longer at the shared tip.  Clearing ensures
    _forward_refs re-evaluates on next sync instead of skipping.
    """
    for session in sessions.list_sessions():
        sid = session["sid"]
        overlay_base = _state.cave_data() / "session-overlays" / sid
        synced_ref = (overlay_base / f"gitdir-{slug}"
                      / "refs" / "synced" / branch)
        try:
            synced_ref.unlink()
        except FileNotFoundError:
            pass


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
    """Print summary of amended, added, and dropped commits."""
    old_by_subject = {}
    old_by_tree = {}
    matched_old = set()
    for h, tree, subj in old_entries:
        old_by_subject[subj] = h
        old_by_tree[tree] = (h, subj)
    for h, tree, subj in new_entries:
        old_h = old_by_subject.pop(subj, None)
        if old_h and old_h != h:
            matched_old.add(old_h)
            print(f"  amend {old_h} -> {h} {subj}", file=sys.stderr)
            old_by_tree.pop(tree, None)
        elif old_h:
            matched_old.add(old_h)
            print(f"  {h} {subj}", file=sys.stderr)
            old_by_tree.pop(tree, None)
        elif tree in old_by_tree:
            old_h, old_subj = old_by_tree.pop(tree)
            matched_old.add(old_h)
            print(f"  amend {old_h} -> {h} {subj}", file=sys.stderr)
        else:
            print(f"  + {h} {subj}", file=sys.stderr)
    for h, tree, subj in old_entries:
        if h not in matched_old:
            print(f"  - {h} {subj}", file=sys.stderr)


def _notify_sessions(slug, branches):
    """Notify active sessions that shared refs were updated by the host.

    Sends a message via the session's IPC socket (Unix domain socket in
    the persistent cave home).  Sessions see it as a 'user:' message.
    """
    active = sessions.list_sessions(project=slug)
    if not active:
        return
    ipc_dir = _state.cave_home() / "ipc"
    if not ipc_dir.is_dir():
        return
    branch_str = ", ".join(branches)
    message = (f"Host pushed new commits to {slug} ({branch_str}). "
               f"Run: git fetch shared && git rebase shared/main")
    for s in active:
        sid = s.get("sid")
        if not sid:
            continue
        sock_path = ipc_dir / f"{sid}.sock"
        if not sock_path.exists():
            continue
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect(str(sock_path))
            sock.sendall(f"user: {message}".encode())
            sock.shutdown(socket.SHUT_WR)
            sock.close()
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            pass


def _cleanup_stale_pull(host_git, project_dir, slug):
    """Abort stale rebase and switch off _cave_pull if needed."""
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
    """Return {branch_name: sha} for branches in cave gitdir."""
    return _get_branches(gitdir_path, bare=True)


def _rebase_plumbing(host_git, onto_sha, merge_base, cave_sha):
    """Rebase commits (merge_base..cave_sha) onto onto_sha using plumbing."""
    def on_skip(_commit, subject):
        print(f"  {subject} (already applied, skipping)", file=sys.stderr)

    return git_mod.rebase_plumbing(
        host_git, onto_sha, merge_base, cave_sha, on_skip=on_skip)


def _sync_branch(gitdir_path, project_dir, host_sha, branch):
    """Sync a single branch ref back to the cave gitdir."""
    git_mod.git(gitdir_path, "fetch", str(project_dir), host_sha,
        capture_output=True)
    git_mod.git(gitdir_path, "update-ref",
        f"refs/heads/{branch}", host_sha, capture_output=True)
    git_mod.git(gitdir_path, "update-ref",
        f"refs/remotes/host/{branch}", host_sha, capture_output=True)


def _sync_tracking_ref(gitdir_path, project_dir, host_sha, branch):
    """Update only refs/remotes/host/<branch> (tracking ref, used by pull)."""
    git_mod.git(gitdir_path, "fetch", str(project_dir), host_sha,
        capture_output=True)
    git_mod.git(gitdir_path, "update-ref",
        f"refs/remotes/host/{branch}", host_sha, capture_output=True)


def _mark_pulled(gitdir_path, cave_sha, branch):
    """Record that cave commits up to cave_sha have been pulled to host."""
    git_mod.git(gitdir_path, "update-ref",
        f"refs/pulled/{branch}", cave_sha, capture_output=True)


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

    # Check if all cave commits were already pulled (skip unless --force)
    if not force:
        pulled_ref = git_mod.git(gitdir_path, "rev-parse",
            f"refs/pulled/{branch}",
            capture_output=True, text=True)
        if pulled_ref.returncode == 0:
            unpulled = git_mod.git(gitdir_path, "rev-list", "--count",
                f"{pulled_ref.stdout.strip()}..{cave_sha}",
                capture_output=True, text=True)
            if unpulled.returncode == 0 and unpulled.stdout.strip() == "0":
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
        if not force:
            return None
        # Cave was reset behind host.  Force-adopt cave's version.
        # This loses host-only commits -- that's what --force means.
        host_count = subprocess.run(
            host_git + ["rev-list", "--count", f"{merge_base}..HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if host_count == "0":
            return None  # truly identical
        stash_result = subprocess.run(
            host_git + ["stash", "--include-untracked"],
            capture_output=True, text=True,
        )
        did_stash = stash_result.returncode == 0 and "No local changes" not in stash_result.stdout
        subprocess.run(
            host_git + ["reset", "--hard", cave_sha], check=True,
            capture_output=True,
        )
        print(f"Force-pulled {slug}/{branch} (cave reset {host_count} commit(s) behind host):",
              file=sys.stderr)
        print(f"  {host_head[:8]} -> {cave_sha[:8]}", file=sys.stderr)
        if did_stash:
            _stash_pop_or_warn(host_git, project_dir)
        return cave_sha

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
        subprocess.run(rebase_cmd, check=True, capture_output=True)
        subprocess.run(host_git + ["checkout", branch], check=True, capture_output=True)
        subprocess.run(host_git + ["merge", "--ff-only", "_cave_pull"], check=True, capture_output=True)
        subprocess.run(host_git + ["branch", "-D", "_cave_pull"], capture_output=True)

        if stashed:
            _stash_pop_or_warn(host_git, project_dir)

        print(f"Pulled {slug} ({host_count} -> {count} commit(s)):", file=sys.stderr)
        new_entries = _parse_log(host_git, f"{merge_base}..HEAD")
        _print_rewrite_summary(old_entries, new_entries)

        return subprocess.run(
            host_git + ["rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    # Detect host-side history rewrite: host amended/squashed commits
    # since last sync while cave built on top of the old history.
    # last_synced is no longer an ancestor of host HEAD.
    if last_synced and last_synced != host_head:
        ls_ancestor = subprocess.run(
            host_git + ["merge-base", "--is-ancestor", last_synced, "HEAD"],
            capture_output=True,
        )
        if ls_ancestor.returncode != 0:
            # Check if host just rolled back (host HEAD is still an
            # ancestor of last_synced).  This is safe to pull without
            # --force: the host went backwards, the cave moved forward.
            host_behind = subprocess.run(
                host_git + ["merge-base", "--is-ancestor", "HEAD", last_synced],
                capture_output=True,
            )
            if host_behind.returncode == 0:
                stash_result = subprocess.run(
                    host_git + ["stash", "--include-untracked"],
                    capture_output=True, text=True,
                )
                stashed = (stash_result.returncode == 0
                           and "No local changes" not in stash_result.stdout)
                subprocess.run(
                    host_git + ["reset", "--hard", "FETCH_HEAD"],
                    check=True, capture_output=True,
                )
                if stashed:
                    _stash_pop_or_warn(host_git, project_dir)
                entries = _parse_log(host_git, f"{host_head}..HEAD")
                print(f"Pulled {slug} ({len(entries)} commit(s), "
                      f"host was behind last sync).", file=sys.stderr)
                for h, tree, subj in entries:
                    print(f"  {h} {subj}", file=sys.stderr)
                return subprocess.run(
                    host_git + ["rev-parse", "HEAD"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()

            if not force:
                print(f"Host rewrote history for {slug} since last sync.",
                      file=sys.stderr)
                subprocess.run(host_git + ["fetch", str(gitdir_path), cave_sha],
                               capture_output=True)
                old = _parse_log(host_git, f"{merge_base}..FETCH_HEAD")
                new = _parse_log(host_git, f"{merge_base}..HEAD")
                _print_rewrite_summary(old, new)
                print(f"Run 'coding-cave pull --force' to adopt host's history.",
                      file=sys.stderr)
                return None

            # With --force: rebase only cave commits AFTER last_synced
            # onto the host's rewritten HEAD.  Commits up to last_synced
            # are already represented in the host's rewritten history.
            new_cave_count = subprocess.run(
                host_git + ["rev-list", "--count",
                    f"{last_synced}..FETCH_HEAD"],
                capture_output=True, text=True,
            )
            new_count = (new_cave_count.stdout.strip()
                         if new_cave_count.returncode == 0 else "?")

            if new_count == "0":
                # No new cave commits beyond the rewritten point —
                # host's rewrite is all that changed.
                print(f"Host rewrote history for {slug}, no new cave commits.",
                      file=sys.stderr)
                host_head_sha = subprocess.run(
                    host_git + ["rev-parse", "HEAD"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
                _sync_tracking_ref(gitdir_path, project_dir,
                    host_head_sha, branch)
                _mark_pulled(gitdir_path, cave_sha, branch)
                return host_head_sha

            stash_result = subprocess.run(
                host_git + ["stash", "--include-untracked"],
                capture_output=True, text=True,
            )
            stashed = (stash_result.returncode == 0
                       and "No local changes" not in stash_result.stdout)

            # Rebase only the new cave commits (after last_synced) onto host
            subprocess.run(
                host_git + ["branch", "-f", "_cave_pull", "FETCH_HEAD"],
                check=True, capture_output=True)
            rebase_cmd = host_git + [
                "rebase", "--onto", branch, last_synced, "_cave_pull",
                "--no-reapply-cherry-picks", "--empty=drop",
            ]
            result = subprocess.run(rebase_cmd, capture_output=True)
            if result.returncode != 0:
                subprocess.run(host_git + ["rebase", "--abort"],
                               capture_output=True)
                subprocess.run(host_git + ["checkout", branch],
                               capture_output=True)
                subprocess.run(host_git + ["branch", "-D", "_cave_pull"],
                               capture_output=True)
                if stashed:
                    _stash_pop_or_warn(host_git, project_dir)
                print(f"Rebase failed for {slug} after host rewrite.",
                      file=sys.stderr)
                return None

            subprocess.run(host_git + ["checkout", branch],
                           capture_output=True, check=True)
            subprocess.run(host_git + ["merge", "--ff-only", "_cave_pull"],
                           capture_output=True, check=True)
            subprocess.run(host_git + ["branch", "-D", "_cave_pull"],
                           capture_output=True)

            if stashed:
                _stash_pop_or_warn(host_git, project_dir)

            print(f"Pulled {new_count} new cave commit(s) for {slug} "
                  f"(host rewrote history).", file=sys.stderr)
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
        "--no-reapply-cherry-picks", "--empty=stop",
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
            subprocess.run(host_git + ["checkout", branch], capture_output=True)
            subprocess.run(host_git + ["branch", "-D", "_cave_pull"], capture_output=True)
            if stashed:
                _stash_pop_or_warn(host_git, project_dir)
            sys.exit(1)

    subprocess.run(host_git + ["checkout", branch], capture_output=True, check=True)
    subprocess.run(host_git + ["merge", "--ff-only", "_cave_pull"], capture_output=True, check=True)
    subprocess.run(host_git + ["branch", "-D", "_cave_pull"], capture_output=True)

    if stashed:
        _stash_pop_or_warn(host_git, project_dir)

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
        return None  # branch doesn't exist on host, skip
    host_sha = host_sha_result.stdout.strip()

    if host_sha == cave_sha:
        return None

    # Check if all cave commits were already pulled
    pulled_ref = git_mod.git(gitdir_path, "rev-parse",
        f"refs/pulled/{branch}",
        capture_output=True, text=True)
    if pulled_ref.returncode == 0:
        unpulled = git_mod.git(gitdir_path, "rev-list", "--count",
            f"{pulled_ref.stdout.strip()}..{cave_sha}",
            capture_output=True, text=True)
        if unpulled.returncode == 0 and unpulled.stdout.strip() == "0":
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
    if merge_base == host_sha:
        # True fast-forward
        new_sha = cave_sha
    else:
        # Rebase (diverged)
        new_sha = _rebase_plumbing(
            host_git, host_sha, merge_base, cave_sha,
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

    sync_live_sessions(slug, force=True)
    git_mod.ensure_cave_remote(project_dir, gitdir_path)

    host_git = ["git", "-C", str(project_dir)] + git_mod.SAFE_GIT_OPTS

    _cleanup_stale_pull(host_git, project_dir, slug)

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
        print(f"Pulled {slug} (initialized from cave):", file=sys.stderr)
        subprocess.run(host_git + ["log", "--oneline"], check=False)
        host_head = subprocess.run(
            host_git + ["rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        git_mod.sync_refs(gitdir_path, project_dir, host_head, branch)
        _mark_pulled(gitdir_path, host_head, branch)
        return

    current_branch = branch_result.stdout.strip()

    # Get all cave branches
    cave_branches = _get_cave_branches(gitdir_path)
    if not cave_branches:
        print(f"No commits in cave gitdir for {slug} -- nothing to pull.",
              file=sys.stderr)
        return

    # Detached HEAD recovery: re-attach to a cave branch so pull works.
    # Prefer main, fall back to any cave branch that matches HEAD.
    if current_branch == "HEAD":
        head_sha = subprocess.run(
            host_git + ["rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        # Pick a branch to attach to
        attach = "main" if "main" in cave_branches else None
        if not attach:
            for b, sha in cave_branches.items():
                if sha == head_sha:
                    attach = b
                    break
        if not attach:
            attach = next(iter(cave_branches))
        # Create branch if it doesn't exist on host, then check it out
        subprocess.run(
            host_git + ["checkout", "-B", attach, head_sha],
            capture_output=True,
        )
        current_branch = attach

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
            _sync_tracking_ref(gitdir_path, project_dir, new_sha, branch)
            _mark_pulled(gitdir_path, cave_branches[branch], branch)
            any_pulled = True

    # Pull current branch (checkout-based, with stash).
    # May call sys.exit(1) on rebase conflict.
    if current_branch in cave_branches:
        current_pulled = _pull_branch_current(
            slug, project_dir, gitdir_path, current_branch,
            cave_branches[current_branch], host_git, force,
        )
        if current_pulled:
            _sync_tracking_ref(gitdir_path, project_dir,
                               current_pulled, current_branch)
            _mark_pulled(gitdir_path,
                         cave_branches[current_branch], current_branch)
            any_pulled = True

    # Keep shared gitdir HEAD in sync with host's current branch so new
    # sessions start on the right branch (gitdir.py copies HEAD on creation).
    git_mod.git(gitdir_path, "symbolic-ref", "HEAD",
        f"refs/heads/{current_branch}", capture_output=True)

    # Final bookkeeping
    current_head = subprocess.run(
        host_git + ["rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    git_mod.finalize_sync(gitdir_path, project_dir)

    # Check if host is ahead of cave (has commits to push)
    host_branches = _get_branches(project_dir)
    total_ahead = 0
    for branch in sorted(host_branches):
        host_sha = host_branches[branch]
        tracking = git_mod.git(gitdir_path, "rev-parse",
            f"refs/remotes/host/{branch}",
            capture_output=True, text=True)
        if tracking.returncode != 0:
            continue
        tracking_sha = tracking.stdout.strip()
        if tracking_sha == host_sha:
            continue
        count_result = subprocess.run(
            host_git + ["rev-list", "--count",
                f"{tracking_sha}..{branch}"],
            capture_output=True, text=True,
        )
        if count_result.returncode == 0:
            total_ahead += int(count_result.stdout.strip())

    if not any_pulled:
        if total_ahead:
            print(f"Already up to date, host is {total_ahead} commit(s) ahead.",
                  file=sys.stderr)
            print(f"  (use 'coding-cave push' to sync)", file=sys.stderr)
        else:
            print(f"Already up to date.", file=sys.stderr)
    elif total_ahead:
        print(f"Host is {total_ahead} commit(s) ahead, use 'coding-cave push' to sync.",
              file=sys.stderr)


def _check_unpulled(gitdir_path, project_dir, cave_sha, host_sha, branch):
    """Check if a cave branch has unpulled commits that a push would lose.

    Returns the count (str) of unpulled commits, or None if clean.
    """
    # Check refs/pulled/<branch> first — records what was already pulled
    pulled = git_mod.git(gitdir_path, "rev-parse",
        f"refs/pulled/{branch}",
        capture_output=True, text=True)
    if pulled.returncode == 0:
        pulled_sha = pulled.stdout.strip()
        unpulled = git_mod.git(gitdir_path, "rev-list", "--count",
            f"{pulled_sha}..{cave_sha}",
            capture_output=True, text=True)
        if unpulled.returncode == 0 and unpulled.stdout.strip() != "0":
            return unpulled.stdout.strip()
        return None

    # Fallback for old gitdirs without refs/pulled
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

    host_git = ["git", "-C", str(project_dir)] + git_mod.SAFE_GIT_OPTS

    _cleanup_stale_pull(host_git, project_dir, slug)

    branch_result = subprocess.run(
        host_git + ["rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    if branch_result.returncode != 0:
        print(f"Host repo has no commits for {slug} -- nothing to push.",
              file=sys.stderr)
        return
    current_branch = branch_result.stdout.strip()

    host_branches = _get_branches(project_dir)
    host_branches.pop("_cave_pull", None)
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
        cave_sha = cave_branches.get(current_branch)
        if cave_sha == host_sha:
            print(f"Already up to date.", file=sys.stderr)
            return
        # Warn if overwriting cave commits from sessions
        if cave_sha and cave_sha != host_sha:
            count = _check_unpulled(
                gitdir_path, project_dir, cave_sha, host_sha,
                current_branch)
            if count:
                print(f"Overwriting {count} unpulled cave commit(s) "
                      f"on {current_branch}.", file=sys.stderr)
                print(f"  Session work will be rebased on next sync.",
                      file=sys.stderr)
        _sync_branch(gitdir_path, project_dir, host_sha, current_branch)
        _mark_pulled(gitdir_path, host_sha, current_branch)
        # Clear refs/synced in running sessions so _forward_refs
        # re-evaluates on next sync (their synced ref now points to a
        # SHA that is no longer the shared branch tip).
        _clear_session_synced_refs(slug, current_branch)
        git_mod.git(gitdir_path, "symbolic-ref", "HEAD",
            f"refs/heads/{current_branch}", capture_output=True)
        git_mod.finalize_sync(gitdir_path, project_dir)
        print(f"Force-pushed to {slug}/{current_branch} ({host_sha[:8]}).",
              file=sys.stderr)
        _notify_sessions(slug, [current_branch])
        return

    # Push all branches, checking each for unpulled cave commits
    pushed_branches = []
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
        _mark_pulled(gitdir_path, host_sha, branch)
        pushed_branches.append((branch, host_sha))

    # Keep shared gitdir HEAD in sync with host's current branch so new
    # sessions start on the right branch (gitdir.py copies HEAD on creation).
    git_mod.git(gitdir_path, "symbolic-ref", "HEAD",
        f"refs/heads/{current_branch}", capture_output=True)

    # Final bookkeeping
    git_mod.finalize_sync(gitdir_path, project_dir)

    if pushed_branches:
        for branch, sha in pushed_branches:
            print(f"Pushed to {slug}/{branch} ({sha[:8]}).",
                  file=sys.stderr)
        _notify_sessions(slug, [b for b, _ in pushed_branches])
    elif not any_blocked:
        print(f"Already up to date.", file=sys.stderr)

    if any_blocked:
        print(f"Run 'coding-cave pull' first, or check out the branch "
              f"and use 'coding-cave push --force'.", file=sys.stderr)
        if not pushed_branches:
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
        if not host_sha or not cave_sha:
            continue

        # to pull: cave commits not yet pulled
        pulled = git_mod.git(gitdir_path, "rev-parse",
            f"refs/pulled/{branch}",
            capture_output=True, text=True)
        if pulled.returncode == 0:
            pulled_sha = pulled.stdout.strip()
            to_pull = git_mod.git(gitdir_path, "rev-list", "--count",
                f"{pulled_sha}..{cave_sha}",
                capture_output=True, text=True)
        else:
            # Fallback: merge-base for old gitdirs without refs/pulled
            git_mod.git(gitdir_path, "fetch", str(project_dir), host_sha,
                capture_output=True)
            mb = git_mod.git(gitdir_path, "merge-base", cave_sha, host_sha,
                capture_output=True, text=True)
            if mb.returncode != 0:
                diverged.append(branch)
                continue
            to_pull = git_mod.git(gitdir_path, "rev-list", "--count",
                f"{mb.stdout.strip()}..{cave_sha}",
                capture_output=True, text=True)
        n_pull = int(to_pull.stdout.strip()) if to_pull.returncode == 0 else 0

        # to push: host commits beyond what cave knows
        tracking_sha = _last_synced_sha(gitdir_path, branch)
        if tracking_sha:
            if tracking_sha == host_sha:
                n_push = 0
            else:
                git_mod.git(gitdir_path, "fetch", str(project_dir), host_sha,
                    capture_output=True)
                to_push = git_mod.git(gitdir_path, "rev-list", "--count",
                    f"{tracking_sha}..{host_sha}",
                    capture_output=True, text=True)
                n_push = int(to_push.stdout.strip()) if to_push.returncode == 0 else 0
        else:
            # Fallback: merge-base
            git_mod.git(gitdir_path, "fetch", str(project_dir), host_sha,
                capture_output=True)
            mb = git_mod.git(gitdir_path, "merge-base", cave_sha, host_sha,
                capture_output=True, text=True)
            if mb.returncode != 0:
                if branch not in diverged:
                    diverged.append(branch)
                continue
            to_push = git_mod.git(gitdir_path, "rev-list", "--count",
                f"{mb.stdout.strip()}..{host_sha}",
                capture_output=True, text=True)
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
