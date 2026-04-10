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
if [ -n "$CAVE_NS_OVERLAY_MERGED" ] && [ -n "$CAVE_NS_FUSE_OVERLAYFS" ]; then
    need_mount=0

    if [ -n "$CAVE_NS_MOUNTPOINT" ]; then
        if ! "$CAVE_NS_MOUNTPOINT" -q "$CAVE_NS_OVERLAY_MERGED"; then
            need_mount=1
        else
            # Mounted, but verify it's functional via a write probe.
            probe="$CAVE_NS_OVERLAY_MERGED/.overlay_probe"
            if touch "$probe" 2>/dev/null && rm "$probe" 2>/dev/null; then
                need_mount=0
            else
                # Stale mount. Unmount before remounting.
                if [ -n "$CAVE_NS_FUSERMOUNT" ]; then
                    "$CAVE_NS_FUSERMOUNT" -u "$CAVE_NS_OVERLAY_MERGED" 2>/dev/null || true
                fi
                need_mount=1
            fi
        fi
    else
        # No mountpoint binary, assume we need to mount.
        need_mount=1
    fi

    if [ "$need_mount" = 1 ]; then
        if [ -n "$CAVE_NS_FUSERMOUNT" ]; then
            export PATH="${CAVE_NS_FUSERMOUNT%/*}:$PATH"
        fi
        # squash_to_uid must match the namespace uid, not the parent's.
        # unshare --map-user=0 remaps the parent uid to 0 here. Using
        # the parent uid would reference an unmapped uid (shows as 65534).
        opts="lowerdir=/nix/store,upperdir=$CAVE_NS_OVERLAY_UPPER,workdir=$CAVE_NS_OVERLAY_WORK"
        opts="$opts,squash_to_uid=0,squash_to_gid=0"
        "$CAVE_NS_FUSE_OVERLAYFS" -o "$opts" "$CAVE_NS_OVERLAY_MERGED"
    fi
fi

exec "$@"
