# agent-lab

Scheduled AI agents with fake-but-realistic jobs that send all their LLM traffic through the
lab's LiteLLM proxy. Built for demos and API-security lab work; no real business use.

**Documentation**

| Doc | What it's for |
|---|---|
| this README | what the lab is, personas, safeguards, day-to-day commands |
| [`docs/MANUAL-SETUP.md`](docs/MANUAL-SETUP.md) | rebuild everything by hand (no scripts, no AI), verify, operate, tear down, troubleshoot |
| [`docs/BUILD-NOTES.md`](docs/BUILD-NOTES.md) | build log: timeline, decisions, incidents, every change outside this repo, test results, follow-ups |
| `~/cropsey-lab/lab-inventory.md` | lab-wide inventory (all 13 hosts), incl. LiteLLM and this lab |
| `~/cropsey-lab/home-lab-topology.html`, `.png` / `cropsey-lab.png` | topology diagrams (light / dark renders of the same page) |

GitHub: `mcropsey/agent-lab` (private, this repo) · `mcropsey/cropsey-lab` (public, lab docs).

```
 .100 hv-rocky-linux-3 ─ docker ─ triage, sre, chaos ─┐
 .85  rasp5            ─ docker ─ analyst, news ──────┤  one virtual key per persona
 .75  rk1              ─ venv   ─ conversation ───────┼──► LiteLLM  .101:4000 ──► LM Studio (.194)
 .76  rk2              ─ venv   ─ reviewer ───────────┘    agentlab-main route      qwen3.8-27b only
          │ one JSON line per run (stdout → docker / journald)
          └──► Alloy ──► Loki (.98:3100) ──► Grafana "Agent Lab" dashboard (.98:3000/d/agent-lab)
```

## Personas

| Persona | Framework | Host | What it does |
|---|---|---|---|
| `triage` | PydanticAI | .100 | Faker-generated support tickets (half written by the LLM), some with fake PII. Tools: customer lookup, status page. Structured output: category, priority, PII flags, draft reply. |
| `sre` | LangGraph | .100 | Writes 300-1500 lines of fake service logs with one injected incident (DB pool, OOM, expired cert, disk full, latency, login failures). Investigates with list/read/search file tools and writes an incident summary. |
| `analyst` | LangGraph | .85 | Randomized business questions against the Chinook SQLite DB with a read-only SQL tool. Failed queries come back as errors and the agent retries; 20% of runs get a stale schema hint to provoke that. |
| `news` | PydanticAI | .85 | Pulls 2-3 public RSS feeds (BBC, Ars, HN, The Register, NASA), summarizes and tags topics. Falls back to canned articles offline. |
| `conversation` | OpenAI Agents SDK | .75 | A customer agent (random persona and goal, key `agent-customer`) chats 3-8 turns with a support agent (tools over a fake CRM, key `agent-support`). |
| `reviewer` | OpenAI Agents SDK | .76 | Reviews bundled diffs with planted bugs, or synthetic diffs made by mutating this repo's own code, with up to ~15k tokens of file context. |
| `chaos` | httpx | .100 | Rate-limit bursts: 5 simultaneous calls on the `agent-chaos` key (1 in flight, 2 rpm), so LiteLLM answers with 429s. |

A red-team persona was scoped out for now; its key (`agent-redteam`) exists but nothing uses it.

## Protecting interactive use of LM Studio

The GPU box is used for real work during the day, so the lab is deliberately gentle:

- **One model only.** Every persona uses the `agentlab-main` route (qwen3.8-27b, the model LM Studio keeps
  loaded). Agent keys are locked in LiteLLM to `agentlab-main` / `agentlab-main-patient`, so no agent can
  ask LM Studio to load another model; any other model name gets a 403.
- **One request at a time.** The `agentlab-main` route has `max_parallel_requests: 1`, so LiteLLM queues
  agent requests instead of stacking them on the GPU.
- **Daytime trickle.** 08:00-22:00 (America/Chicago) runs at 13% of the night rate (~1.4 runs/hour lab-wide);
  22:00-08:00 is ~10.5 runs/hour.
