#!/usr/bin/env bash
# Stop agent-lab on every host in hosts.conf (or just the IPs given). Code, venvs, keys and logs stay in place;
# ./deploy.sh starts it again. For a pause without stopping processes, set global.enabled: false in
# config/lab.yaml and run ./deploy.sh config.
set -euo pipefail
cd "$(dirname "$0")"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=5)

while read -r ip runtime personas <&3; do
  if [[ $# -gt 0 && ! " $* " =~ " $ip " ]]; then continue; fi
  if [[ $runtime == compose ]]; then
    "${SSH[@]}" "$ip" 'cd ~/agent-lab 2>/dev/null && docker compose down 2>&1 | tail -1 || echo "  not deployed"'
  else
    "${SSH[@]}" "$ip" 'sudo systemctl disable --now agentlab 2>/dev/null; echo "  agentlab.service: $(systemctl is-active agentlab 2>/dev/null || true)"'
  fi
  echo "stopped $ip ($personas)"
done 3< <(grep -vE '^[[:space:]]*(#|$)' hosts.conf)
