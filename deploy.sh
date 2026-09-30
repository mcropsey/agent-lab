#!/usr/bin/env bash
# Deploy and operate agent-lab on the hosts listed in hosts.conf. Run from the LiteLLM host (.101).
#
#   ./deploy.sh [up] [ip...]          sync code + config + that host's keys, build/install, (re)start  (default: all)
#   ./deploy.sh config                push config/lab.yaml to every host (applies on each scheduler's next wake-up)
#   ./deploy.sh run <persona> [--no-errors]   run one persona once, now, on the host that owns it
#   ./deploy.sh status                what runs where + each host's latest run lines
#   ./deploy.sh logs <ip> [lines]     recent JSON log lines from one host
#
# Safe to rerun: syncing overwrites code in place, keeps the venv, and restarts only what it deployed.
set -euo pipefail
cd "$(dirname "$0")"

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=5)
COMPOSE_VERSION=v5.5.1

die() { echo "error: $*" >&2; exit 1; }
hosts() { grep -vE '^[[:space:]]*(#|$)' hosts.conf; }
line_for_ip() { hosts | awk -v h="$1" '$1 == h'; }
owner_of() { hosts | awk -v p="$1" '{ n = split($3, a, ","); for (i = 1; i <= n; i++) if (a[i] == p) print $1, $2 }'; }

[[ -f .env ]] || die ".env missing - run scripts/setup_keys.sh first"

key_vars() {
  local p out=()
  IFS=, read -ra ps <<< "$1"
  for p in "${ps[@]}"; do
    case $p in
      conversation) out+=(AGENTLAB_KEY_CUSTOMER AGENTLAB_KEY_SUPPORT) ;;
      *) out+=("AGENTLAB_KEY_${p^^}") ;;
    esac
  done
  echo "${out[@]}"
}

write_host_env() {  # <ip> <personas> -> pipes this host's .env (only the keys it needs) to the host
  local ip=$1 personas=$2 name v content
  name=$("${SSH[@]}" "$ip" hostname -s)
  content="PERSONAS=$personas"$'\n'"AGENTLAB_HOST=$name"$'\n'"$(grep -E '^OPENAI_BASE_URL=' .env)"
  for v in $(key_vars "$personas"); do
    grep -qE "^$v=" .env || die "$v missing from .env - run scripts/setup_keys.sh"
    content+=$'\n'"$(grep -E "^$v=" .env)"
  done
  printf '%s\n' "$content" | "${SSH[@]}" "$ip" 'umask 077; mkdir -p ~/agent-lab && cat > ~/agent-lab/.env'
}

sync_code() {
  tar czf - --exclude=__pycache__ agentlab config data requirements.txt Dockerfile docker-compose.yml systemd \
    |"${SSH[@]}" "$1" 'mkdir -p ~/agent-lab && cd ~/agent-lab && rm -rf agentlab data && tar xzf -'
}

start_compose() {
  "${SSH[@]}" "$1" bash -s "$COMPOSE_VERSION" <<'EOF'
set -euo pipefail
if ! docker compose version > /dev/null 2>&1; then
  echo "  installing docker compose plugin $1 for this user (~/.docker/cli-plugins)"
  mkdir -p ~/.docker/cli-plugins
  curl -fsSL -o ~/.docker/cli-plugins/docker-compose \
    "https://github.com/docker/compose/releases/download/$1/docker-compose-linux-$(uname -m)"
  chmod +x ~/.docker/cli-plugins/docker-compose
fi
cd ~/agent-lab
docker compose up -d --build --quiet-pull 2>&1 | grep -vE '^\s*$' | tail -3
EOF
}

start_venv() {
  "${SSH[@]}" "$1" bash -s <<'EOF'
set -euo pipefail
cd ~/agent-lab
if ! python3 -c 'import ensurepip' 2> /dev/null; then
  echo "  installing python3-venv"
  sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq python3-venv > /dev/null 2>&1
fi
[[ -x .venv/bin/python ]] || python3 -m venv .venv
hash=$(sha256sum requirements.txt | cut -c1-16)
if [[ "$(cat .venv/.req-hash 2> /dev/null)" != "$hash" ]]; then
  echo "  installing Python dependencies"
  .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt
  echo "$hash" > .venv/.req-hash
fi
sed "s|@HOME@|$HOME|g; s|@USER@|$USER|g" systemd/agentlab.service | sudo tee /etc/systemd/system/agentlab.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable agentlab > /dev/null 2>&1
sudo systemctl restart agentlab
echo "  agentlab.service: $(systemctl is-active agentlab)"
EOF
}

