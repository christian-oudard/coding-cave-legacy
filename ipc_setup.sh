#!/bin/bash
# Write IPC metadata for cave-send session discovery.
# Sourced before exec-ing claude.
_ipc="$HOME/ipc"
mkdir -p "$_ipc"
# Acquire a flock on a per-session lock file via fd 9.
# The lock is inherited through exec and released when the process exits.
# Other sessions check liveness by attempting a non-blocking flock.
exec 9>"$_ipc/$CODING_CAVE_SESSION_ID.lock"
flock -n 9
printf '{"sid":"%s","index":"%s","project":"%s","started":%s}\n' \
  "$CODING_CAVE_SESSION_ID" "$CODING_CAVE_SESSION_INDEX" \
  "$(basename "$CODING_CAVE_PROJECT_PATH")" "$(date +%s.%N)" \
  > "$_ipc/$CODING_CAVE_SESSION_ID.json"
