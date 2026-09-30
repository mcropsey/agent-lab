#!/usr/bin/env bash
# Install/refresh the Agent Lab dashboard on the Grafana hub (.98). Touches only the "Agent Lab" folder;
# Grafana's file provisioner picks it up within ~30s, no restart.
set -euo pipefail
cd "$(dirname "$0")"
HUB=${GRAFANA_HOST:-192.168.1.98}
DIR="/opt/monitoring/grafana/dashboards/Agent Lab"

python3 gen_dashboard.py agent-lab.json > /dev/null
ssh -o BatchMode=yes "$HUB" "sudo mkdir -p '$DIR' && sudo tee '$DIR/agent-lab.json' > /dev/null && sudo chown -R 472:0 '$DIR'" < agent-lab.json
echo "installed: $HUB:$DIR/agent-lab.json -> http://$HUB:3000/d/agent-lab"
