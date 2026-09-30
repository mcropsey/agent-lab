"""Shared LLM plumbing.

Every persona's HTTP client goes through LabTransport, which:
  * tags each chat request with lab/persona/host metadata (shows up in LiteLLM spend logs),
  * meters tokens, tool calls, latency and LiteLLM's response cost per run,
  * occasionally sends a request LiteLLM will refuse (wrong model name or an over-long prompt),
    so error handling shows up in the traffic. LiteLLM rejects both before they reach LM Studio.
"""
import json
import random
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx
from openai import AsyncOpenAI

from . import config

WRONG_MODEL_NAMES = ["agentlab-mian", "qwen3.8-27b-instruct", "gpt-4o-mini", "agentlab-large"]
# ~40k tokens: above the 32k max_input_tokens LiteLLM enforces for agentlab-main,
# below the per-key TPM limit, so the refusal comes from LiteLLM's context check.
_PADDING_LINE = "2026-09-29T03:14:07Z INFO checkout-api request_id=7f3a latency_ms=41 status=200 path=/api/cart\n"
OVERSIZED_PADDING = _PADDING_LINE * 1700

SLOW_CALL_SECS = 20
SLOW_CALL_TOK_S = 20


@dataclass
class RunStats:
    calls: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    llm_seconds: float = 0.0
    slow_calls: int = 0
    injected: list = field(default_factory=list)
    http_errors: list = field(default_factory=list)


class Run:
    """One execution of a persona: owns its clients and accumulates stats for the run log line."""

    def __init__(self, persona: str, framework: str, error_rate: float, trigger: str):
        self.persona = persona
        self.framework = framework
        self.trigger = trigger
        self.id = uuid.uuid4().hex[:12]
        self.model = config.persona_cfg(persona)["model"]
        self.error_rate = error_rate
        self.stats = RunStats()
        self.tool_executions = 0
        self.extra: dict = {}
        self._lock = threading.Lock()

    def http_client(self, key_name: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=LabTransport(self, key_name or self.persona),
            timeout=httpx.Timeout(700, connect=10),
        )

    def openai_client(self, key_name: str | None = None) -> AsyncOpenAI:
        key_name = key_name or self.persona
        return AsyncOpenAI(
            base_url=config.BASE_URL,
            api_key=config.key_for(key_name),
            http_client=self.http_client(key_name),
            max_retries=4,
        )

    def api_key(self, key_name: str | None = None) -> str:
        return config.key_for(key_name or self.persona)

    def tool_used(self) -> None:
        with self._lock:
            self.tool_executions += 1

    def record(self, fn) -> None:
        with self._lock:
            fn(self.stats)


class LabTransport(httpx.AsyncBaseTransport):
    def __init__(self, run: Run, key_name: str):
        self.run = run
        self.key_name = key_name
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        is_chat = request.method == "POST" and request.url.path.endswith("/chat/completions")
        if not is_chat:
            return await self.inner.handle_async_request(request)

        body = json.loads(await request.aread())
        body["metadata"] = {
            **(body.get("metadata") or {}),
            "tags": ["agent-lab", f"persona:{self.run.persona}", f"host:{config.HOST}"],
            "agentlab_run_id": self.run.id,
        }
        body["user"] = f"agent-{self.key_name}@{config.HOST}"

        if self.run.error_rate and random.random() < self.run.error_rate:
            if random.random() < 0.5:
                body["model"] = random.choice(WRONG_MODEL_NAMES)
                kind = "wrong_model"
            else:
                body["messages"] = list(body["messages"]) + [{"role": "user", "content": OVERSIZED_PADDING}]
                kind = "oversized_prompt"
            self.run.record(lambda s: s.injected.append(kind))

        headers = [(k, v) for k, v in request.headers.raw if k.lower() != b"content-length"]
        request = httpx.Request(request.method, request.url, headers=headers,
                                content=json.dumps(body).encode(), extensions=request.extensions)

        started = time.monotonic()
        response = await self.inner.handle_async_request(request)
        await response.aread()
        elapsed = time.monotonic() - started
        self._meter(response, elapsed)
        return response

    def _meter(self, response: httpx.Response, elapsed: float) -> None:
        def update(s: RunStats):
            s.calls += 1
            s.llm_seconds += elapsed
            if response.status_code >= 400:
                s.errors += 1
                s.http_errors.append(response.status_code)
                return
            try:
                data = response.json()
            except ValueError:
                return
            usage = data.get("usage") or {}
            out = usage.get("completion_tokens") or 0
            s.prompt_tokens += usage.get("prompt_tokens") or 0
            s.completion_tokens += out
            s.reasoning_tokens += (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
            for choice in data.get("choices") or []:
                s.tool_calls += len((choice.get("message") or {}).get("tool_calls") or [])
            try:
                s.cost_usd += float(response.headers.get("x-litellm-response-cost") or 0)
            except ValueError:
                pass
            if elapsed > SLOW_CALL_SECS and out / elapsed < SLOW_CALL_TOK_S:
                s.slow_calls += 1

        self.run.record(update)
