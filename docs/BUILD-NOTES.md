# Agent Lab: build notes (2026-09-29)

A record of what was built, the decisions behind it, what went wrong, and every change made outside this
repo, so the work can be audited, reproduced by hand ([`MANUAL-SETUP.md`](MANUAL-SETUP.md)) or undone.
Built with Claude Code over one session on 2026-09-29 (times are America/Chicago).

---

## 1. Timeline

| Time (CDT) | What happened |
|---|---|
| ~19:45 | **Discovery (read-only).** LiteLLM found on .101 as rootful Podman container `litellm` (port 4000, Postgres-backed, 5 models, all served by LM Studio on .194). All 13 lab IPs reachable over SSH. Existing Prometheus/Grafana/Loki/Alertmanager on .98. No Ollama or Langfuse anywhere. |
| 19:58–20:10 | Model tests through LiteLLM (latency, tool calls, throughput). These loaded qwen3.6, gemma-4 and muse-glimmer into LM Studio next to qwen3.8 (see incident 2). |
| 20:16 | LiteLLM config patch v1 (`agentlab-fast/alt/muse` aliases, fallback fast → alt), LiteLLM restarted. |
| ~20:17 | Team `agent-lab` and 9 virtual keys created; rerun confirmed idempotent (updated, no duplicates). |
| ~20:20 | User: LM Studio is used for real work 08:00–22:00; qwen3.8-27b must stay the default. |
| 20:23–20:35 | qwen3.8 re-test: 22 tokens in 66 s, 85 tokens in 307 s (~0.3 tok/s). Diagnosis: VRAM crowded by the models the earlier tests loaded. Background test stopped; user unloaded the other models. |
| ~20:37 | qwen3.8 alone: 0.7 s, 78 tok/s, tool call OK. |
| 20:38 | Config v2: v1 aliases replaced by `agentlab-main` + `agentlab-main-patient` (both qwen3.8), restart. Keys locked to those two routes, 1 parallel request per key. |
| 20:39 | Route cap `max_parallel_requests: 1` added (team-level cap isn't stored by this LiteLLM version), restart. Guardrails tested: 3 concurrent → served sequentially (0.7/1.8/3.3 s); other model → 403; oversized → rejected in 0.1 s. |
| 20:46–20:55 | Six personas written and tested on a temporary dev venv on .103. Reviewer failed twice and was fixed (incidents 5 and 6). Error injection and chaos burst verified. |
| 20:57–21:02 | Deployed: .100 and .85 (Docker Compose), rk1 and rk2 (venv + systemd). |
| 21:02–21:05 | Each persona run once from its real host; all six succeeded and appeared in LiteLLM spend logs. |
| 21:07 | Grafana *Agent Lab* dashboard installed on .98 (own folder). |
| 21:09 | Key-recovery test: removed chaos key from `.env`, reran setup → orphan replaced, still 9 keys; .100 redeployed with the new key. |
| ~21:10 | Commit `ec8505a`. Dev copy on .103 deleted. |
| 21:13–21:18 | Lab docs refreshed (`~/cropsey-lab`): inventory, topology HTML, both PNGs re-rendered; old PNGs archived. |
| ~21:25 | 7 personas registered in LiteLLM's agent registry (UI *Agents* page), keys linked via `agent_id`; triage run confirmed per-agent spend. Commit `513a478`. |
| ~21:35 | This documentation (`docs/`) written; lab docs updated with the Agents page and doc locations. |

---

## 2. Decisions and why

| Decision | Why |
|---|---|
| **Only qwen3.8-27b**, via `agentlab-main`; keys model-locked | It's the user's default model and fast (~78 tok/s) when alone on the 5090. Any second big model pushes it partly onto CPU (~0.3 tok/s). Model lock makes it impossible for the lab to trigger that. |
| **Local models only**, no cloud | User's choice. |
| **Daytime trickle** 08:00–22:00 (13% of night rate), back-off on slow calls | The GPU is used interactively during the day. |
| **1 request in flight** on the route | Lab-wide GPU protection that doesn't depend on the schedule; LiteLLM queues instead of rejecting. |
| Fallback `agentlab-main → agentlab-main-patient` (same model, longer timeout) | Demonstrates a fallback route without ever loading a second model. |
| **No agents on .101** | LiteLLM host; reports only 3.6 Gi RAM with ~1.2 Gi free. |
| **.77/.78 untouched** | Reserved by the user. |
| .103 dropped from the plan | Only needed for the red-team persona, which was deferred. |
| **Red team deferred** | Twice during the build Claude's own safety classifier stopped responses while it was writing the adversarial-traffic parts; the user chose to skip it for now. Its key `agent-redteam` exists, unused. |
| Deliberate errors limited to wrong model name (403), over-long prompt (400) and rate-limit bursts (429) | All three are refused by LiteLLM itself, so they never cost GPU time. |
| One scheduler per host with `PERSONAS`, not one container per persona | Fewer moving parts; one image. |
| Docker Compose on .100/.85, venv + systemd on rk1/rk2 | rk1/rk2 have no container runtime; installing one wasn't needed. |
| Per-user compose plugin on .85 | Docker engine had no compose plugin; a user-level binary avoids touching system packages. |
| Frameworks: PydanticAI (triage, news), LangGraph (sre, analyst), OpenAI Agents SDK (conversation, reviewer) | Brief asked for 2-3 frameworks; each fits its persona (structured output, tool loops with retries, multi-agent conversation). |
| Simulated pricing on the lab routes | Makes budgets, spend logs and the Agents page meaningful for a free local model. |
| Grafana from agent JSON logs via Loki, not LiteLLM metrics | Uses the existing Alloy → Loki pipeline; no change to LiteLLM callbacks. |
| Separate dashboard generator, own folder | rasp5's `gen_dashboards.py` doesn't know about the *Raspberry Pi Cluster* folder and its deploy step wipes the dashboards directory. |
| Agent registry entries without an A2A URL | The agents are scheduled clients, not A2A servers; registration gives them a page and per-agent spend. |

---

## 3. Incidents and mistakes

1. **Master key exposure.** During discovery Claude pasted the full LiteLLM master key into a `curl` command
   (blocked by the permission classifier, but present in the session transcript), and `podman inspect litellm`
   printed the container environment, including the master key, the Postgres password in `DATABASE_URL` and the
   LiteLLM UI password. **Action: rotate the master key (and those passwords if the transcript could leave the box).**
   Everything after that ran key-handling code inside the container so the key never appeared again.
2. **Model crowding caused by testing.** The first model tests loaded three extra models into LM Studio. qwen3.8
   then measured 140 s per call and was wrongly judged "too slow"; the first plan routed traffic to qwen3.6. The
   user pushed back ("works pretty well on a 5090"); the real cause was VRAM crowding. The user unloaded the
   extra models; qwen3.8 measured 78 tok/s. The plan was changed to qwen3.8 only.
3. **A 10-minute background test** at ~0.3 tok/s ran against the GPU during the user's working hours before
   being stopped.
4. **Wrong host identity in discovery.** The first report treated "this host" and .101 as different machines;
   they are the same (hv-rocky-linux-4).
5. **Reviewer: JSON-schema grammar error.** LM Studio rejected the structured-output grammar sent by the OpenAI
   Agents SDK (`Failed to initialize samplers: failed to parse grammar`). Fixed by returning the review through a
   `submit_review` tool call.
6. **Reviewer: out of tokens.** One run spent all 6144 tokens reasoning and produced nothing. Fixed with
   `max_tokens` 12288 and a smaller context cap (60k chars).
7. **Script bugs caught before or at first deploy:** tar `--exclude` placed after the file list; `ssh` inside
   `while read` loops swallowing the host list (status showed only the first host); a Python 3.9 f-string error in
   `deploy.sh status`; the scheduler would have run every persona within 5 minutes of each (re)start, even in the
   daytime. All fixed; the startup-burst bug was fixed before the first deploy.
8. **Installed `chromium-headless` on .101 without asking first** (needed to render the lab PNGs).
9. **Overwrote `~/cropsey-lab/lab-inventory.md` and `home-lab-topology.html` in place** without keeping copies;
   the old PNGs were archived to `~/cropsey-lab/archive/2026-09-19/`.
10. **Grafana admin password** in `/opt/monitoring/.env` no longer works (changed in the UI); dashboard
    provisioning was confirmed through Grafana's database instead.

---

## 4. Every change made outside this repo

| Where | Change | Undo |
|---|---|---|
| .101 `/opt/litellm/config.yaml` | Added two marked `agent-lab` blocks (routes + `router_settings`). Backups: `config.yaml.bak-agentlab-20260929-201628` (**original**), `-203813`, `-203852`. The pre-existing `config.yaml.bak` is the user's own. | Delete the marked blocks or restore the original in place (`sudo tee config.yaml < …201628`), restart |
| .101 LiteLLM container | Restarted 3× (~20:16, 20:38, 20:39), ~15 s downtime each | — |
| .101 LiteLLM database | Team `agent-lab`; 9 keys `agent-*` (chaos recreated once); 7 registered agents | MANUAL-SETUP §8 |
| .101 packages | `chromium-headless` (EPEL) + dependencies | `sudo dnf history undo <id>` or `sudo dnf remove chromium-headless` |
| .101 `~/agent-lab` | This repo + `.env` (mode 600, all keys) | `rm -rf` after teardown |
| .100 | `~/agent-lab` (code + `.env`), image `agentlab:latest`, base `python:3.12-slim`, container `agentlab` | `docker compose down --rmi local`, `rm -rf ~/agent-lab` |
| .85 | Same as .100, plus `~/.docker/cli-plugins/docker-compose` (v5.5.1) | same, `rm ~/.docker/cli-plugins/docker-compose` |
| rk1 .75 / rk2 .76 | `apt-get update` + `python3-venv`; `~/agent-lab` + `.venv`; `/etc/systemd/system/agentlab.service` (enabled) | `sudo systemctl disable --now agentlab`, remove unit + dir, `apt remove python3-venv` if unwanted |
| .98 | `/opt/monitoring/grafana/dashboards/Agent Lab/agent-lab.json` | `sudo rm -rf '/opt/monitoring/grafana/dashboards/Agent Lab'` |
| .103 | `~/agentlab-dev` (venv + key file) created for testing | Already deleted (~21:10) |
| LM Studio .194 | Never accessed directly. Test traffic via LiteLLM loaded 3 extra models (since unloaded by the user) | — |
| `~/cropsey-lab` on .101 | `lab-inventory.md`, `home-lab-topology.html` rewritten; `home-lab-topology.png` (light) and `cropsey-lab.png` (dark) re-rendered; `archive/2026-09-19/` holds the old PNGs | — |
| .101 `~/.ssh` | Deploy keys `github_agent_lab`, `github_cropsey_lab` (+ `.pub`) and two `Host github-*` entries in `~/.ssh/config` were generated but never registered on GitHub (the user uploaded via the web UI). `~/cropsey-lab` turned into a git clone of `mcropsey/cropsey-lab` | `rm ~/.ssh/github_agent_lab* ~/.ssh/github_cropsey_lab*` and delete the two `Host github-*` blocks |
| GitHub | New **public** repo `mcropsey/agent-lab` and refreshed `mcropsey/cropsey-lab`, both uploaded by the user through the web UI (~21:37). Scanned after upload: no agent keys, master key or passwords; `.env` not included. Master-key suffix removed from the public inventory. | — |
| Nothing on .77, .78, .99, .102, .104, .105 | Read-only checks only | — |

---

## 5. Verification results

| Test | Result |
|---|---|
| qwen3.8-27b alone on the 5090 | 0.7 s, 78 tok/s, tool call returned |
| Idempotent key setup | Rerun → all 9 `updated`, 1 row per alias |
| Lost-key recovery | Chaos key removed from `.env` → `recreated`, still 9 keys |
| Route cap | 3 keys at once → finished at 0.7 / 1.8 / 3.3 s (queued) |
| Model lock | Agent key asking for qwen3.6 → 403 in 0.0 s |
| Oversized prompt | 400 `ContextWindowExceededError` from LiteLLM |
| Chaos burst | 5 requests → 1 × 200, 4 × 429 |
| Manual run of each persona from its host (21:02–21:05) | triage 7 s / 2 calls; sre 35 s / 7 calls, correct diagnosis; analyst 7 s / 5 calls; news 19 s, 2 live feeds; conversation 31 s / 17 calls / 7 turns; reviewer 36 s. All `success: true`. |
| LiteLLM spend logs | Every `agent-*` key with its `host:` tag (hv-rocky-linux-3, rasp5, rk1, rk2), model `openai/qwen/qwen3.8-27b` |
| Agents page | 7 agents, 1 linked key each; triage run attributed ($0.0019) |
| Loki / Grafana | Run lines from all 4 hosts; all 16 dashboard queries parse; dashboard stored in Grafana (unified storage, folder *Agent Lab*) |
| Monitoring health | Prometheus 24/24 targets up |

---

## 6. Open follow-ups

- **Rotate the LiteLLM master key** (incident 1); consider the Postgres and LiteLLM UI passwords too.
- Red-team persona: deferred; `agent-redteam` key exists unused.
- `gen_dashboards.py` on rasp5: add the *Raspberry Pi Cluster* and *Agent Lab* folders or change its deploy step,
  before anyone runs it again.
- Grafana admin password in `/opt/monitoring/.env` is stale.
- Alertmanager receiver is still `null` (pre-existing).
- .99 and .101 report 3.6 Gi RAM (documented 6.0 / 8.1 Gi on 2026-09-19); cause unverified.
- `vulnnotes` container on .98 exited 13 days ago (pre-existing, safe to remove).
- ~~`~/agent-lab` exists only on .101~~ Uploaded to public `github.com/mcropsey/agent-lab`. `.env` is not in git;
  regenerate it with `scripts/setup_keys.sh`.
- Spend before 2026-09-29 21:25 isn't attributed to agents (pre-registration test traffic).
- `.gitignore` isn't on GitHub yet (the web uploader skips dotfiles); add it via *Add file → Create new file*.
- Local git history on .101 differs from GitHub's upload commits (same content). To track GitHub:
  `git fetch <repo url> main && git reset --hard FETCH_HEAD` in each repo.
