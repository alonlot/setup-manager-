#!/usr/bin/env bash
# Remove the Setup Manager agent. Add --purge to also delete /etc/setup-agent.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "run as root" >&2; exit 1; fi
systemctl disable --now setup-agent.service 2>/dev/null || true
systemctl disable --now setup-usbipd.service 2>/dev/null || true
rm -f /etc/systemd/system/setup-agent.service /etc/systemd/system/setup-usbipd.service
rm -f /etc/modules-load.d/setup-agent.conf
systemctl daemon-reload
rm -rf /opt/setup-agent
[[ "${1:-}" == "--purge" ]] && rm -rf /etc/setup-agent
echo "setup-agent removed"
