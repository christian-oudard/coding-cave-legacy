"""Per-session gitdir creation and merging."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import _state, git as git_mod


def _atomic_write(path, content):
    """Write content to path atomically via temp file + rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    try:
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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

    # Add remote pointing to shared gitdir (cave-internal path)
    git_mod.git(session_gitdir, "remote", "add", "shared",
        f"/projects/.shared-gitdirs/{slug}", capture_output=True)
    # Also fetch host-tracking refs so post-rewrite hook can detect
    # commits that have been pulled to the host.
    git_mod.git(session_gitdir, "config", "--add", "remote.shared.fetch",
        "+refs/remotes/host/*:refs/remotes/host/*", capture_output=True)

    # Post-commit hook: push current branch to shared, fetch latest
    hooks = session_gitdir / "hooks"
    if hooks.exists():
        shutil.rmtree(hooks)
    hooks.mkdir()
    (hooks / "post-commit").write_text(
        '#!/bin/bash\n'
        'branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || exit 0\n'
        'git push shared "$branch" 2>/dev/null &&\n'
        '  git update-ref "refs/pushed/$branch" HEAD 2>/dev/null\n'
        'git fetch shared 2>/dev/null\n'
        'exit 0\n'
    )
    os.chmod(hooks / "post-commit", 0o755)

    # --- Amend/rebase guards ---
    # Three layers of protection against rewriting pulled commits:
    #   1. prepare-commit-msg: blocks amend BEFORE it happens (primary)
    #   2. pre-rebase: blocks rebase of pulled commits
    #   3. post-rewrite: undoes rewrites that slip through (fallback)
    # All use refs/remotes/host/* to detect pulled commits.

    # Layer 1: prepare-commit-msg blocks amend before commit is created.
    # Detects --amend via /proc/$PPID/cmdline (Linux-specific).
    # Not bypassed by --no-verify.
    (hooks / "prepare-commit-msg").write_text(
        '#!/bin/bash\n'
        'parent_cmd=$(tr "\\0" " " < /proc/$PPID/cmdline 2>/dev/null)\n'
        'echo "$parent_cmd" | grep -q -- "--amend" || exit 0\n'
        'head_sha=$(git rev-parse HEAD 2>/dev/null) || exit 0\n'
        'pulled_refs=$(git for-each-ref --format="%(objectname)" '
        'refs/remotes/host/ 2>/dev/null)\n'
        '[ -z "$pulled_refs" ] && exit 0\n'
        'for pulled in $pulled_refs; do\n'
        '    if git merge-base --is-ancestor "$head_sha" "$pulled" '
        '2>/dev/null; then\n'
        '        echo >&2\n'
        '        echo "ERROR: Cannot amend. commit '
        '${head_sha:0:8} was already pulled to the host." >&2\n'
        '        echo "Create a new commit instead." >&2\n'
        '        exit 1\n'
        '    fi\n'
        'done\n'
        'exit 0\n'
    )
    os.chmod(hooks / "prepare-commit-msg", 0o755)

    # Layer 2: pre-rebase blocks rebase of pulled commits.
    # Bypassed by --no-verify, but post-rewrite catches that.
    (hooks / "pre-rebase").write_text(
        '#!/bin/bash\n'
        'upstream=$1\n'
        'branch=${2:-HEAD}\n'
        'pulled_refs=$(git for-each-ref --format="%(objectname)" '
        'refs/remotes/host/ 2>/dev/null)\n'
        '[ -z "$pulled_refs" ] && exit 0\n'
        'for commit in $(git rev-list "$upstream..$branch" 2>/dev/null); do\n'
        '    for pulled in $pulled_refs; do\n'
        '        if git merge-base --is-ancestor "$commit" "$pulled" '
        '2>/dev/null; then\n'
        '            echo >&2\n'
        '            echo "ERROR: Cannot rebase. commit '
        '${commit:0:8} was already pulled to the host." >&2\n'
        '            echo "Create new commits instead." >&2\n'
        '            exit 1\n'
        '        fi\n'
        '    done\n'
        'done\n'
        'exit 0\n'
    )
    os.chmod(hooks / "pre-rebase", 0o755)

    # Layer 3: post-rewrite undoes rewrites that slip past layers 1-2.
    # Cannot be bypassed with --no-verify.
    (hooks / "post-rewrite").write_text(
        '#!/bin/bash\n'
        'action=$1\n'
        'pulled_refs=$(git for-each-ref --format="%(objectname)" '
        'refs/remotes/host/ 2>/dev/null)\n'
        '[ -z "$pulled_refs" ] && exit 0\n'
        'while read old_sha new_sha extra; do\n'
        '    for pulled in $pulled_refs; do\n'
        '        if git merge-base --is-ancestor "$old_sha" "$pulled" '
        '2>/dev/null; then\n'
        '            echo >&2\n'
        '            echo "ERROR: Cannot $action. commit '
        '${old_sha:0:8} was already pulled to the host." >&2\n'
        '            echo "Create a new commit instead." >&2\n'
        '            if [ "$action" = "amend" ]; then\n'
        '                git reset --soft "$old_sha" 2>/dev/null\n'
        '            else\n'
        '                git reset --hard ORIG_HEAD 2>/dev/null\n'
        '            fi\n'
        '            exit 1\n'
        '        fi\n'
        '    done\n'
        'done\n'
        'exit 0\n'
    )
    os.chmod(hooks / "post-rewrite", 0o755)

    return session_gitdir



