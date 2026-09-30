"""APScheduler loop for the personas listed in $PERSONAS (comma-separated; may include 'chaos').

Each persona reschedules itself after every wake-up. The wait comes from the persona's
runs_per_hour x global intensity x the time-of-day multiplier, plus jitter. Waits are capped at
max_wait_minutes: when the real mean wait is longer, a capped wake-up runs only with probability
cap/mean. That keeps the average rate right and lets config/time-of-day changes apply quickly.
"""
import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from . import chaos, config
from .runner import PERSONAS, emit, execute

logging.basicConfig(level=logging.WARNING)
DAYTIME_BACKOFF = timedelta(minutes=30)
_paused_until: datetime | None = None


def time_multiplier(now: datetime, g: dict) -> float:
    s = g["schedule"]
    return s["day_rate"] if s["day_start_hour"] <= now.hour < s["day_end_hour"] else s["night_rate"]


def is_daytime(now: datetime, g: dict) -> bool:
    s = g["schedule"]
    return s["day_start_hour"] <= now.hour < s["day_end_hour"]


def plan_next(rate_per_hour: float, g: dict) -> tuple[float, float]:
    """Returns (seconds to wait, probability of actually running on that wake-up)."""
    cap = g["max_wait_minutes"] * 60
    jitter = random.uniform(1 - g["jitter"], 1 + g["jitter"])
    if rate_per_hour <= 0:
        return cap * jitter, 0.0
    mean = 3600 / rate_per_hour
    if mean <= cap:
        return max(30.0, mean * jitter), 1.0
    return cap * jitter, cap / mean


def main() -> None:
    wanted = [p.strip() for p in os.environ.get("PERSONAS", "").split(",") if p.strip()]
    unknown = [p for p in wanted if p not in PERSONAS + ["chaos"]]
    if not wanted or unknown:
        raise SystemExit(f"PERSONAS must list some of {PERSONAS + ['chaos']}; got {wanted}")

    tz = ZoneInfo(config.load()["global"]["timezone"])
    sched = BlockingScheduler(timezone=tz, executors={"default": ThreadPoolExecutor(len(wanted))},
                              job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600})

    def schedule(name: str, delay: float, probability: float) -> None:
        sched.add_job(tick, "date", run_date=datetime.now(tz) + timedelta(seconds=delay),
                      args=[name, probability], id=f"{name}-{random.getrandbits(32):x}")

    def tick(name: str, probability: float) -> None:
        global _paused_until
        cfg = config.load()
        g = cfg["global"]
        now = datetime.now(tz)
        is_chaos = name == "chaos"
        enabled = g["enabled"] and (True if is_chaos else cfg["personas"][name]["enabled"])
        paused = _paused_until is not None and now < _paused_until

        if enabled and not paused and random.random() < probability:
            if is_chaos:
                chaos.burst()
            else:
                run = asyncio.run(execute(name))
                if run.stats.slow_calls and is_daytime(now, g):
                    _paused_until = datetime.now(tz) + DAYTIME_BACKOFF
                    emit({"ts": datetime.now(tz).isoformat(timespec="seconds"), "lab": "agent-lab",
                          "event": "backoff", "host": config.HOST, "persona": name,
                          "paused_until": _paused_until.isoformat(timespec="seconds"),
                          "reason": "slow LLM response in daytime - GPU likely busy with interactive work"})

        base = g["chaos_bursts_per_day"] / 24 if is_chaos else cfg["personas"][name]["runs_per_hour"]
        rate = base * g["intensity"] * time_multiplier(datetime.now(tz), g) if enabled else 0.0
        schedule(name, *plan_next(rate, g))

    for name in wanted:
        schedule(name, random.uniform(30, 300), 0.0)  # first wake-up only plans, so a restart doesn't cause a burst
    emit({"ts": datetime.now(tz).isoformat(timespec="seconds"), "lab": "agent-lab", "event": "scheduler_start",
          "host": config.HOST, "personas": wanted})
    sched.start()


if __name__ == "__main__":
    main()
