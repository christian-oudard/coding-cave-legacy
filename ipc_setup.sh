#!/bin/bash
# Write IPC metadata for cave-send session discovery.
# Sourced before exec-ing claude. $$ becomes claude's PID after exec.
_ipc="$HOME/ipc"
mkdir -p "$_ipc"
rm -f "$_ipc"/*.sock "$_ipc"/*.lock 2>/dev/null
printf '{"sid":"%s","index":"%s","project":"%s","started":%s,"pid":%s}\n' \
  "$CODING_CAVE_SESSION_ID" "$CODING_CAVE_SESSION_INDEX" \
  "$(basename "$CODING_CAVE_PROJECT_PATH")" "$(date +%s.%N)" "$$" \
  > "$_ipc/$CODING_CAVE_SESSION_ID.json"
