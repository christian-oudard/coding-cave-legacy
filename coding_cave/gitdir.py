"""Per-session gitdir creation and merging."""
import os
import shutil
import subprocess
from pathlib import Path

from . import _state, git as git_mod


def create_session_gitdir(slug, sid, overlay_base):
    """Create a per-session gitdir with alternates pointing to shared objects.

    The session gitdir has its own HEAD, index, and refs but shares the object
    store with the shared gitdir via alternates.  Alternates starts with the
    host path; the caller rewrites it to the cave-internal path before bwrap,
    and merge_session_gitdirs rewrites it back for cleanup.
    """
    shared_gitdir = _state.projects_dir() / slug / "gitdir"
    if not shared_gitdir.is_dir():
        return None

    session_gitdir = overlay_base / f"gitdir-{slug}"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(session_gitdir)],
        capture_output=True, check=True,
    )

    # Not actually bare -- used with a separate working tree via .git file
    git_mod.git(session_gitdir, "config", "core.bare", "false", capture_output=True)

    # Alternates: host path (tests + cleanup use this directly)
    objects_info = session_gitdir / "objects" / "info"
    objects_info.mkdir(parents=True, exist_ok=True)
    (objects_info / "alternates").write_text(
        f"{shared_gitdir / 'objects'}\n"
    )

    # Copy HEAD
    (session_gitdir / "HEAD").write_text((shared_gitdir / "HEAD").read_text())

    # Copy all refs from shared gitdir and record initial state
    result = subprocess.run(
        ["git", "--git-dir", str(shared_gitdir), "for-each-ref",
         "--format=%(refname) %(objectname)"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            ref, sha = line.split()
            git_mod.git(session_gitdir, "update-ref", ref, sha, capture_output=True)
            # Record initial ref state so _forward_refs can skip
            # unmodified refs during live sync.
            if ref.startswith("refs/heads/"):
                branch = ref.removeprefix("refs/heads/")
                git_mod.git(session_gitdir, "update-ref",
                    f"refs/init/{branch}", sha, capture_output=True)

    # Copy stash reflog (stash ref itself is copied above with all refs,
    # but the reflog tracks the full stash stack)
    src_stash_log = shared_gitdir / "logs" / "refs" / "stash"
    if src_stash_log.is_file():
        dst_stash_log = session_gitdir / "logs" / "refs" / "stash"
        dst_stash_log.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_stash_log, dst_stash_log)

    # Restore saved index if available (preserves staging area from prior
    # session), otherwise populate from HEAD so git status starts clean.
    saved_index = _state.projects_dir() / slug / "saved-index"
    if saved_index.is_file():
        shutil.copy2(saved_index, session_gitdir / "index")
        saved_index.unlink()
    else:
        git_mod.git(session_gitdir, "read-tree", "HEAD", capture_output=True)

    # Disable hooks
    git_mod.git(session_gitdir, "config", "core.hooksPath", "/dev/null",
        capture_output=True)
    hooks = session_gitdir / "hooks"
    if hooks.exists():
        shutil.rmtree(hooks)

    return session_gitdir


def _rebase_onto_reauthored(gitdir, shared_sha, session_sha, merge_base):
    """Rebase session commits onto a re-authored shared chain.

    When the shared gitdir has re-authored commits (same trees, different
    hashes from pull --reset-author), and the session has new commits on top
    of the old chain, this rebases only the new commits onto the shared HEAD.

    Returns the new tip SHA if successful, None if not applicable.
    """
    if not merge_base:
        return None

    # Build tree->commit map from shared chain (merge_base..shared_sha)
    shared_log = git_mod.git(gitdir, "log", "--format=%H %T",
                     f"{merge_base}..{shared_sha}",
                     capture_output=True, text=True)
    if shared_log.returncode != 0:
        return None
    shared_trees = {}
    for line in shared_log.stdout.strip().splitlines():
        if not line:
            continue
        commit, tree = line.split()
        shared_trees[tree] = commit
    if not shared_trees:
        return None

    # Walk session chain (merge_base..session_sha) to find re-authoring boundary
    session_log = git_mod.git(gitdir, "log", "--format=%H %T",
                      f"{merge_base}..{session_sha}",
                      capture_output=True, text=True)
    if session_log.returncode != 0:
        return None
    session_commits = []
    for line in session_log.stdout.strip().splitlines():
        if not line:
            continue
        commit, tree = line.split()
        session_commits.append((commit, tree))

    # Find boundary: first session commit whose tree exists in shared chain
    # (session_commits is newest-first from git log)
    boundary_idx = None
    for i, (commit, tree) in enumerate(session_commits):
        if tree in shared_trees:
            boundary_idx = i
            break
    if boundary_idx is None:
        return None

    # Commits above the boundary are new work to rebase (reverse for oldest-first)
    new_commits = list(reversed(session_commits[:boundary_idx]))
    if not new_commits:
        return None

    # Rebase new commits onto shared HEAD using commit-tree
    parent = shared_sha
    for commit, tree in new_commits:
        info = git_mod.git(gitdir, "log", "-1", "--format=%an%n%ae%n%aI%n%B", commit,
                   capture_output=True, text=True)
        if info.returncode != 0:
            return None
        lines = info.stdout.rstrip("\n").split("\n", 3)
        author_name, author_email, author_date = lines[0], lines[1], lines[2]
        message = lines[3] if len(lines) > 3 else ""
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_AUTHOR_DATE": author_date,
        }
        result = subprocess.run(
            ["git", "--git-dir", str(gitdir)] + git_mod.SAFE_GIT_OPTS +
            ["commit-tree", tree, "-p", parent, "-m", message],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            return None
        parent = result.stdout.strip()

    return parent


def _rebase_commits(gitdir, shared_sha, session_sha, merge_base):
    """Rebase session commits onto shared HEAD using three-way merge.

    For each commit in merge_base..session_sha, compute a three-way merge
    (base=commit's parent, ours=current target, theirs=commit) using
    git merge-tree --write-tree.  Returns the new tip SHA if all merges
    succeed cleanly, None if any conflict.
    """
    if not merge_base:
        return None

    # Collect session commits after merge_base (newest-first from log)
    session_log = git_mod.git(gitdir, "log", "--format=%H",
                      f"{merge_base}..{session_sha}",
                      capture_output=True, text=True)
    if session_log.returncode != 0:
        return None
    commits = [c for c in session_log.stdout.strip().splitlines() if c]
    if not commits:
        return None
    commits.reverse()  # oldest-first

    parent = shared_sha
    for commit in commits:
        # Get this commit's parent (for the merge base of the cherry-pick)
        cp = git_mod.git(gitdir, "rev-parse", f"{commit}^",
                 capture_output=True, text=True)
        if cp.returncode != 0:
            return None
        commit_parent = cp.stdout.strip()

        # Three-way merge: base=commit_parent, ours=parent, theirs=commit
        merge = git_mod.git(gitdir, "merge-tree", "--write-tree",
                    f"--merge-base={commit_parent}", parent, commit,
                    capture_output=True, text=True)
        if merge.returncode != 0:
            return None  # conflict
        merged_tree = merge.stdout.strip().splitlines()[0]

        # Preserve original author info
        info = git_mod.git(gitdir, "log", "-1", "--format=%an%n%ae%n%aI%n%B", commit,
                   capture_output=True, text=True)
        if info.returncode != 0:
            return None
        lines = info.stdout.rstrip("\n").split("\n", 3)
        author_name, author_email, author_date = lines[0], lines[1], lines[2]
        message = lines[3] if len(lines) > 3 else ""
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_AUTHOR_DATE": author_date,
        }
        result = subprocess.run(
            ["git", "--git-dir", str(gitdir)] + git_mod.SAFE_GIT_OPTS +
            ["commit-tree", merged_tree, "-p", parent, "-m", message],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            return None
        parent = result.stdout.strip()

    return parent


def _forward_refs(sid, session_gitdir, shared_gitdir, *, force=False):
    """Forward branch refs from a session gitdir into the shared gitdir.

    Handles fast-forwards, re-authoring detection, rebase-onto-reauthored,
    and general rebase (when shared moved forward while session was working).

    When force=False (live sync): never force-updates, never moves refs
    backward.  Diverged refs that can't be resolved by fast-forward or
    rebase are skipped -- they'll be picked up on final cleanup.

    When force=True (final cleanup): force-updates diverged refs and backs
    up the old shared ref under refs/sessions/<sid>/<branch>.
    """
    result = git_mod.git(session_gitdir, "for-each-ref",
                 "--format=%(refname) %(objectname)", "refs/heads/",
                 capture_output=True, text=True)
    if result.returncode != 0:
        return

    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        ref, sha = line.split()
        branch = ref.removeprefix("refs/heads/")

        # Skip refs the session never modified (still at init value)
        # or hasn't changed since last sync (still at synced value).
        # Without this, a session that hasn't touched a project can
        # revert another session's amend by forwarding its stale ref,
        # and a session whose commits were re-authored by pull would
        # overwrite the re-authored ref with its original.
        if not force:
            init_ref = git_mod.git(session_gitdir, "rev-parse",
                           f"refs/init/{branch}",
                           capture_output=True, text=True)
            if (init_ref.returncode == 0
                    and init_ref.stdout.strip() == sha):
                continue
            synced_ref = git_mod.git(session_gitdir, "rev-parse",
                             f"refs/synced/{branch}",
                             capture_output=True, text=True)
            if (synced_ref.returncode == 0
                    and synced_ref.stdout.strip() == sha):
                continue

        # Fetch the objects into shared gitdir
        fetch = git_mod.git(shared_gitdir, "fetch", str(session_gitdir), sha,
                    capture_output=True)
        if fetch.returncode != 0:
            continue

        # Check if fast-forward is possible
        existing = git_mod.git(shared_gitdir, "rev-parse", ref,
                       capture_output=True, text=True)
        if existing.returncode != 0:
            # Ref doesn't exist in shared -- create it
            git_mod.git(shared_gitdir, "update-ref", ref, sha,
                capture_output=True)
            continue

        existing_sha = existing.stdout.strip()
        if existing_sha == sha:
            continue  # already up to date

        merge_base = git_mod.git(shared_gitdir, "merge-base",
                         existing_sha, sha,
                         capture_output=True, text=True)
        if (merge_base.returncode == 0
                and merge_base.stdout.strip() == existing_sha):
            # Fast-forward
            git_mod.git(shared_gitdir, "update-ref", ref, sha,
                capture_output=True)
            # Record that this session has synced this branch
            git_mod.git(session_gitdir, "update-ref",
                f"refs/synced/{branch}", sha, capture_output=True)
        elif (merge_base.returncode == 0
                  and merge_base.stdout.strip() == sha):
            continue  # session is behind shared, skip
        else:
            # Diverged -- check if it's a real content change or just
            # re-authoring (pull rewrites commits to change author).
            session_tree = git_mod.git(shared_gitdir, "rev-parse",
                               f"{sha}^{{tree}}",
                               capture_output=True, text=True)
            shared_tree = git_mod.git(shared_gitdir, "rev-parse",
                              f"{existing_sha}^{{tree}}",
                              capture_output=True, text=True)
            if (session_tree.returncode == 0
                    and shared_tree.returncode == 0
                    and session_tree.stdout.strip()
                        == shared_tree.stdout.strip()):
                # Same final tree.  If the session changed since last
                # sync, it rewrote history (squash/amend) preserving
                # content -- update shared so pull can detect the
                # rewrite.  Otherwise it's just re-authoring metadata.
                synced = git_mod.git(session_gitdir, "rev-parse",
                              f"refs/synced/{branch}",
                              capture_output=True, text=True)
                if (synced.returncode == 0
                        and synced.stdout.strip() != sha):
                    git_mod.git(shared_gitdir, "update-ref", ref, sha,
                        capture_output=True)
                    git_mod.git(session_gitdir, "update-ref",
                        f"refs/synced/{branch}", sha,
                        capture_output=True)
                continue

            # Try rebasing new session commits onto shared chain
            mb = (merge_base.stdout.strip()
                  if merge_base.returncode == 0 else None)

            # Detect single-commit amendment: both sides have exactly
            # 1 commit above the merge base (same parent).  This is
            # the normal `git commit --amend` case -- let the session
            # version win since it has the most recent work.
            # Trigger when either:
            #   - refs/synced exists (session previously synced this branch)
            #   - refs/init equals shared sha (session was born with this
            #     exact commit and amended it -- first sync after amend)
            has_synced = git_mod.git(session_gitdir, "rev-parse",
                             f"refs/synced/{branch}",
                             capture_output=True, text=True)
            init_ref = git_mod.git(session_gitdir, "rev-parse",
                           f"refs/init/{branch}",
                           capture_output=True, text=True)
            init_matches_shared = (init_ref.returncode == 0
                                   and init_ref.stdout.strip() == existing_sha)
            if mb and not force and (has_synced.returncode == 0
                                     or init_matches_shared):
                shared_count = git_mod.git(shared_gitdir, "rev-list", "--count",
                                   f"{mb}..{existing_sha}",
                                   capture_output=True, text=True)
                session_count = git_mod.git(shared_gitdir, "rev-list", "--count",
                                    f"{mb}..{sha}",
                                    capture_output=True, text=True)
                if (shared_count.returncode == 0
                        and session_count.returncode == 0
                        and shared_count.stdout.strip() == "1"
                        and session_count.stdout.strip() == "1"):
                    git_mod.git(shared_gitdir, "update-ref", ref, sha,
                        capture_output=True)
                    git_mod.git(session_gitdir, "update-ref",
                        f"refs/synced/{branch}", sha,
                        capture_output=True)
                    continue

            rebased = _rebase_onto_reauthored(
                shared_gitdir, existing_sha, sha, mb)
            if not rebased:
                rebased = _rebase_commits(
                    shared_gitdir, existing_sha, sha, mb)
            if rebased:
                git_mod.git(shared_gitdir, "update-ref", ref, rebased,
                    capture_output=True)
                continue

            if not force:
                continue  # live sync: skip diverged refs

            # Final cleanup: force-update shared ref, back up old ref.
            git_mod.git(shared_gitdir, "update-ref",
                f"refs/sessions/{sid}/{branch}", existing_sha,
                capture_output=True)
            git_mod.git(shared_gitdir, "update-ref", ref, sha,
                capture_output=True)

    # Also fetch tags
    git_mod.git(shared_gitdir, "fetch", str(session_gitdir),
        "refs/tags/*:refs/tags/*", capture_output=True)

    # Forward stash (ref + reflog for the full stash stack)
    stash = git_mod.git(session_gitdir, "rev-parse", "refs/stash",
                capture_output=True, text=True)
    if stash.returncode == 0:
        stash_sha = stash.stdout.strip()
        fetch = git_mod.git(shared_gitdir, "fetch", str(session_gitdir), stash_sha,
                    capture_output=True)
        if fetch.returncode == 0:
            git_mod.git(shared_gitdir, "update-ref", "refs/stash", stash_sha,
                capture_output=True)
            # Copy reflog to preserve the full stash stack
            src_log = session_gitdir / "logs" / "refs" / "stash"
            dst_log = Path(str(shared_gitdir)) / "logs" / "refs" / "stash"
            if src_log.is_file():
                dst_log.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_log, dst_log)


