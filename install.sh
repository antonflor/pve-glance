#!/usr/bin/env bash
# pve-glance installer — run as root on a Proxmox VE node.
set -euo pipefail

DEST=/opt/pve-glance
UNIT=/etc/systemd/system/pve-glance.service
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "error: run as root (needed to install the systemd unit)." >&2
  exit 1
fi
if ! command -v pvesh >/dev/null 2>&1; then
  echo "error: 'pvesh' not found — this does not look like a Proxmox VE node." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found." >&2
  exit 1
fi

mkdir -p "$DEST"
install -m 0755 "$SRC/pve-glance.py" "$DEST/pve-glance.py"
install -m 0644 "$SRC/pve-glance.service" "$UNIT"

systemctl daemon-reload
systemctl enable --now pve-glance.service

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "pve-glance installed and started."
echo "  → http://${IP:-<node-ip>}:8008"
echo
echo "Manage it with:"
echo "  systemctl status pve-glance     # check it's running"
echo "  journalctl -u pve-glance -f     # follow logs"
echo "  systemctl restart pve-glance    # after editing config in $UNIT"
