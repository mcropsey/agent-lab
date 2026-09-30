"""Rate-limit burst: several simultaneous requests on the agent-chaos key (1 in flight, 2 rpm).

LiteLLM answers the excess with 429s itself; at most one tiny request reaches LM Studio.
Uses plain httpx with no retries so every 429 is visible.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

from . import config
from .runner import emit

BURST_SIZE = 5


def _one(client: httpx.Client) -> int:
    body = {"model": "agentlab-main", "max_tokens": 64,
            "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
            "metadata": {"tags": ["agent-lab", "persona:chaos", f"host:{config.HOST}"]},
            "user": f"agent-chaos@{config.HOST}"}
    try:
        return client.post(f"{config.BASE_URL}/chat/completions", json=body).status_code
    except httpx.HTTPError:
        return 0


def burst() -> None:
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {config.key_for('chaos')}"}
    with httpx.Client(headers=headers, timeout=120) as client, ThreadPoolExecutor(BURST_SIZE) as pool:
        codes = list(pool.map(lambda _: _one(client), range(BURST_SIZE)))
    emit({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lab": "agent-lab",
        "event": "run",
        "host": config.HOST,
        "persona": "chaos",
        "framework": "httpx",
        "model": "agentlab-main",
        "trigger": "schedule",
        "success": True,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "llm_calls": len(codes),
        "llm_errors": sum(1 for c in codes if c != 200),
        "http_errors": [c for c in codes if c != 200],
        "injected_errors": ["rate_limit_burst"],
        "rate_limited": codes.count(429),
    })
