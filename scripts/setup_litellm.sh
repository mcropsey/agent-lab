#!/usr/bin/env bash
# Add agent-lab model aliases + a fallback route to the existing LiteLLM config.
# Idempotent, additive, backs up first, validates inside the container, rolls back on failure.
# Run on the LiteLLM host (.101).
set -euo pipefail
umask 077
CFG=${LITELLM_CONFIG:-/opt/litellm/config.yaml}
CONTAINER=${LITELLM_CONTAINER:-litellm}
HERE=$(cd "$(dirname "$0")" && pwd)

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
sudo cat "$CFG" | python3 "$HERE/_patch_litellm_config.py" \
  "$HERE/litellm-agentlab-models.yaml" "$HERE/litellm-agentlab-router.yaml" > "$TMP"

if sudo cmp -s "$CFG" "$TMP"; then
  echo "agent-lab blocks in $CFG are already current - nothing to do"
  exit 0
fi

BACKUP="$CFG.bak-agentlab-$(date +%Y%m%d-%H%M%S)"
sudo cp -p "$CFG" "$BACKUP"
echo "backup written: $BACKUP"

# tee rewrites the file in place: same inode, so the container's single-file bind mount sees it.
sudo tee "$CFG" < "$TMP" > /dev/null

restore() { echo "restoring backup"; sudo tee "$CFG" < "$BACKUP" > /dev/null; }

if ! sudo podman exec "$CONTAINER" python3 -c "
import yaml
c = yaml.safe_load(open('/app/config.yaml'))
names = [m['model_name'] for m in c['model_list']]
assert {'agentlab-main', 'agentlab-main-patient', 'qwen3.8-27b', 'qwen3.6-35b-a3b', 'nomic-embed-text'} <= set(names), names
assert not any(n.startswith('agentlab-') and n not in ('agentlab-main', 'agentlab-main-patient') for n in names), names
lab = [m for m in c['model_list'] if m['model_name'].startswith('agentlab-')]
assert all(m['litellm_params']['model'] == 'openai/qwen/qwen3.8-27b' for m in lab), 'agent-lab routes must only use qwen3.8-27b'
assert c['router_settings']['fallbacks']
print('config valid; models:', ', '.join(names))
"; then
  restore
  exit 1
fi

echo "restarting $CONTAINER ..."
sudo podman restart "$CONTAINER" > /dev/null
for i in $(seq 1 60); do
  if curl -sf -m 3 http://localhost:4000/health/liveliness > /dev/null; then
    echo "LiteLLM healthy after ~$((i * 3))s"
    exit 0
  fi
  sleep 3
done
echo "LiteLLM did not come back within 180s - restoring backup and restarting"
restore
sudo podman restart "$CONTAINER" > /dev/null
exit 1
