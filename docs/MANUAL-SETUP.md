# Agent Lab: manual setup, verification and teardown

This guide rebuilds the Agent Lab by hand, without AI and without relying on the helper scripts.
Each step shows the script that automates it and the manual commands it runs. The state described
here was verified on 2026-09-29.

- What the lab is and how to operate it: [`../README.md`](../README.md)
- What was built, why, and what went wrong: [`BUILD-NOTES.md`](BUILD-NOTES.md)
- Lab-wide topology and inventory: `~/cropsey-lab/lab-inventory.md` and `home-lab-topology.html`/`.png`

Conventions: commands run as `mcropsey`. "On .101" means a shell on hv-rocky-linux-4 (192.168.1.101),
where LiteLLM and this repo live. Never echo the master key or agent keys; the commands below load
them into variables or files without printing them.

---

## 0. Architecture in one paragraph

Four hosts run a scheduler (`python -m agentlab.scheduler`) for the personas assigned to them. Each persona
calls LiteLLM on `.101:4000` with its own virtual key; LiteLLM forwards to LM Studio on `.194:1234`, which serves
**qwen3.8-27b** on an RTX 5090. Each run prints one JSON line to stdout. Alloy on each host already ships
docker/journald logs to Loki on `.98`, and a Grafana dashboard reads those lines.

| Host | Runtime | Personas | Keys it needs |
|---|---|---|---|
| 192.168.1.100 hv-rocky-linux-3 | Docker Compose | triage, sre, chaos | TRIAGE, SRE, CHAOS |
| 192.168.1.85 rasp5 | Docker Compose | analyst, news | ANALYST, NEWS |
| 192.168.1.75 rk1 | venv + systemd | conversation | CUSTOMER, SUPPORT |
| 192.168.1.76 rk2 | venv + systemd | reviewer | REVIEWER |

---

## 1. Prerequisites

