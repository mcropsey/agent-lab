#!/usr/bin/env bash
# Create/update one LiteLLM virtual key per persona (idempotent) and store them in ./.env.
# Run on the LiteLLM host (.101). Keys are never printed - only alias, action and last 4 chars.
set -euo pipefail
umask 077
ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$ROOT/.env"
CONTAINER=${LITELLM_CONTAINER:-litellm}
touch "$ENV_FILE"

OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT

# Payload = key spec + any keys we already hold (so reruns update instead of duplicating).
python3 - "$ROOT/scripts/keys.json" "$ENV_FILE" <<'EOF' |
import json, sys
spec = json.load(open(sys.argv[1]))
env = dict(l.split("=", 1) for l in open(sys.argv[2]).read().splitlines() if "=" in l and not l.startswith("#"))
spec["existing"] = {k["alias"]: env[k["env"]] for k in spec["keys"] if env.get(k["env"])}
json.dump(spec, sys.stdout)
EOF
sudo podman exec -i "$CONTAINER" python3 -c "$(cat "$ROOT/scripts/_keys_in_container.py")" > "$OUT"

python3 - "$ROOT/scripts/keys.json" "$ENV_FILE" "$OUT" <<'EOF'
import json, sys
spec = json.load(open(sys.argv[1]))
result = json.load(open(sys.argv[3]))
lines = open(sys.argv[2]).read().splitlines()
values = {k["env"]: result["keys"][k["alias"]]["key"] for k in spec["keys"]}
values.setdefault("OPENAI_BASE_URL", "http://192.168.1.101:4000/v1")
kept = [l for l in lines if l.split("=", 1)[0] not in values]
if not any(l.startswith("OPENAI_BASE_URL=") for l in kept):
    kept.insert(0, "OPENAI_BASE_URL=" + values.pop("OPENAI_BASE_URL"))
else:
    values.pop("OPENAI_BASE_URL")
open(sys.argv[2], "w").write("\n".join(kept + [f"{k}={v}" for k, v in values.items()]) + "\n")
print(f"team agent-lab: {result['team_id']}  (read back: {result.get('team_check')})")
for k in spec["keys"]:
    r = result["keys"][k["alias"]]
    agent = result.get("agents", {}).get(k["alias"])
    print(f"  {k['alias']:<16} {r['action']:<9} ...{r['key'][-4:]}  rpm={k['rpm_limit']} tpm={k['tpm_limit']} "
          f"budget=${k['max_budget']}/day  agent={agent or '-'}")
EOF
echo "keys stored in $ENV_FILE (mode $(stat -c %a "$ENV_FILE"))"
