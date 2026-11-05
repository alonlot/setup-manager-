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
for t in iptables ip6tables; do  # the rule that blocks USB/IP from the network
  command -v $t >/dev/null || continue
  for target in "REJECT --reject-with tcp-reset" DROP; do  # DROP: the rule of older agents
    # shellcheck disable=SC2086
    while $t -D INPUT -p tcp --dport 3240 ! -i lo -m comment --comment setup-agent-usbip -j $target 2>/dev/null; do :; done
  done
  # All the other rules of the agent: the redirect to the filter for devices shared by hand, the rules
  # that let one client in, and the rule that keeps their connections.
  for chain in "filter INPUT" "nat PREROUTING"; do
    table="${chain% *}" name="${chain#* }"
    $t -t "$table" -S "$name" 2>/dev/null | grep -- "--comment setup-agent-usbip" | sed 's/^-A /-D /' |
      while read -r rule; do eval "$t -t $table $rule" 2>/dev/null || true; done
  done
done
[[ "${1:-}" == "--purge" ]] && rm -rf /etc/setup-agent
echo "setup-agent removed"