| Requirement | Check |
|---|---|
| LiteLLM running as rootful Podman container `litellm` on .101, port 4000, with a Postgres DB (`DATABASE_URL`) and `LITELLM_MASTER_KEY` set | `sudo podman ps --filter name=litellm` · `curl -s localhost:4000/health/liveliness` |
| LiteLLM config at `/opt/litellm/config.yaml`, bind-mounted read-only into the container as `/app/config.yaml` | `sudo podman inspect litellm --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'` |
| LM Studio reachable by LiteLLM via env `LMSTUDIO_API_BASE` (http://192.168.1.194:1234/v1) with **only qwen3.8-27b loaded** | In LM Studio: eject everything else, or on that box `lms unload --all && lms load qwen/qwen3.8-27b` |
| Passwordless SSH + sudo from .101 to .100, .85, .75, .76 | `ssh -o BatchMode=yes 192.168.1.100 sudo -n true && echo ok` |
| Docker on .100 and .85 (user in `docker` group); Python 3.12 on rk1/rk2 | `ssh 192.168.1.85 docker ps` · `ssh 192.168.1.75 python3 --version` |
| Internet from the agent hosts (PyPI, Docker Hub, GitHub, RSS feeds) | `ssh 192.168.1.100 curl -sI https://pypi.org | head -1` |
| Loki/Grafana on .98 with Alloy shipping docker + journald logs from the agent hosts | Grafana → Explore → Loki → `{host="rk1"}` |
| `git`, `jq`, `python3` on .101 | `jq --version` |

Get the code: the repo lives at `~/agent-lab` on .101 (`git log` shows the history). If rebuilding elsewhere, copy the repo;
**`.env` is not in git** (it holds the keys). Step 3 recreates it.

---

## 2. LiteLLM routes and fallback

Automated: `./scripts/setup_litellm.sh` (idempotent; backs up, validates, restarts, rolls back on failure).

### By hand

1. Back up the config:
   ```bash
   sudo cp -p /opt/litellm/config.yaml /opt/litellm/config.yaml.bak-agentlab-$(date +%Y%m%d-%H%M%S)
   ```
   The original pre-Agent-Lab config is `/opt/litellm/config.yaml.bak-agentlab-20260929-201628`.

2. Edit `/opt/litellm/config.yaml` **in place** (`sudo vi` or `sudoedit`; do not `sed -i` or move a new file over it:
   the container bind-mounts the file by inode and would keep seeing the old one).

   a. Inside `model_list:`, after the last existing entry and before `litellm_settings:`, add the block below.
      Replace each `api_key: <copy ...>` line with the **exact `api_key:` line of the existing `qwen3.8-27b` entry**
      (same indentation, 6 spaces):
   ```yaml
     # >>> agent-lab models (added by agent-lab/scripts/setup_litellm.sh; delete this block to undo) >>>
     # Both routes use qwen3.8-27b, the model LM Studio keeps loaded, so lab traffic never
     # triggers a model load/swap. Pricing is simulated so budgets and spend are meaningful.
     - model_name: agentlab-main
       litellm_params:
         model: openai/qwen/qwen3.8-27b
         api_base: os.environ/LMSTUDIO_API_BASE
         api_key: <copy the api_key line from the qwen3.8-27b entry>
         timeout: 180
         max_parallel_requests: 1   # lab-wide cap: at most one agent request on the GPU at a time
       model_info:
         input_cost_per_token: 0.0000003
         output_cost_per_token: 0.0000012
         max_input_tokens: 32768
     - model_name: agentlab-main-patient
       litellm_params:
         model: openai/qwen/qwen3.8-27b
         api_base: os.environ/LMSTUDIO_API_BASE
         api_key: <copy the api_key line from the qwen3.8-27b entry>
         timeout: 600
         max_parallel_requests: 1
       model_info:
         input_cost_per_token: 0.0000003
         output_cost_per_token: 0.0000012
         max_input_tokens: 32768
     # <<< agent-lab models <<<
   ```
   b. At the end of the file add (only if there is no `router_settings:` section yet; otherwise merge the keys):
   ```yaml
   # >>> agent-lab router settings (added by agent-lab/scripts/setup_litellm.sh; delete this block to undo) >>>
   router_settings:
     # Rejects requests larger than a model's max_input_tokens before they reach LM Studio,
     # so the lab's deliberate "oversized context" errors never touch the GPU.
     enable_pre_call_checks: true
     # Same model, longer timeout: demonstrates fallback without ever loading a second model.
     fallbacks:
       - agentlab-main: ["agentlab-main-patient"]
   # <<< agent-lab router settings <<<
   ```

3. Validate inside the container (it has PyYAML), then restart:
   ```bash
   sudo podman exec litellm python3 -c "import yaml; c=yaml.safe_load(open('/app/config.yaml')); print([m['model_name'] for m in c['model_list']]); print(c['router_settings'])"
   sudo podman restart litellm
   until curl -sf localhost:4000/health/liveliness >/dev/null; do sleep 3; done; echo healthy
   ```
   Restart takes ~15 s. If LiteLLM does not come back: `sudo cp /opt/litellm/config.yaml.bak-agentlab-<latest> /opt/litellm/config.yaml`
   is **not** safe (replaces the inode); use `sudo tee /opt/litellm/config.yaml < /opt/litellm/config.yaml.bak-agentlab-<latest> >/dev/null`
   then restart again.

---

## 3. Team, virtual keys and registered agents

Automated: `./scripts/setup_keys.sh` (idempotent; writes `.env` mode 600, prints only the last 4 characters).
The spec it applies is `scripts/keys.json`.

### By hand

Load the master key into your shell without printing it, and set helpers:
```bash
cd ~/agent-lab
export MK=$(sudo podman exec litellm printenv LITELLM_MASTER_KEY)
L=http://localhost:4000
api() { curl -s -H "Authorization: Bearer $MK" -H "Content-Type: application/json" "$@"; }
```

1. **Team** (skip if `api $L/team/list | jq -r '.[].team_alias'` already lists `agent-lab`):
   ```bash
   TEAM=$(api -X POST $L/team/new -d '{"team_alias":"agent-lab","max_budget":10.0,"budget_duration":"1d",
     "models":["agentlab-main","agentlab-main-patient"],"metadata":{"lab":"agent-lab"}}' | jq -r .team_id)
   echo "team_id=$TEAM"
   ```
   (Existing team: `TEAM=$(api $L/team/list | jq -r '.[] | select(.team_alias=="agent-lab") | .team_id')`.)

2. **Registered agents** (LiteLLM UI → *Agents*). One per persona except chaos/redteam. Example for triage;
   repeat with the values from the table below. Skip any that `api $L/v1/agents | jq -r '.[].agent_name'` already lists.
   ```bash
   AGENT_TRIAGE=$(api -X POST $L/v1/agents -d '{
     "agent_name": "agent-triage",
     "agent_card_params": {
       "name": "Ticket triage (agent-triage)",
       "description": "Classifies synthetic support tickets, sets priority, flags PII and drafts a reply. Framework: PydanticAI. Runs on hv-rocky-linux-3 (.100). Part of the agent-lab traffic generator (~/agent-lab on .101).",
       "version": "1.0.0", "capabilities": {"streaming": false},
       "defaultInputModes": ["text"], "defaultOutputModes": ["text"],
       "skills": [{"id": "triage-ticket", "name": "Triage a support ticket", "description": "Triage a support ticket", "tags": ["support","classification","pii"]}]
     },
     "object_permission": {"models": ["agentlab-main","agentlab-main-patient"]}
   }' | jq -r .agent_id)
   ```
   | agent_name | card name | framework / host | skill id |
   |---|---|---|---|
   | agent-triage | Ticket triage | PydanticAI · hv-rocky-linux-3 (.100) | triage-ticket |
   | agent-sre | SRE log analyst | LangGraph · hv-rocky-linux-3 (.100) | investigate-logs |
   | agent-analyst | Data analyst | LangGraph · rasp5 (.85) | sql-analysis |
   | agent-news | News digest | PydanticAI · rasp5 (.85) | news-digest |
   | agent-customer | Simulated customer | OpenAI Agents SDK · rk1 (.75) | customer-chat |
   | agent-support | Customer support | OpenAI Agents SDK · rk1 (.75) | support-chat |
   | agent-reviewer | Code reviewer | OpenAI Agents SDK · rk2 (.76) | code-review |

   Full descriptions and skill tags are in `scripts/keys.json` (`"agent"` blocks).
   No A2A `url` is set on purpose: the agents are scheduled clients, not A2A servers.

3. **Virtual keys**: one per persona. The key value is chosen by us (must start with `sk-`), so it can be
   written straight into `.env`. Example for triage:
   ```bash
   umask 077; touch .env
   KEY="sk-agentlab-$(openssl rand -hex 20)"
   api -X POST $L/key/generate -d "{\"key\":\"$KEY\",\"key_alias\":\"agent-triage\",\"team_id\":\"$TEAM\",
     \"agent_id\":\"$AGENT_TRIAGE\",\"max_budget\":1.0,\"budget_duration\":\"1d\",\"rpm_limit\":30,\"tpm_limit\":120000,
     \"max_parallel_requests\":1,\"models\":[\"agentlab-main\",\"agentlab-main-patient\"],
     \"metadata\":{\"lab\":\"agent-lab\",\"persona\":\"triage\",\"tags\":[\"agent-lab\",\"persona:triage\"]}}" | jq -r .key_alias
   echo "AGENTLAB_KEY_TRIAGE=$KEY" >> .env; unset KEY
   ```
   | key_alias | .env variable | rpm | tpm | $/day | agent_id |
   |---|---|---|---|---|---|
   | agent-triage | AGENTLAB_KEY_TRIAGE | 30 | 120000 | 1.0 | agent-triage |
   | agent-sre | AGENTLAB_KEY_SRE | 30 | 120000 | 1.0 | agent-sre |
   | agent-analyst | AGENTLAB_KEY_ANALYST | 30 | 120000 | 1.0 | agent-analyst |
   | agent-news | AGENTLAB_KEY_NEWS | 20 | 120000 | 1.0 | agent-news |
   | agent-customer | AGENTLAB_KEY_CUSTOMER | 30 | 120000 | 1.0 | agent-customer |
   | agent-support | AGENTLAB_KEY_SUPPORT | 30 | 120000 | 1.0 | agent-support |
   | agent-reviewer | AGENTLAB_KEY_REVIEWER | 20 | 250000 | 1.0 | agent-reviewer |
   | agent-redteam | AGENTLAB_KEY_REDTEAM | 20 | 120000 | 1.0 | — (unused) |
   | agent-chaos | AGENTLAB_KEY_CHAOS | 2 | 5000 | 0.1 | — |

   All keys: `team_id` = agent-lab, `budget_duration` 1d, `max_parallel_requests` 1, models `agentlab-main`,
   `agentlab-main-patient`. Finally add the base URL line: `echo "OPENAI_BASE_URL=http://192.168.1.101:4000/v1" >> .env`.

   **Avoiding duplicates:** before creating a key, check `api "$L/key/list?key_alias=agent-triage" | jq .keys`.
   If a key with that alias exists but you no longer have its value, delete it first:
   `api -X POST $L/key/delete -d '{"keys": <the hashed tokens from key/list>}'`. To change limits on an existing
   key you still hold: `api -X POST $L/key/update -d '{"key":"<value>","rpm_limit":30}'`.

4. `unset MK` when done.

---

## 4. Deploy to the agent hosts

Automated: `./deploy.sh` (all hosts in `hosts.conf`) or `./deploy.sh 192.168.1.85`.

### Per-host `.env` (all hosts)

Each host gets `~/agent-lab/.env` (mode 600) with only what it needs:
```
PERSONAS=<comma list from hosts.conf, e.g. analyst,news>
AGENTLAB_HOST=<short hostname, e.g. rasp5>
OPENAI_BASE_URL=http://192.168.1.101:4000/v1
AGENTLAB_KEY_ANALYST=<from .101's .env>
AGENTLAB_KEY_NEWS=<from .101's .env>
```
Build it on .101 and send it without echoing keys:
```bash
H=192.168.1.85; P=analyst,news; N=$(ssh $H hostname -s)
{ echo "PERSONAS=$P"; echo "AGENTLAB_HOST=$N"; grep -E '^(OPENAI_BASE_URL|AGENTLAB_KEY_(ANALYST|NEWS))=' .env; } \
  | ssh $H 'umask 077; mkdir -p ~/agent-lab && cat > ~/agent-lab/.env'
```
(conversation needs `AGENTLAB_KEY_CUSTOMER` and `AGENTLAB_KEY_SUPPORT`; chaos needs `AGENTLAB_KEY_CHAOS`.)

### Copy the code (all hosts)
```bash
tar czf - --exclude=__pycache__ agentlab config data requirements.txt Dockerfile docker-compose.yml systemd \
  | ssh $H 'mkdir -p ~/agent-lab && cd ~/agent-lab && rm -rf agentlab data && tar xzf -'
```

### Docker Compose hosts (.100, .85)
```bash
ssh $H
# only if `docker compose version` fails (it did on .85): per-user compose plugin, no sudo
mkdir -p ~/.docker/cli-plugins
curl -fsSL -o ~/.docker/cli-plugins/docker-compose \
  https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-$(uname -m)
chmod +x ~/.docker/cli-plugins/docker-compose
cd ~/agent-lab && docker compose up -d --build
docker logs agentlab          # expect a {"event": "scheduler_start", ...} line
```
The container runs as uid 10001, mounts `./config` read-only (config edits need no rebuild), 768 MB memory cap,
`restart: unless-stopped`.

### venv + systemd hosts (rk1 .75, rk2 .76)
```bash
ssh $H
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv      # was missing on rk1/rk2
cd ~/agent-lab && python3 -m venv .venv
.venv/bin/pip install --upgrade pip && .venv/bin/pip install -r requirements.txt
sed "s|@HOME@|$HOME|g; s|@USER@|$USER|g" systemd/agentlab.service | sudo tee /etc/systemd/system/agentlab.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now agentlab
journalctl -u agentlab -o cat -n 5     # expect scheduler_start
```

---

## 5. Grafana dashboard (.98)

Automated: `./grafana/install.sh`.

By hand (writes only the *Agent Lab* folder; **do not** use the `rm -rf .../dashboards/*` step from rasp5's
`MONITORING_SETUP.md` §4.8, which would delete this folder and *Raspberry Pi Cluster*):
```bash
cd ~/agent-lab/grafana && python3 gen_dashboard.py agent-lab.json
ssh 192.168.1.98 "sudo mkdir -p '/opt/monitoring/grafana/dashboards/Agent Lab'"
ssh 192.168.1.98 "sudo tee '/opt/monitoring/grafana/dashboards/Agent Lab/agent-lab.json' >/dev/null && sudo chown -R 472:0 '/opt/monitoring/grafana/dashboards/Agent Lab'" < agent-lab.json
```
Grafana picks it up within 30 s: http://192.168.1.98:3000/d/agent-lab. Queries use
`{job=~"docker|journal"} |= "\"lab\": \"agent-lab\"" | json | event="run"`.

---

## 6. Verify

| Check | Command (on .101 unless noted) | Expected |
|---|---|---|
| Schedulers up | `./deploy.sh status` | compose hosts `running`, venv hosts `active`, a `scheduler_start` line each |
| One run per persona | `./deploy.sh run triage --no-errors` (sre, analyst, news, conversation, reviewer) | JSON line with `"success": true` |
| By hand, compose | `ssh 192.168.1.100 'cd ~/agent-lab && docker compose exec -T agentlab python -m agentlab.runner triage --no-errors'` | same |
| By hand, venv | `ssh 192.168.1.75 'cd ~/agent-lab && set -a && . ./.env && set +a && .venv/bin/python -m agentlab.runner conversation --no-errors'` | same |
| Spend logs | see SQL below | rows per `agent-*` key with a `host:` tag, model `openai/qwen/qwen3.8-27b` |
| Agents page | `api $L/v1/agents \| jq -r '.[] \| "\(.agent_name) keys=\(.keys\|length) spend=\(.spend)"'` | 7 agents, 1 key each |
| Model lock | `curl -s -o /dev/null -w '%{http_code}\n' localhost:4000/v1/chat/completions -H "Authorization: Bearer $(grep ^AGENTLAB_KEY_TRIAGE= .env \| cut -d= -f2-)" -H 'Content-Type: application/json' -d '{"model":"qwen3.6-35b-a3b","messages":[{"role":"user","content":"hi"}]}'` | `403` |
| Loki | `curl -s -G http://192.168.1.98:3100/loki/api/v1/query --data-urlencode 'query=sum by (host) (count_over_time({job=~"docker\|journal"} \|= "\"lab\": \"agent-lab\"" \| json \| event="run" [6h]))' \| jq -c '[.data.result[].metric.host]'` | agent hosts that logged runs (hv-rocky-linux-3, rasp5, rk1, rk2 once each has run) |

Spend-log query (per key alias, last hour):
```bash
sudo podman exec postgresdb psql -U admin -d litellm -c "
select k.key_alias, count(*) requests, sum(s.total_tokens) tokens, round(sum(s.spend)::numeric,4) spend,
       max(s.request_tags::text) tags
from \"LiteLLM_SpendLogs\" s join \"LiteLLM_VerificationToken\" k on k.token = s.api_key
where s.\"startTime\" > now() - interval '1 hour' and k.key_alias like 'agent-%' group by 1 order by 1;"
```

---

## 7. Operate

| Task | How |
|---|---|
| Traffic volume | `global.intensity` in `config/lab.yaml` → `./deploy.sh config` (or copy `config/lab.yaml` to `~/agent-lab/config/` on each host) |
| Daytime window / rates | `global.schedule` (08:00–22:00, `day_rate` 0.13) and `personas.<name>.runs_per_hour` |
| Pause (kill switch) | `global.enabled: false` → `./deploy.sh config`; applies within `max_wait_minutes` (30) |
| Stop now | `./stop.sh` — by hand: `docker compose down` in `~/agent-lab` (compose) / `sudo systemctl disable --now agentlab` (venv) |
| Logs | `docker logs -f agentlab` · `journalctl -u agentlab -f -o cat` · Grafana dashboard |
| Move a persona | edit `hosts.conf`, `./stop.sh <old>`, `./deploy.sh <new>` (key env lines follow automatically) |
| Rotate agent keys | delete the `AGENTLAB_KEY_*` lines from `.env`, `./scripts/setup_keys.sh` (orphans are replaced), `./deploy.sh` |

---

## 8. Tear down completely

```bash
cd ~/agent-lab
./stop.sh                                                     # stop every host
# remove from hosts (optional)
for h in 192.168.1.100 192.168.1.85; do ssh $h 'cd ~/agent-lab && docker compose down --rmi local; rm -rf ~/agent-lab'; done
for h in 192.168.1.75 192.168.1.76; do ssh $h 'sudo rm -f /etc/systemd/system/agentlab.service && sudo systemctl daemon-reload && rm -rf ~/agent-lab'; done
# LiteLLM keys, agents, team
export MK=$(sudo podman exec litellm printenv LITELLM_MASTER_KEY); L=http://localhost:4000
api() { curl -s -H "Authorization: Bearer $MK" -H "Content-Type: application/json" "$@"; }
api -X POST $L/key/delete -d '{"key_aliases":["agent-triage","agent-sre","agent-analyst","agent-news","agent-customer","agent-support","agent-reviewer","agent-redteam","agent-chaos"]}'
for id in $(api $L/v1/agents | jq -r '.[] | select(.agent_name|startswith("agent-")) | .agent_id'); do api -X DELETE $L/v1/agents/$id; done
api -X POST $L/team/delete -d "{\"team_ids\":[\"$(api $L/team/list | jq -r '.[]|select(.team_alias=="agent-lab").team_id')\"]}"
unset MK
# LiteLLM config: delete the two "agent-lab" marked blocks (sudoedit), or restore the original in place:
sudo tee /opt/litellm/config.yaml < /opt/litellm/config.yaml.bak-agentlab-20260929-201628 >/dev/null && sudo podman restart litellm
# Grafana
ssh 192.168.1.98 "sudo rm -rf '/opt/monitoring/grafana/dashboards/Agent Lab'"
```
Packages added during the build (remove only if nothing else needs them): `chromium-headless` on .101
(`sudo dnf remove chromium-headless`, used to render the lab PNGs), `python3-venv` on rk1/rk2,
`~/.docker/cli-plugins/docker-compose` on .85.

---

## 9. Troubleshooting (things that actually happened)

| Symptom | Cause | Fix |
|---|---|---|
| qwen3.8-27b suddenly ~0.3 tok/s, calls take minutes | Other models (qwen3.6, gemma, muse) were loaded in LM Studio alongside it; qwen3.8 got pushed partly onto CPU | Eject the others: `lms unload --all && lms load qwen/qwen3.8-27b`. The agent keys can only use qwen3.8, so the lab can't cause this; direct calls to the other model names can |
| Run fails `finish_reason='length' but produced no assistant text` | Reasoning model spent the whole `max_tokens` thinking | Raise `max_tokens` for that persona (reviewer uses 12288) |
| `Failed to initialize samplers: failed to parse grammar` (400) | LM Studio can't compile the JSON-schema grammar the OpenAI Agents SDK sends for `output_type` | Return structured output through a tool call instead (see `personas/reviewer.py`, `submit_review` + `StopAtTools`) |
| Config edit has no effect in the container | File was replaced (new inode); the single-file bind mount still points at the old one | Write in place: `sudo tee /opt/litellm/config.yaml < new.yaml` |
| `podman ps` shows nothing on .101 | Containers are rootful | `sudo podman ...` |
| `docker compose` missing on .85 | Only the Docker engine is installed | Per-user plugin, see §4 |
| Team-level `max_parallel_requests` shows `None` | This LiteLLM version doesn't store it on teams | The lab-wide cap is set on the route (`max_parallel_requests: 1` in the model entry) |
| Grafana API says invalid password | `GF_SECURITY_ADMIN_PASSWORD` in `/opt/monitoring/.env` only applies at first start; it was changed in the UI | Use the current admin password; the dashboard itself is file-provisioned and needs no API |
| Agents not on LiteLLM's *Agents* page | Keys alone don't register agents | §3 step 2, then link keys with `agent_id` |
| Agent runs don't appear in Grafana | Manual runs print to your terminal, not the service log | `./deploy.sh run` tees them into the service log; scheduled runs always log |
