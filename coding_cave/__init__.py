"""coding-cave: per-session bwrap sandboxes with shared gitdirs."""
from ._state import (  # noqa: F401
    SANDBOX_BIN, SANDBOX_GPU_BIN, VERSION, JAIL_HOME,
    cave_data, set_cave_data, projects_file, projects_dir,
    nix_overlay_dir, sessions_dir, cave_home,
)
from .config import load_global_config, setup_env  # noqa: F401
from .projects import (  # noqa: F401
    load_projects, save_projects, list_projects, slugify,
    detect_project_from_cwd, ensure_project, cave_workdir, resolve_slug,
)
from .sessions import (  # noqa: F401
    session_id, register_session, list_sessions, cleanup_session,
)
from .git import (  # noqa: F401
    SAFE_GIT_OPTS, initialize_gitdir, ensure_cave_remote,
    mirror_origin_refs, finalize_sync, sync_refs, find_tree_merge_base,
    rebase_plumbing,
)
from .git import git as safe_git  # noqa: F401  (don't shadow the git module)
from .gitdir import (  # noqa: F401
    create_session_gitdir, forward_session_refs,
    merge_session_gitdirs,
)
from .overlay import (  # noqa: F401
    unmount_fuse, unmount_all_under, force_rmtree,
    reset_project_overlay, clean_stale_session_overlays,
)
from .lifecycle import (  # noqa: F401
    start_session, wait_for_child, find_bwrap_child,
    nsenter_shell_script, enter_session,
)
from .sync import (  # noqa: F401
    sync_live_sessions, pull_project, push_project,
    find_unsynced_project, pull_cave_project, project_sync_status,
)
from .commands import (  # noqa: F401
    cmd_agent, cmd_shell, cmd_add, cmd_remove, cmd_move,
    cmd_reset, cmd_pull, cmd_push, cmd_cp, cmd_status,
    _shell_exec_cmd,
)
from .cli import build_parser, main  # noqa: F401