cmd_up() {
  local targets=("$@") ip runtime personas
  [[ ${#targets[@]} -gt 0 ]] || mapfile -t targets < <(hosts | awk '{print $1}')
  for ip in "${targets[@]}"; do
    read -r _ runtime personas <<< "$(line_for_ip "$ip")"
    [[ -n "${runtime:-}" ]] || die "$ip is not in hosts.conf"
    echo "== $ip ($runtime: $personas)"
    sync_code "$ip"
    write_host_env "$ip" "$personas"
    "start_$runtime" "$ip"
  done
}

cmd_config() {
  local ip
  for ip in $(hosts | awk '{print $1}'); do
    "${SSH[@]}" "$ip" 'mkdir -p ~/agent-lab/config && cat > ~/agent-lab/config/lab.yaml' < config/lab.yaml
    echo "pushed config/lab.yaml -> $ip"
  done
}

cmd_run() {
  local persona=${1:-} flag=${2:-} owner ip runtime
  [[ -n "$persona" ]] || die "usage: ./deploy.sh run <persona> [--no-errors]"
  owner=$(owner_of "$persona")
  [[ -n "$owner" ]] || die "no host in hosts.conf runs '$persona'"
  read -r ip runtime <<< "$owner"
  echo "== $persona on $ip ($runtime)" >&2
  # The run line is also written to the host's normal log stream so it reaches Loki/Grafana.
  if [[ $runtime == compose ]]; then
    "${SSH[@]}" "$ip" "cd ~/agent-lab && docker compose exec -T agentlab sh -c 'python -m agentlab.runner $persona $flag | tee /proc/1/fd/1'"
  else
    "${SSH[@]}" "$ip" "cd ~/agent-lab && set -a && . ./.env && set +a && .venv/bin/python -m agentlab.runner $persona $flag | tee >(systemd-cat -t agentlab)"
  fi
}

recent_lines() {  # <ip> <runtime> <n>
  if [[ $2 == compose ]]; then
    "${SSH[@]}" "$1" "cd ~/agent-lab && docker compose logs --no-log-prefix --tail 500 agentlab 2>/dev/null | grep '\"lab\": \"agent-lab\"' | tail -$3"
  else
    "${SSH[@]}" "$1" "journalctl -u agentlab -n 500 -o cat --no-pager 2>/dev/null | grep '\"lab\": \"agent-lab\"' | tail -$3"
  fi
}

cmd_status() {
  local ip runtime personas state
  while read -r ip runtime personas <&3; do
    if [[ $runtime == compose ]]; then
      state=$("${SSH[@]}" "$ip" "docker inspect -f '{{.State.Status}} since {{.State.StartedAt}}' agentlab 2>/dev/null || echo 'not deployed'")
    else
      state=$("${SSH[@]}" "$ip" "systemctl is-active agentlab 2>/dev/null || true")
    fi
    echo "== $ip  $runtime  [$personas]  $state"
    recent_lines "$ip" "$runtime" 3 | python3 -c '
import json, sys
for line in sys.stdin:
    try: d = json.loads(line)
    except ValueError: continue
    if d.get("event") != "run": print("   ", d.get("ts"), d.get("event"), d.get("reason", "")); continue
    secs = "%.0fs" % (d.get("latency_ms", 0) / 1000)
    toks = "%d tok" % d.get("total_tokens", 0)
    print("   ", d["ts"], d["persona"], "ok" if d["success"] else "FAIL", secs, toks, (d.get("error") or "")[:80])'
  done 3< <(hosts)
}

cmd_logs() {
  local ip=${1:-} runtime
  [[ -n "$ip" ]] || die "usage: ./deploy.sh logs <ip> [lines]"
  read -r _ runtime _ <<< "$(line_for_ip "$ip")"
  recent_lines "$ip" "$runtime" "${2:-20}"
}

case ${1:-up} in
  up) shift || true; cmd_up "$@" ;;
  config) cmd_config ;;
  run) shift; cmd_run "$@" ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "$@" ;;
  [0-9]*) cmd_up "$@" ;;
  *) die "unknown command '$1' (up|config|run|status|logs)" ;;
esac