def _is_patch_upstream(gitdir, commit, merge_base, upstream_sha):
    """Check if a commit's patch is already applied in the upstream range.

    Uses git patch-id to compare the commit's diff against all diffs
    in merge_base..upstream_sha.  Returns True if a match is found.
    """
    # Get patch-id of the commit
    show = git_mod.git(gitdir, "diff-tree", "-p", commit,
                       capture_output=True)
    if show.returncode != 0 or not show.stdout:
        return False
    pid = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=show.stdout, capture_output=True,
    )
    if pid.returncode != 0 or not pid.stdout.strip():
        return False
    commit_patch_id = pid.stdout.split()[0]

    # Get patch-ids of all upstream commits
    log = git_mod.git(gitdir, "log", "-p",
                      f"{merge_base}..{upstream_sha}",
                      capture_output=True)
    if log.returncode != 0 or not log.stdout:
        return False
    upstream_pids = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=log.stdout, capture_output=True,
    )
    if upstream_pids.returncode != 0:
        return False
    for line in upstream_pids.stdout.strip().splitlines():
        if line and line.split()[0] == commit_patch_id:
            return True
    return False


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
            # Conflict — check if this commit's patch is already upstream
            # (equivalent to --no-reapply-cherry-picks in git rebase).
            # Use patch-id to detect cherry-pick equivalence.
            already_applied = _is_patch_upstream(
                gitdir, commit, merge_base, shared_sha)
            if already_applied:
                continue
            return None  # real conflict
        merged_tree = merge.stdout.strip().splitlines()[0]

        # Skip empty commits (tree unchanged) — happens when the commit's
        # changes are already in the target, e.g. after an amend of a
        # previously-merged session commit.
        parent_tree = git_mod.git(gitdir, "rev-parse", f"{parent}^{{tree}}",
                          capture_output=True, text=True)
        if (parent_tree.returncode == 0
                and parent_tree.stdout.strip() == merged_tree):
            continue

        # Preserve original author and committer info
        info = git_mod.git(gitdir, "log", "-1",
                   "--format=%an%n%ae%n%aI%n%cn%n%ce%n%cI%n%B", commit,
                   capture_output=True, text=True)
        if info.returncode != 0:
            return None
        lines = info.stdout.rstrip("\n").split("\n", 6)
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
            ["git", "--git-dir", str(gitdir)] + git_mod.SAFE_GIT_OPTS +
            ["commit-tree", merged_tree, "-p", parent, "-m", message],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            return None
        parent = result.stdout.strip()

    return parent


