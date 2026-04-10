#!/bin/sh
# cave-ns.sh - Namespace setup helper for Coding Cave sandboxes.
#
# Runs inside a fresh user+mount+net namespace created by unshare(1).
# Signals the parent that the namespace is ready, mounts the nix store
# overlay if needed, then execs the bwrap sandbox wrapper.
#
# Environment variables (set by sandbox.py):
#   CAVE_NS_SYNC_FD           - fd to write sync byte to
#   CAVE_NS_OVERLAY_MERGED    - fuse-overlayfs merged mountpoint
#   CAVE_NS_OVERLAY_UPPER     - fuse-overlayfs upper dir
#   CAVE_NS_OVERLAY_WORK      - fuse-overlayfs work dir
#   CAVE_NS_MOUNTPOINT        - absolute path to mountpoint binary
#   CAVE_NS_FUSE_OVERLAYFS    - absolute path to fuse-overlayfs binary
#   CAVE_NS_FUSERMOUNT        - absolute path to fusermount/fusermount3
set -e

# Signal parent: namespace exists, safe to apply iptables + slirp4netns.
printf '1' >&"$CAVE_NS_SYNC_FD"
eval exec "$CAVE_NS_SYNC_FD>&-"

# Mount nix store overlay if the direct mount didn't succeed.
if [ -n "$CAVE_NS_OVERLAY_MERGED" ] && [ -n "$CAVE_NS_MOUNTPOINT" ] \
   && ! "$CAVE_NS_MOUNTPOINT" -q "$CAVE_NS_OVERLAY_MERGED"; then
    # fuse-overlayfs needs fusermount on PATH to register the mount.
    if [ -n "$CAVE_NS_FUSERMOUNT" ]; then
        export PATH="$(dirname "$CAVE_NS_FUSERMOUNT"):$PATH"
    fi
    "$CAVE_NS_FUSE_OVERLAYFS" \
        -o "lowerdir=/nix/store,upperdir=$CAVE_NS_OVERLAY_UPPER,workdir=$CAVE_NS_OVERLAY_WORK" \
        "$CAVE_NS_OVERLAY_MERGED"
fi

exec "$@"
