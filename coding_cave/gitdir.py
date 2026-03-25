"""Per-session gitdir creation and merging."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import _state, git as git_mod

# Shell snippet appended to each cave hook: chains to a repo-local hook of the
# same name via cave.repoHooksPath (forwarded from the host's hooks config).
# The path may be absolute (for copied hooks) or relative (resolved against
# the worktree root).  Exits 0 if no repo hook is configured.
_CHAIN_SNIPPET = (
    '# Chain to repo-local hook\n'
    '_cave_hpath=$(git config cave.repoHooksPath 2>/dev/null) || exit 0\n'
    'case "$_cave_hpath" in\n'
    '    /*) ;;\n'
    '    *) _cave_hpath="$(git rev-parse --show-toplevel 2>/dev/null)/$_cave_hpath" ;;\n'
    'esac\n'
    '_rh="$_cave_hpath/$(basename "$0")"\n'
    '[ -x "$_rh" ] && exec "$_rh" "$@"\n'
    'exit 0\n'
)


def _session_label(sid):
    """Return a friendly label like 'coding-cave@2' for a session ID."""
    meta = _state.sessions_dir() / f"{sid}.json"
    try:
        info = json.loads(meta.read_text())
        return f"{info['project']}@{info['index']}"
    except (OSError, json.JSONDecodeError, KeyError):
        return sid


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


def _forward_repo_hooks(source_dir, slug, session_gitdir):
    """Forward host project's hook config into the session gitdir.

    If core.hooksPath is set, store it as cave.repoHooksPath (relative,
    resolved against the worktree at hook runtime).

    If core.hooksPath is not set, copy any executable hooks from the
    host's .git/hooks/ into session_gitdir/repo-hooks/ and store the
    absolute cave path.
    """
    result = subprocess.run(
        ["git", "-C", str(source_dir), "config", "--local",
         "core.hooksPath"],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        git_mod.git(session_gitdir, "config", "cave.repoHooksPath",
                    result.stdout.strip(), capture_output=True)
        return

    # Default hooks location: .git/hooks/
    host_hooks = Path(source_dir) / ".git" / "hooks"
    if not host_hooks.is_dir():
        return
    repo_hooks = session_gitdir / "repo-hooks"
    copied = False
    for hook in host_hooks.iterdir():
        if hook.is_file() and os.access(hook, os.X_OK):
            # Skip samples
            if hook.suffix == ".sample":
                continue
            if not copied:
                repo_hooks.mkdir(exist_ok=True)
                copied = True
            shutil.copy2(hook, repo_hooks / hook.name)
    if copied:
        # Absolute cave path: session gitdir is bind-mounted at
        # /projects/.gitdirs/<slug>
        git_mod.git(session_gitdir, "config", "cave.repoHooksPath",
                    f"/projects/.gitdirs/{slug}/repo-hooks",
                    capture_output=True)


def create_session_gitdir(slug, sid, overlay_base, *, source_dir=None):
    """Create a per-session gitdir with alternates pointing to shared objects.

    The session gitdir has its own HEAD, index, and refs but shares the object
    store with the shared gitdir via alternates.  Alternates starts with the
    host path; the caller rewrites it to the cave-internal path before bwrap,
    and merge_session_gitdirs rewrites it back for cleanup.

    source_dir: host project directory.  If provided, the host's
    core.hooksPath config is forwarded so cave hooks can chain to repo hooks.
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

    # Forward host project's hook configuration so cave hooks can chain to
    # repo-local hooks (e.g. .githooks/pre-commit with nixfmt --check).
    #
    # Two cases:
    #   1. core.hooksPath is set: store the relative path (resolved against
    #      the worktree at runtime).  These hooks live in the overlay.
    #   2. core.hooksPath is not set: hooks are in host's .git/hooks/ which
    #      isn't in the overlay.  Copy them to session_gitdir/repo-hooks/
    #      and store the absolute cave path.
    if source_dir is not None:
        _forward_repo_hooks(source_dir, slug, session_gitdir)

    # Post-commit hook: sync with shared like a developer pushing to origin.
    # 1. Try FF push (common case: no other session modified shared)
    # 2. If non-FF + we own the branch: force push (handles amends)
    # 3. If non-FF + someone else modified shared: rebase + push
    # 4. If rebase conflicts: abort, defer to cleanup
    # Always fetch at the end to keep tracking refs current.
    hooks = session_gitdir / "hooks"
    if hooks.exists():
        shutil.rmtree(hooks)
    hooks.mkdir()
    # Pre-commit hook: rebase onto shared/main if behind.
    # This integrates pushed commits (from cav push or other sessions)
    # before the commit, so the session stays current.
    # After cave logic, chains to any repo-local pre-commit hook.
    (hooks / "pre-commit").write_text(
        '#!/bin/bash\n'
        '[ -n "$_CAVE_HOOK_RUNNING" ] && exit 0\n'
        '_cave_pre_commit() {\n'
        '    branch=$(git symbolic-ref HEAD 2>/dev/null) || return\n'
        '    branch=${branch#refs/heads/}\n'
        '    git fetch shared 2>/dev/null\n'
        '    shared_sha=$(git rev-parse --verify '
        '"refs/remotes/shared/$branch" 2>/dev/null) || return\n'
        '    head_sha=$(git rev-parse HEAD 2>/dev/null) || return\n'
        '    [ "$shared_sha" = "$head_sha" ] && return\n'
        '    if git merge-base --is-ancestor "$head_sha" "$shared_sha" '
        '2>/dev/null; then\n'
        '        git rebase "$shared_sha" 2>/dev/null || '
        'git rebase --abort 2>/dev/null\n'
        '    fi\n'
        '}\n'
        '_cave_pre_commit\n'
        + _CHAIN_SNIPPET
    )
    os.chmod(hooks / "pre-commit", 0o755)
    # Post-commit hook: push/pull semantics with shared gitdir.
    #
    # On every commit, try to push to shared (fast-forward).  If shared
    # has moved (host pushed, or another session committed), rebase only
    # our new commits (since refs/pushed or refs/init) onto shared, then
    # push.  This is the session-side integration point — equivalent to
    # "git pull --rebase && git push" in a normal workflow.
    (hooks / "post-commit").write_text(
        '#!/bin/bash\n'
        '[ -n "$_CAVE_HOOK_RUNNING" ] && exit 0\n'
        'export _CAVE_HOOK_RUNNING=1\n'
        '_cave_post_commit() {\n'
        '    branch=$(git symbolic-ref HEAD 2>/dev/null) || return\n'
        '    branch=${branch#refs/heads/}\n'
        # Try fast-forward push
        '    pushed_ok=0\n'
        '    if git push shared "$branch" 2>/dev/null; then\n'
        '        git update-ref "refs/pushed/$branch" HEAD 2>/dev/null\n'
        '        pushed_ok=1\n'
        '    else\n'
        '        pushed_sha=$(git rev-parse --verify "refs/pushed/$branch" '
        '2>/dev/null)\n'
        '        git fetch shared 2>/dev/null\n'
        '        shared_sha=$(git rev-parse --verify '
        '"refs/remotes/shared/$branch" 2>/dev/null)\n'
        # Shared hasn't moved since our last push — we amended/rebased.
        # But only force-push if the host hasn't already pulled that commit.
        '        if [ -n "$pushed_sha" ] && [ "$shared_sha" = "$pushed_sha" ]'
        '; then\n'
        '            host_ref=$(git rev-parse --verify '
        '"refs/remotes/host/$branch" 2>/dev/null)\n'
        '            if [ -n "$host_ref" ] && git merge-base --is-ancestor '
        '"$shared_sha" "$host_ref" 2>/dev/null; then\n'
        '                echo >&2\n'
        '                echo "WARNING: Cannot force-push $branch. commit '
        '${shared_sha:0:8} was already pulled to the host." >&2\n'
        '                echo "Create a new commit instead of amending." >&2\n'
        '            else\n'
        '                git push shared +"$branch" 2>/dev/null &&\n'
        '                    git update-ref "refs/pushed/$branch" HEAD &&\n'
        '                    pushed_ok=1\n'
        '            fi\n'
        '        else\n'
        # Shared has moved.  Rebase only our new commits onto shared.
        # Use refs/pushed (last successful push) or refs/init (session
        # start point) as the base — never rebase old diverged history.
        '            base=${pushed_sha:-$(git rev-parse --verify '
        '"refs/init/$branch" 2>/dev/null)}\n'
        '            if [ -n "$base" ] && git rebase --onto '
        '"shared/$branch" "$base" 2>/dev/null; then\n'
        '                git push shared "$branch" 2>/dev/null &&\n'
        '                    git update-ref "refs/pushed/$branch" HEAD &&\n'
        '                    pushed_ok=1\n'
        '            else\n'
        '                git rebase --abort 2>/dev/null\n'
        '                echo >&2\n'
        '                echo "WARNING: Could not auto-integrate with '
        'shared/$branch (conflict)." >&2\n'
        '                echo "Your commit is saved locally but not '
        'synced to shared." >&2\n'
        '                echo "Run: git fetch shared && '
        'git rebase shared/$branch" >&2\n'
        '            fi\n'
        '        fi\n'
        '    fi\n'
        # Notify sibling sessions on the same project
        '    if [ "$pushed_ok" = 1 ] && [ -z "$CODING_CAVE_NO_SEND" ] '
        '&& command -v cave-send >/dev/null; then\n'
        '        cave-send --all-project "Pushed to $branch, run: '
        'git fetch shared && git rebase shared/$branch" '
        '</dev/null 2>/dev/null &\n'
        '    fi\n'
        '    git fetch shared 2>/dev/null\n'
        '}\n'
        '_cave_post_commit\n'
        'unset _CAVE_HOOK_RUNNING\n'
        + _CHAIN_SNIPPET
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
    # Fetches from shared first to close the race window where the user
    # runs `cav pull` after the session's last commit but before amend.
    (hooks / "prepare-commit-msg").write_text(
        '#!/bin/bash\n'
        '_cave_prepare_commit_msg() {\n'
        '    parent_cmd=$(tr "\\0" " " < /proc/$PPID/cmdline 2>/dev/null)\n'
        '    echo "$parent_cmd" | grep -q -- "--amend" || return 0\n'
        '    git fetch shared 2>/dev/null\n'
        '    head_sha=$(git rev-parse HEAD 2>/dev/null) || return 0\n'
        '    pulled_refs=$(git for-each-ref --format="%(objectname)" '
        'refs/remotes/host/ 2>/dev/null)\n'
        '    [ -z "$pulled_refs" ] && return 0\n'
        '    for pulled in $pulled_refs; do\n'
        '        if git merge-base --is-ancestor "$head_sha" "$pulled" '
        '2>/dev/null; then\n'
        '            echo >&2\n'
        '            echo "ERROR: Cannot amend. commit '
        '${head_sha:0:8} was already pulled to the host." >&2\n'
        '            echo "Create a new commit instead." >&2\n'
        '            return 1\n'
        '        fi\n'
        '    done\n'
        '}\n'
        '_cave_prepare_commit_msg || exit 1\n'
        + _CHAIN_SNIPPET
    )
    os.chmod(hooks / "prepare-commit-msg", 0o755)

    # Layer 2: pre-rebase blocks rebase of pulled commits.
    # Bypassed by --no-verify, but post-rewrite catches that.
    # Fetches from shared to get latest pulled refs (same race as layer 1).
    (hooks / "pre-rebase").write_text(
        '#!/bin/bash\n'
        '_cave_pre_rebase() {\n'
        '    upstream=$1\n'
        '    branch=${2:-HEAD}\n'
        '    git fetch shared 2>/dev/null\n'
        '    pulled_refs=$(git for-each-ref --format="%(objectname)" '
        'refs/remotes/host/ 2>/dev/null)\n'
        '    [ -z "$pulled_refs" ] && return 0\n'
        '    for commit in $(git rev-list "$upstream..$branch" 2>/dev/null); do\n'
        '        for pulled in $pulled_refs; do\n'
        '            if git merge-base --is-ancestor "$commit" "$pulled" '
        '2>/dev/null; then\n'
        '                echo >&2\n'
        '                echo "ERROR: Cannot rebase. commit '
        '${commit:0:8} was already pulled to the host." >&2\n'
        '                echo "Create new commits instead." >&2\n'
        '                return 1\n'
        '            fi\n'
        '        done\n'
        '    done\n'
        '}\n'
        '_cave_pre_rebase "$@" || exit 1\n'
        + _CHAIN_SNIPPET
    )
    os.chmod(hooks / "pre-rebase", 0o755)

    # Layer 3: post-rewrite undoes rewrites that slip past layers 1-2.
    # Cannot be bypassed with --no-verify.
    # Fetches from shared to get latest pulled refs (same race as layer 1).
    # post-rewrite reads rewrite mappings from stdin.  Save stdin so we can
    # replay it for a chained repo hook.
    (hooks / "post-rewrite").write_text(
        '#!/bin/bash\n'
        '_cave_stdin=$(cat)\n'
        '_cave_post_rewrite() {\n'
        '    action=$1\n'
        '    git fetch shared 2>/dev/null\n'
        '    pulled_refs=$(git for-each-ref --format="%(objectname)" '
        'refs/remotes/host/ 2>/dev/null)\n'
        '    [ -z "$pulled_refs" ] && return 0\n'
        '    while read old_sha new_sha extra; do\n'
        '        for pulled in $pulled_refs; do\n'
        '            if git merge-base --is-ancestor "$old_sha" "$pulled" '
        '2>/dev/null; then\n'
        '                echo >&2\n'
        '                echo "ERROR: Cannot $action. commit '
        '${old_sha:0:8} was already pulled to the host." >&2\n'
        '                echo "Create a new commit instead." >&2\n'
        '                if [ "$action" = "amend" ]; then\n'
        '                    git reset --soft "$old_sha" 2>/dev/null\n'
        '                else\n'
        '                    git reset --hard ORIG_HEAD 2>/dev/null\n'
        '                fi\n'
        '                return 1\n'
        '            fi\n'
        '        done\n'
        '    done <<< "$_cave_stdin"\n'
        '}\n'
        '_cave_post_rewrite "$@" || exit 1\n'
        # Chain variant: replay saved stdin to the repo hook via pipe.
        '_cave_hpath=$(git config cave.repoHooksPath 2>/dev/null) || exit 0\n'
        'case "$_cave_hpath" in\n'
        '    /*) ;;\n'
        '    *) _cave_hpath="$(git rev-parse --show-toplevel 2>/dev/null)/$_cave_hpath" ;;\n'
        'esac\n'
        '_rh="$_cave_hpath/$(basename "$0")"\n'
        'if [ -x "$_rh" ]; then\n'
        '    echo "$_cave_stdin" | "$_rh" "$@"\n'
        '    exit $?\n'
        'fi\n'
        'exit 0\n'
    )
    os.chmod(hooks / "post-rewrite", 0o755)

    # Passthrough stubs for every client-side hook type the cave doesn't
    # define its own logic for.  Git only looks in $GIT_DIR/hooks/, so
    # without a stub the repo's hook is invisible when core.hooksPath
    # points elsewhere.
    for stub in ("applypatch-msg", "pre-applypatch", "post-applypatch",
                 "commit-msg", "pre-merge-commit", "post-merge",
                 "pre-push", "post-checkout", "post-index-change",
                 "sendemail-validate"):
        (hooks / stub).write_text('#!/bin/bash\n' + _CHAIN_SNIPPET)
        os.chmod(hooks / stub, 0o755)

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
    """Rebase session commits onto shared HEAD using plumbing.

    Returns the new tip SHA if all merges succeed, None on conflict.
    Uses patch-id to detect already-applied commits on conflict.
    """
    git_cmd = ["git", "--git-dir", str(gitdir)] + git_mod.SAFE_GIT_OPTS

    def on_conflict(commit):
        return _is_patch_upstream(gitdir, commit, merge_base, shared_sha)

    return git_mod.rebase_plumbing(
        git_cmd, shared_sha, merge_base, session_sha,
        on_conflict=on_conflict)


def _ref_sha(gitdir, ref):
    """Return the SHA a ref points to, or None if it doesn't exist."""
    r = git_mod.git(gitdir, "rev-parse", ref,
                    capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _forward_refs(sid, session_gitdir, shared_gitdir, *, force=False,
                  protected_refs=None):
    """Forward branch refs from a session gitdir into the shared gitdir.

    Handles fast-forwards, re-authoring detection, rebase-onto-reauthored,
    and general rebase (when shared moved forward while session was working).

    When force=False (live sync): never force-updates, never moves refs
    backward.  Diverged refs that can't be resolved by fast-forward or
    rebase are skipped -- they'll be picked up on final cleanup.

    When force=True (final cleanup): force-updates diverged refs and backs
    up the old shared ref under refs/sessions/<sid>/<branch>.

    protected_refs: set of branch names that another session already
    force-updated in this sync cycle.  Force-update is suppressed for
    these to prevent last-writer-wins races.
    """
    if protected_refs is None:
        protected_refs = set()
    label = _session_label(sid)
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
            # But don't overwrite if the host already pulled the existing
            # ref — that would force `pull --force` on the host.
            host_sha = _ref_sha(shared_gitdir,
                                f"refs/remotes/host/{branch}")
            if (host_sha and merge_base.returncode == 0
                    and git_mod.git(shared_gitdir, "merge-base",
                        "--is-ancestor", existing_sha, host_sha,
                        capture_output=True).returncode == 0):
                # Host has this commit. Create a new commit with the
                # session's tree on top of the existing shared ref.
                # This always works (no conflicts) and preserves the
                # host's ability to fast-forward.
                tree = git_mod.git(shared_gitdir, "rev-parse",
                    f"{sha}^{{tree}}", capture_output=True, text=True)
                if tree.returncode == 0:
                    msg = git_mod.git(shared_gitdir, "log", "-1",
                        "--format=%B", sha,
                        capture_output=True, text=True)
                    new = subprocess.run(
                        ["git", "--git-dir", str(shared_gitdir),
                         "commit-tree", tree.stdout.strip(),
                         "-p", existing_sha,
                         "-m", msg.stdout.strip()],
                        capture_output=True, text=True,
                        env={**os.environ,
                             "GIT_AUTHOR_NAME": "cave-coder",
                             "GIT_AUTHOR_EMAIL": "cave@localhost",
                             "GIT_COMMITTER_NAME": "cave-coder",
                             "GIT_COMMITTER_EMAIL": "cave@localhost"},
                    )
                    if new.returncode == 0:
                        git_mod.git(shared_gitdir, "update-ref", ref,
                            new.stdout.strip(), capture_output=True)
                        git_mod.git(session_gitdir, "update-ref",
                            f"refs/synced/{branch}", sha,
                            capture_output=True)
            else:
                # Host hasn't pulled — safe to adopt directly.
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
            elif force and branch not in protected_refs:
                # Diverged, can't rebase.  Back up session's tip so the
                # work isn't lost when the session gitdir is cleaned up.
                backup_ref = f"refs/sessions/{sid}/{branch}"
                git_mod.git(shared_gitdir, "update-ref", backup_ref, sha,
                    capture_output=True)
                protected_refs.add(branch)
                print(f"WARNING: {label} had commits on "
                      f"{branch} that couldn't be rebased "
                      f"(backed up to {backup_ref}).",
                      file=sys.stderr)
            else:
                # Non-force live sync (push/status): can't rebase,
                # skip.  Pull uses force=True so this path only fires
                # for push/status where we don't want to clobber.
                count = git_mod.git(shared_gitdir, "rev-list", "--count",
                    f"{mb}..{sha}" if mb else f"{existing_sha}..{sha}",
                    capture_output=True, text=True)
                n = (count.stdout.strip()
                     if count.returncode == 0 else "?")
                print(f"{label} has {n} commit(s) on {branch} that "
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
                         sid=None, force=False, protected_refs=None):
    """Forward refs from a live session into the shared gitdir.

    Called by sync_live_sessions during pull/push.  Temporarily rewrites
    alternates to host path for git operations, then restores cave path.

    When force=False (default, used by push/status): diverged refs that
    can't be rebased are skipped.
    When force=True (used by pull): diverged refs are force-updated with
    backup, same as final cleanup.

    protected_refs: shared set tracking branches already force-updated
    in this sync cycle.  Prevents last-writer-wins when multiple sessions
    have diverged refs for the same branch.
    """
    alt_file = session_gitdir / "objects" / "info" / "alternates"
    cave_path = None
    if alt_file.exists():
        cave_path = alt_file.read_text().strip()
        _atomic_write(alt_file, f"{shared_gitdir / 'objects'}\n")

    _forward_refs(sid, session_gitdir, shared_gitdir, force=force,
                  protected_refs=protected_refs)

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