- **Back-off.** If a daytime call is slow (>20 s at <20 tok/s, i.e. the GPU is busy), that host pauses for 30 min.
- **Deliberate errors never touch the GPU.** Wrong model names (403) and over-long prompts
  (LiteLLM's pre-call context check, 400) are rejected by LiteLLM itself.

## Day-to-day operation

All commands run from `~/agent-lab` on .101.

| Task | How |
|---|---|
| Change traffic volume | edit `global.intensity` (or a persona's `runs_per_hour`) in `config/lab.yaml`, then `./deploy.sh config` |
| Pause everything (kill switch) | set `global.enabled: false` in `config/lab.yaml`, then `./deploy.sh config` (applies within ~30 min) |
| Stop everything now | `./stop.sh` (or `./stop.sh 192.168.1.100` for one host) |
| Start / redeploy | `./deploy.sh` (all hosts) or `./deploy.sh 192.168.1.85` |
| Run one persona right now | `./deploy.sh run triage` (add `--no-errors` to skip deliberate errors) |
| What's running where | `./deploy.sh status` |
| Recent run lines | `./deploy.sh logs 192.168.1.75 20` |
| Spend by persona | LiteLLM UI → Usage / Logs (filter by key alias `agent-*` or tag `persona:*`), or the Grafana dashboard |

`config/lab.yaml` is re-read on every scheduler wake-up, so config pushes need no restart.
Moving a persona to another host means editing `hosts.conf`, then running `./stop.sh <old ip>` and `./deploy.sh <new ip>`.

## Run log format

Every run writes exactly one JSON line to stdout (docker logs on .100/.85, journald on rk1/rk2):

```json
{"ts": "...", "lab": "agent-lab", "event": "run", "host": "rasp5", "persona": "analyst", "framework": "langgraph",
 "model": "agentlab-main", "run_id": "...", "trigger": "schedule", "success": true, "error": null, "latency_ms": 7100,
 "llm_calls": 5, "llm_errors": 0, "http_errors": [], "injected_errors": [], "prompt_tokens": 3671,
 "completion_tokens": 676, "reasoning_tokens": 180, "total_tokens": 4347, "tool_calls": 5, "tool_executions": 5,
 "cost_usd": 0.0019, "slow_calls": 0, "...persona-specific fields": "..."}
```

Other events: `scheduler_start`, `backoff`. Each LLM request is also tagged for LiteLLM's spend logs with
`agent-lab`, `persona:<name>` and `host:<hostname>`, and sets `user=agent-<key>@<host>`.

## LiteLLM integration (on .101)

- `scripts/setup_litellm.sh` adds a marked `agent-lab` block to `/opt/litellm/config.yaml`: the `agentlab-main`
  route (qwen3.8-27b, 180 s timeout, 1 in flight, simulated pricing, 32k input limit), an `agentlab-main-patient`
  fallback route (same model, 600 s timeout), and `router_settings` with pre-call checks and the fallback.
  It backs up the config first (`config.yaml.bak-agentlab-*`), validates it inside the container, restarts
  LiteLLM and restores the backup on failure. Rerunning it is a no-op. **To undo:** delete the two
  `agent-lab` marked blocks (or restore the first backup) and `sudo podman restart litellm`.
- `scripts/setup_keys.sh` creates or updates team `agent-lab` and one virtual key per persona (daily budget,
  rpm/tpm, 1 parallel request, model-locked). It runs inside the LiteLLM container so the master key never
  leaves it, writes keys only to `.env` (mode 600) and prints only the last 4 characters. Reruns update keys
  in place; if `.env` lost a key, the orphan is deleted and replaced, so aliases stay unique.
  It also registers each persona (except chaos) in LiteLLM's **agent registry** (`/v1/agents`, the UI's
  *Agents* page) and links the persona's key to it with `agent_id`, so each agent's spend and activity show
  up there. The registry entries have no A2A URL: these agents are scheduled clients, not A2A servers, so
  "invoke" from the UI won't work.

## Setup from scratch

```bash
./scripts/setup_litellm.sh     # LiteLLM routes + fallback (restarts LiteLLM ~15 s)
./scripts/setup_keys.sh        # virtual keys -> .env
./deploy.sh                    # sync + build/install + start on every host in hosts.conf
./grafana/install.sh           # Agent Lab dashboard on .98 (own folder; doesn't touch other dashboards)
```

`deploy.sh` needs passwordless SSH plus sudo on the targets. On first deploy it installs `python3-venv` on
venv hosts and, where missing, the docker compose CLI plugin for the user (`~/.docker/cli-plugins`).

## Layout

```
agentlab/            llm.py (metering, tagging, deliberate errors) · runner.py (one run + log line)
                     scheduler.py (APScheduler + time-of-day curve) · chaos.py · personas/*.py
config/lab.yaml      intensity, kill switch, schedule, per-persona rates
hosts.conf           which personas run on which host, and how (compose | venv)
data/                chinook.sqlite · news_fallback.json · diffs/*.diff (planted issues)
scripts/             LiteLLM config patch + key setup
grafana/             dashboard generator + installer
Dockerfile, docker-compose.yml, systemd/agentlab.service
```