def _ref_sha(gitdir, ref):
    """Return the SHA a ref points to, or None if it doesn't exist."""
    r = git_mod.git(gitdir, "rev-parse", ref,
                    capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


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

        # Read all tracking refs once per branch.
        init_sha = _ref_sha(session_gitdir, f"refs/init/{branch}")
        synced_sha = _ref_sha(session_gitdir, f"refs/synced/{branch}")
        pushed_sha = _ref_sha(session_gitdir, f"refs/pushed/{branch}")

        # Skip refs the session never modified (still at init value)
        # or hasn't changed since last sync (still at synced value).
        # This applies to BOTH live sync and cleanup — without it,
        # a session that never touched a branch will force-forward its
        # stale ref on cleanup, reverting another session's amend.
        if sha == init_sha or sha == synced_sha:
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
            git_mod.git(session_gitdir, "update-ref",
                f"refs/synced/{branch}", sha, capture_output=True)
            continue

        existing_sha = existing.stdout.strip()
        if existing_sha == sha:
            continue  # already up to date

        # Ownership: can this session prove no other source modified
        # shared?  Three independent signals — any match suffices.
        session_owns = (existing_sha in (init_sha, synced_sha, pushed_sha))

        merge_base = git_mod.git(shared_gitdir, "merge-base",
                         existing_sha, sha,
                         capture_output=True, text=True)
        if (merge_base.returncode == 0
                and merge_base.stdout.strip() == existing_sha):
            # Fast-forward
            git_mod.git(shared_gitdir, "update-ref", ref, sha,
                capture_output=True)
            git_mod.git(session_gitdir, "update-ref",
                f"refs/synced/{branch}", sha, capture_output=True)
        elif (merge_base.returncode == 0
                  and merge_base.stdout.strip() == sha):
            continue  # session is behind shared, skip
        elif session_owns:
            # Session owns this branch (no other source modified shared).
            # Adopt session's version directly — handles amend, amend +
            # new commits, squash, message-only amend, any rewrite.
            git_mod.git(shared_gitdir, "update-ref", ref, sha,
                capture_output=True)
            git_mod.git(session_gitdir, "update-ref",
                f"refs/synced/{branch}", sha, capture_output=True)
        else:
            # Diverged, session doesn't own — try rebase, then
            # force-update (cleanup only) or skip (live sync).
            mb = (merge_base.stdout.strip()
                  if merge_base.returncode == 0 else None)

            rebased = _rebase_commits(
                shared_gitdir, existing_sha, sha, mb)
            if rebased:
                git_mod.git(shared_gitdir, "update-ref", ref, rebased,
                    capture_output=True)
                # Mark session as synced so next cycle doesn't re-rebase.
                git_mod.git(session_gitdir, "update-ref",
                    f"refs/synced/{branch}", sha, capture_output=True)
            elif force:
                # Final cleanup: force-update, back up old ref.
                git_mod.git(shared_gitdir, "update-ref",
                    f"refs/sessions/{sid}/{branch}", existing_sha,
                    capture_output=True)
                git_mod.git(shared_gitdir, "update-ref", ref, sha,
                    capture_output=True)
            else:
                # Non-force live sync (push/status): can't rebase,
                # skip.  Pull uses force=True so this path only fires
                # for push/status where we don't want to clobber.
                count = git_mod.git(shared_gitdir, "rev-list", "--count",
                    f"{mb}..{sha}" if mb else f"{existing_sha}..{sha}",
                    capture_output=True, text=True)
                n = (count.stdout.strip()
                     if count.returncode == 0 else "?")
                print(f"Session has {n} commit(s) on {branch} that "
                      f"diverged from shared (use 'pull' to force-sync).",
                      file=sys.stderr)

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


def forward_session_refs(slug, session_gitdir, shared_gitdir, *,
                         sid=None, force=False):
    """Forward refs from a live session into the shared gitdir.

    Called by sync_live_sessions during pull/push.  Temporarily rewrites
    alternates to host path for git operations, then restores cave path.

    When force=False (default, used by push/status): diverged refs that
    can't be rebased are skipped.
    When force=True (used by pull): diverged refs are force-updated with
    backup, same as final cleanup.
    """
    alt_file = session_gitdir / "objects" / "info" / "alternates"
    cave_path = None
    if alt_file.exists():
        cave_path = alt_file.read_text().strip()
        _atomic_write(alt_file, f"{shared_gitdir / 'objects'}\n")

    _forward_refs(sid, session_gitdir, shared_gitdir, force=force)

    # Restore cave-internal alternates path
    if cave_path is not None:
        _atomic_write(alt_file, f"{cave_path}\n")


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
            _atomic_write(alt_file, f"{shared_gitdir / 'objects'}\n")

        _forward_refs(sid, session_gitdir, shared_gitdir, force=True)

        # Save index and its loose objects so staging area survives
        # session restarts.  Loose objects in the session gitdir include
        # blobs from git-add that aren't reachable from any commit.
        _save_session_index(session_gitdir, shared_gitdir, slug)