def forward_session_refs(slug, session_gitdir, shared_gitdir):
    """Forward refs from a live session into the shared gitdir (safe).

    Called by sync_live_sessions during pull/push.  Temporarily rewrites
    alternates to host path for git operations, then restores cave path.
    Never force-updates -- diverged refs are skipped.
    """
    alt_file = session_gitdir / "objects" / "info" / "alternates"
    cave_path = None
    if alt_file.exists():
        cave_path = alt_file.read_text().strip()
        alt_file.write_text(f"{shared_gitdir / 'objects'}\n")

    _forward_refs(None, session_gitdir, shared_gitdir, force=False)

    # Restore cave-internal alternates path
    if cave_path is not None:
        alt_file.write_text(f"{cave_path}\n")


def _save_session_index(session_gitdir, shared_gitdir, slug):
    """Save session index and its loose objects for staging area preservation.

    Only saves if the index differs from HEAD (has staged changes).
    Copies loose objects (blobs from git-add not reachable from commits)
    from the session gitdir into the shared gitdir, then saves the index
    file to a persistent location.
    """
    index_file = session_gitdir / "index"
    if not index_file.is_file():
        return
    # Check if index has staged changes (differs from HEAD tree).
    # If index matches HEAD, don't save -- avoids clobbering another
    # session's saved-index with a clean index.
    diff = git_mod.git(session_gitdir, "diff-index", "--cached", "--quiet", "HEAD",
               capture_output=True)
    if diff.returncode == 0:
        return  # index matches HEAD, nothing staged
    # Copy loose objects to shared gitdir
    objects_dir = session_gitdir / "objects"
    shared_objects = shared_gitdir / "objects"
    for subdir in objects_dir.iterdir():
        if not subdir.is_dir() or subdir.name in ("info", "pack"):
            continue
        if len(subdir.name) != 2:
            continue
        dst_subdir = shared_objects / subdir.name
        dst_subdir.mkdir(exist_ok=True)
        for obj_file in subdir.iterdir():
            dst = dst_subdir / obj_file.name
            if not dst.exists():
                try:
                    shutil.copy2(obj_file, dst)
                except OSError:
                    pass
    # Save index file
    saved_index = _state.projects_dir() / slug / "saved-index"
    try:
        shutil.copy2(index_file, saved_index)
    except OSError:
        pass


