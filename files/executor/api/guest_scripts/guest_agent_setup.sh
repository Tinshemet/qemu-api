#!/usr/bin/env bash
# Installs and enables qemu-guest-agent so the host can run commands in this VM.
set -e
echo "[guest-agent] installing qemu-guest-agent..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y qemu-guest-agent
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y qemu-guest-agent
elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm qemu-guest-agent
else
    echo "ERROR: no supported package manager found (apt/dnf/pacman)." >&2
    exit 1
fi
# The unit is qemu-guest-agent on most distros, qemu-ga on some.
sudo systemctl enable --now qemu-guest-agent 2>/dev/null \
  || sudo systemctl enable --now qemu-ga 2>/dev/null \
  || echo "[guest-agent] enable the qemu-guest-agent service manually if it did not start."
echo "[guest-agent] done."
