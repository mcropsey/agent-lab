# Runs INSIDE the litellm container (python3 -c), so the master key never leaves it.
# stdin:  {"team": {...}, "keys": [...], "existing": {"agent-triage": "sk-..."}}
# stdout: {"team_id": "...", "keys": {"agent-triage": {"key": "sk-...", "action": "..."}}}
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://localhost:4000"
MASTER = os.environ["LITELLM_MASTER_KEY"]


def api(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + MASTER, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]


spec = json.load(sys.stdin)
existing = spec.get("existing", {})

status, teams = api("GET", "/team/list")
if status != 200:
    sys.exit(f"team/list failed: {status} {teams}")
team = next((t for t in teams if t.get("team_alias") == spec["team"]["team_alias"]), None)
if team:
    team_id = team["team_id"]
    api("POST", "/team/update", {"team_id": team_id, **spec["team"]})
else:
    status, created = api("POST", "/team/new", {**spec["team"], "metadata": {"lab": "agent-lab"}})
    if status != 200:
        sys.exit(f"team/new failed: {status} {created}")
    team_id = created["team_id"]

def ensure_agent(alias: str, meta: dict) -> str:
    """Register (or update) the persona in LiteLLM's agent registry; returns its agent_id.

    No A2A url is set: these agents are scheduled clients, not A2A servers. The registry entry
    gives them a page in the LiteLLM UI, and the key's agent_id attributes their traffic.
    """
    body = {
        "agent_name": alias,
        "agent_card_params": {
            "name": f"{meta['title']} ({alias})",
            "description": f"{meta['description']} Framework: {meta['framework']}. Runs on {meta['host']}. "
                           "Part of the agent-lab traffic generator (~/agent-lab on .101).",
            "version": "1.0.0",
            "capabilities": {"streaming": False},
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [{**s, "description": s["name"]} for s in meta["skills"]],
        },
        "object_permission": {"models": ["agentlab-main", "agentlab-main-patient"]},
    }
    status, agents = api("GET", "/v1/agents")
    if status != 200:
        sys.exit(f"GET /v1/agents failed: {status} {agents}")
    existing_agent = next((a for a in agents if a.get("agent_name") == alias), None)
    if existing_agent:
        status, res = api("PUT", f"/v1/agents/{existing_agent['agent_id']}", body)
        if status != 200:
            sys.exit(f"update agent {alias} failed: {status} {res}")
        return existing_agent["agent_id"]
    status, res = api("POST", "/v1/agents", body)
    if status != 200:
        sys.exit(f"create agent {alias} failed: {status} {res}")
    return res["agent_id"]


out = {"team_id": team_id, "keys": {}, "agents": {}}
for k in spec["keys"]:
    alias = k["alias"]
    persona = alias.removeprefix("agent-")
    agent_id = ensure_agent(alias, k["agent"]) if k.get("agent") else None
    if agent_id:
        out["agents"][alias] = agent_id
    settings = {
        "key_alias": alias,
        "team_id": team_id,
        "max_budget": k["max_budget"],
        "budget_duration": "1d",
        "rpm_limit": k["rpm_limit"],
        "tpm_limit": k["tpm_limit"],
        "models": k["models"],
        "max_parallel_requests": k["max_parallel_requests"],
        "metadata": {"lab": "agent-lab", "persona": persona, "tags": ["agent-lab", f"persona:{persona}"]},
    }
    if agent_id:
        settings["agent_id"] = agent_id
    current = existing.get(alias)
    if current:
        status, _ = api("GET", "/key/info?key=" + urllib.parse.quote(current))
        if status == 200:
            status, body = api("POST", "/key/update", {"key": current, **settings})
            if status != 200:
                sys.exit(f"key/update {alias} failed: {status} {body}")
            out["keys"][alias] = {"key": current, "action": "updated"}
            continue
    # No usable local copy: remove any orphaned key with this alias so aliases stay unique.
    status, listed = api("GET", "/key/list?key_alias=" + urllib.parse.quote(alias))
    orphans = listed.get("keys", []) if status == 200 and isinstance(listed, dict) else []
    if orphans:
        status, body = api("POST", "/key/delete", {"keys": orphans})
        if status != 200:
            sys.exit(f"key/delete {alias} failed: {status} {body}")
    new_key = "sk-agentlab-" + secrets.token_hex(20)
    status, body = api("POST", "/key/generate", {"key": new_key, **settings})
    if status != 200:
        sys.exit(f"key/generate {alias} failed: {status} {body}")
    out["keys"][alias] = {"key": new_key, "action": "recreated" if orphans else "created"}

status, info = api("GET", "/team/info?team_id=" + urllib.parse.quote(team_id))
ti = info.get("team_info", {}) if isinstance(info, dict) else {}
out["team_check"] = {"max_parallel_requests": ti.get("max_parallel_requests"), "models": ti.get("models")}
json.dump(out, sys.stdout)
