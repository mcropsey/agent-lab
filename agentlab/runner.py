"""Executes one persona run and writes exactly one structured JSON log line for it."""
import asyncio
import importlib
import json
import sys
import time
from datetime import datetime, timezone

from . import config
from .llm import Run

PERSONAS = ["triage", "sre", "analyst", "news", "conversation", "reviewer"]
RUN_TIMEOUT_SECS = 900


def emit(record: dict) -> None:
    print(json.dumps(record, default=str), flush=True)


async def execute(persona: str, trigger: str = "schedule", inject_errors: bool = True) -> Run:
    module = importlib.import_module(f"agentlab.personas.{persona}")
    cfg = config.load()
    run = Run(persona, module.FRAMEWORK, cfg["global"]["error_rate"] if inject_errors else 0.0, trigger)
    started = time.monotonic()
    ok, error = True, None
    try:
        await asyncio.wait_for(module.run(run), timeout=RUN_TIMEOUT_SECS)
    except Exception as e:  # a failed run is still a data point - log it and move on
        ok, error = False, f"{type(e).__name__}: {str(e)[:300]}"
    run.ok = ok
    s = run.stats
    emit({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lab": "agent-lab",
        "event": "run",
        "host": config.HOST,
        "persona": persona,
        "framework": run.framework,
        "model": run.model,
        "run_id": run.id,
        "trigger": trigger,
        "success": ok,
        "error": error,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "llm_calls": s.calls,
        "llm_errors": s.errors,
        "http_errors": s.http_errors,
        "injected_errors": s.injected,
        "prompt_tokens": s.prompt_tokens,
        "completion_tokens": s.completion_tokens,
        "reasoning_tokens": s.reasoning_tokens,
        "total_tokens": s.prompt_tokens + s.completion_tokens,
        "tool_calls": s.tool_calls,
        "tool_executions": run.tool_executions,
        "cost_usd": round(s.cost_usd, 6),
        "slow_calls": s.slow_calls,
        **run.extra,
    })
    return run


def main() -> None:
    """python -m agentlab.runner <persona> [--no-errors]   (one manual run, ignores schedule/kill switch)"""
    if len(sys.argv) < 2 or sys.argv[1] not in PERSONAS:
        sys.exit(f"usage: python -m agentlab.runner <{'|'.join(PERSONAS)}> [--no-errors]")
    run = asyncio.run(execute(sys.argv[1], trigger="manual", inject_errors="--no-errors" not in sys.argv))
    sys.exit(0 if run.ok else 1)


if __name__ == "__main__":
    main()