def merge_session_gitdirs(sid, overlay_base):
    """Merge per-session gitdir refs and objects back into shared gitdirs.

    Called on session exit (final cleanup).  Force-updates diverged refs
    with backup under refs/sessions/<sid>/<branch>.

    Scans overlay_base for gitdir-<slug> directories rather than relying on
    the project registry, so cleanup works even if the registry changes.
    """
    if not overlay_base.is_dir():
        return
    for entry in sorted(overlay_base.iterdir()):
        if not entry.name.startswith("gitdir-") or not entry.is_dir():
            continue
        slug = entry.name.removeprefix("gitdir-")
        session_gitdir = entry
        shared_gitdir = _state.projects_dir() / slug / "gitdir"
        if not shared_gitdir.is_dir():
            continue

        # Rewrite alternates to host path (cave path won't exist on host)
        alt_file = session_gitdir / "objects" / "info" / "alternates"
        if alt_file.exists():
            alt_file.write_text(f"{shared_gitdir / 'objects'}\n")

        _forward_refs(sid, session_gitdir, shared_gitdir, force=True)

        # Save index and its loose objects so staging area survives
        # session restarts.  Loose objects in the session gitdir include
        # blobs from git-add that aren't reachable from any commit.
        _save_session_index(session_gitdir, shared_gitdir, slug)
