#!/usr/bin/env bash
# Remove pve-glance. Run as root on the node where it was installed.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "run as root" >&2; exit 1; fi

systemctl disable --now pve-glance.service 2>/dev/null || true
rm -f /etc/systemd/system/pve-glance.service
rm -rf /opt/pve-glance
systemctl daemon-reload
echo "pve-glance removed."
