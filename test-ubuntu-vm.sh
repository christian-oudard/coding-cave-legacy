#!/usr/bin/env bash
# Test coding-cave build + install on a fresh Ubuntu VM.
# Run from the NixOS host (not inside a cave).
# Requires: qemu, cloud-utils (cloud-localds), openssh
#
# Usage: ./test-ubuntu-vm.sh <path-to-cloud-image.img>
set -euo pipefail

WIPE=0
if [ "${1:-}" = "--wipe" ]; then WIPE=1; shift; fi

if [ $# -ne 1 ] || [ ! -f "$1" ]; then
    echo "Usage: $0 [--wipe] <cloud-image.img>"
    exit 1
fi

BASE_IMG="$(realpath "$1")"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK_DIR="$(dirname "$BASE_IMG")"
IMG_BASE="$(basename "$BASE_IMG" .img)"
WORK_IMG="${WORK_DIR}/${IMG_BASE}.overlay.qcow2"
LOGIN_ISO="${WORK_DIR}/${IMG_BASE}.login.iso"
SSH_KEY="${WORK_DIR}/vm_key"
SSH_PORT=2222

cleanup() {
    if [ -f "${WORK_DIR}/qemu.pid" ]; then
        kill "$(cat "${WORK_DIR}/qemu.pid")" 2>/dev/null || true
        rm -f "${WORK_DIR}/qemu.pid"
    fi
}
trap cleanup EXIT

ssh_vm() {
    ssh -q \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -i "$SSH_KEY" -p "$SSH_PORT" \
        ubuntu@localhost "$@"
}

scp_vm() {
    scp -q \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -i "$SSH_KEY" -P "$SSH_PORT" \
        "$@"
}

# SSH key for scripted access
[ -f "$SSH_KEY" ] || ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -q

# Overlay disk (reuse existing unless --fresh)
if [ "$WIPE" -eq 1 ] || [ ! -f "$WORK_IMG" ]; then
    rm -f "$WORK_IMG"
    qemu-img create -f qcow2 -b "$BASE_IMG" -F qcow2 "$WORK_IMG" 20G
fi

# Cloud-init seed ISO
PUB_KEY=$(cat "${SSH_KEY}.pub")
CI_DIR=$(mktemp -d)
cat > "$CI_DIR/user-data" << EOF
#cloud-config
users:
  - name: ubuntu
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
    plain_text_passwd: ubuntu
    ssh_authorized_keys:
      - ${PUB_KEY}
ssh_pwauth: true
chpasswd:
  expire: false
EOF
cat > "$CI_DIR/meta-data" << 'EOF'
instance-id: coding-cave-test
local-hostname: cave-test
EOF
cloud-localds "$LOGIN_ISO" "$CI_DIR/user-data" "$CI_DIR/meta-data"
rm -rf "$CI_DIR"

# Archive project source (committed state)
echo "Archiving source..."
git -C "$PROJECT_DIR" archive --format=tar.gz HEAD > "${WORK_DIR}/source.tar.gz"

# Start VM (reuse if already running)
vm_running() {
    [ -f "${WORK_DIR}/qemu.pid" ] && kill -0 "$(cat "${WORK_DIR}/qemu.pid")" 2>/dev/null
}
if vm_running; then
    echo "VM already running."
else
    echo "Starting VM..."
qemu-system-x86_64 \
    -enable-kvm \
    -m 4G -smp 2 \
    -drive file="$WORK_IMG",if=virtio \
    -cdrom "$LOGIN_ISO" \
    -nic user,model=virtio-net-pci,hostfwd=tcp::${SSH_PORT}-:22 \
    -display none \
    -daemonize \
    -pidfile "${WORK_DIR}/qemu.pid"
fi

# Wait for SSH
echo "Waiting for VM to boot..."
for i in $(seq 1 60); do
    if ssh_vm true 2>/dev/null; then break; fi
    if [ "$i" -eq 60 ]; then echo "Timed out waiting for SSH"; exit 1; fi
    sleep 2
done

echo "Waiting for cloud-init..."
ssh_vm sudo cloud-init status --wait

# Copy source into VM
echo "Copying source..."
scp_vm "${WORK_DIR}/source.tar.gz" ubuntu@localhost:~/source.tar.gz

# Install nix, build cav, show version
echo "Installing nix and building coding-cave..."
ssh_vm bash << 'REMOTE'
set -euxo pipefail

# Install nix (single-user, no daemon)
if ! command -v nix &>/dev/null; then
    curl -L https://nixos.org/nix/install | sh -s -- --no-daemon
fi
. ~/.nix-profile/etc/profile.d/nix.sh

# Enable flakes
mkdir -p ~/.config/nix
echo "experimental-features = nix-command flakes" > ~/.config/nix/nix.conf

# Extract source and init git repo (nix flakes require one)
cd ~/coding-cave 2>/dev/null || { mkdir ~/coding-cave && cd ~/coding-cave; }
rm -rf ./*
tar xzf ~/source.tar.gz
if [ ! -d .git ]; then
    git init
    git config user.email "test@test.com"
    git config user.name "Test"
fi
git add -A
git commit -q -m "test build" --allow-empty

# Build
nix build

# Show version
./result/bin/cav version
REMOTE

echo "Done."
