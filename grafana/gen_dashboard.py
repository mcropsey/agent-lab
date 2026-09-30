#!/usr/bin/env python3
"""Generate the "Agent Lab" Grafana dashboard (Loki queries over the agents' JSON run lines).

Kept separate from ~/monitoring/gen_dashboards.py on rasp5 on purpose: that script's deploy step
replaces the whole dashboards directory. This dashboard lives in its own folder ("Agent Lab").
Usage: python3 gen_dashboard.py [out.json]
"""
import json
import sys

LDS = {"type": "loki", "uid": "loki"}
SEL = '{job=~"docker|journal"} |= `"lab": "agent-lab"` | json'
RUNS = f'{SEL} | event="run" | persona=~"$persona" | host=~"$host"'
_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def target(expr, legend="", ref="A", instant=False):
    return {"refId": ref, "datasource": LDS, "expr": expr, "legendFormat": legend,
            "queryType": "instant" if instant else "range"}


def stat(title, expr, x, unit="short", decimals=None, desc=""):
    return {"id": nid(), "type": "stat", "title": title, "description": desc, "datasource": LDS,
            "gridPos": {"x": x, "y": 0, "w": 4, "h": 4},
            "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none"},
            "targets": [target(expr, instant=True)]}


def series(title, expr, x, y, legend="{{persona}}", unit="short", w=12, h=8, bars=True, desc=""):
    return {"id": nid(), "type": "timeseries", "title": title, "description": desc, "datasource": LDS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": {"unit": unit, "custom": {
                "drawStyle": "bars" if bars else "line", "fillOpacity": 70 if bars else 10, "lineWidth": 1,
                "showPoints": "never", "stacking": {"mode": "normal" if bars else "none"}}}, "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["sum"]},
                        "tooltip": {"mode": "multi", "sort": "desc"}},
            "targets": [target(expr, legend)]}


def logs(title, expr, y, h=10, desc=""):
    return {"id": nid(), "type": "logs", "title": title, "description": desc, "datasource": LDS,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": h},
            "options": {"showTime": True, "wrapLogMessage": True, "enableLogDetails": True,
                        "sortOrder": "Descending", "prettifyLogMessage": False},
            "targets": [target(expr)]}


def over(fn, field, by="persona", rng="$__auto"):
    return f"sum by ({by}) ({fn}({RUNS} | unwrap {field} [{rng}]))"


panels = [
    stat("Runs", f"sum(count_over_time({RUNS} [$__range]))", 0),
    stat("Success rate", f'sum(count_over_time({RUNS} | success="true" [$__range])) / '
                         f"sum(count_over_time({RUNS} [$__range]))", 4, "percentunit", 1),
    stat("Tokens", f"sum(sum_over_time({RUNS} | unwrap total_tokens [$__range]))", 8, "short"),
    stat("LLM calls", f"sum(sum_over_time({RUNS} | unwrap llm_calls [$__range]))", 12),
    stat("LLM errors", f"sum(sum_over_time({RUNS} | unwrap llm_errors [$__range]))", 16,
         desc="Includes the deliberate ones: wrong model name (403), over-long prompt (400), rate-limit bursts (429)."),
    stat("Spend (simulated $)", f"sum(sum_over_time({RUNS} | unwrap cost_usd [$__range]))", 20, "currencyUSD", 4,
         desc="From LiteLLM's x-litellm-response-cost header; pricing on the agentlab-* routes is simulated."),

    series("Runs per persona", f"sum by (persona) (count_over_time({RUNS} [$__auto]))", 0, 4),
    series("Tokens per persona", over("sum_over_time", "total_tokens"), 12, 4),
    series("LLM errors per persona", over("sum_over_time", "llm_errors"), 0, 12,
           desc="Deliberate errors are listed in each run's injected_errors field."),
    series("Spend per persona (simulated $)", over("sum_over_time", "cost_usd"), 12, 12, unit="currencyUSD"),
    series("p95 run latency", f"quantile_over_time(0.95, {RUNS} | unwrap latency_ms [$__auto]) by (persona)",
           0, 20, unit="ms", bars=False),
    series("Runs per host (source IP)", f"sum by (host) (count_over_time({RUNS} [$__auto]))", 12, 20,
           legend="{{host}}"),
    series("Tool executions per persona", over("sum_over_time", "tool_executions"), 0, 28),
    series("Failed runs per persona", f'sum by (persona) (count_over_time({RUNS} | success="false" [$__auto]))',
           12, 28),
    logs("Failed runs and daytime back-offs",
         f'{SEL} | persona=~"$persona" | host=~"$host" | success="false" or event="backoff"', 36),
    logs("All run lines", RUNS, 46, 12),
]

templating = [
    {"name": "persona", "label": "persona", "type": "custom", "multi": True, "includeAll": True,
     "query": "triage,sre,analyst,news,conversation,reviewer,chaos", "allValue": ".+",
     "current": {"text": "All", "value": ["$__all"]}},
    {"name": "host", "label": "host", "type": "custom", "multi": True, "includeAll": True,
     "query": "hv-rocky-linux-3,rasp5,rk1,rk2", "allValue": ".+",
     "current": {"text": "All", "value": ["$__all"]}},
]

dashboard = {
    "uid": "agent-lab", "title": "Agent Lab: AI agent traffic via LiteLLM", "tags": ["agent-lab", "litellm", "loki"],
    "description": "Scheduled AI agents (~/agent-lab on .101) calling LiteLLM (.101:4000) -> LM Studio qwen3.8-27b. "
                   "One JSON log line per agent run, shipped by Alloy to Loki.",
    "timezone": "browser", "schemaVersion": 39, "editable": False, "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"}, "panels": panels, "templating": {"list": templating},
}

out = sys.argv[1] if len(sys.argv) > 1 else "agent-lab.json"
with open(out, "w") as f:
    json.dump(dashboard, f, indent=1)
print(out)
