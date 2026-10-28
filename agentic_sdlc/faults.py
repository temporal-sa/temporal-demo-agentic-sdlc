"""Presenter-injected faults: demo machinery, kept out of the Workflow code on purpose.

The presenter arms a fault for one Workflow (a coding agent, or the pipeline). The next
Activity attempts that Workflow runs fail as if the provider had rate limited them, and
Temporal's Retry Policy carries the work through. Faults live in Redis with a TTL, so a
forgotten one expires on its own, and reset clears them.

Every key is scoped to a session slot's task queue. The web server and the Worker both pass it
in: in the Worker, ``config.TASK_QUEUE`` is the copy's queue, which is never the slot's.
"""

from __future__ import annotations

import time
from typing import Literal

from redis.asyncio import Redis

from agentic_sdlc import config

FaultKind = Literal["model_rate_limit", "github_rate_limit"]
FAULT_TTL_SECONDS = 600
Provider = Literal["anthropic", "openai", "google"]
PROVIDERS: tuple[Provider, ...] = ("anthropic", "openai", "google")

_redis: Redis | None = None


def _client() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(config.REDIS_URL)
    return _redis


def _key(queue: str, workflow_id: str, kind: FaultKind) -> str:
    return f"faults:{queue}:{workflow_id}:{kind}"


async def arm(queue: str, workflow_id: str, kind: FaultKind, attempts: int = 2) -> None:
    await _client().set(_key(queue, workflow_id, kind), attempts, ex=FAULT_TTL_SECONDS)


async def take(queue: str, workflow_id: str, kind: FaultKind) -> bool:
    """Consume one injected failure for this Workflow, if any are armed."""
    key = _key(queue, workflow_id, kind)
    remaining = await _client().decr(key)
    if remaining < 0:
        await _client().delete(key)
        return False
    return True


async def armed(queue: str, workflow_ids: list[str]) -> dict[str, list[str]]:
    """Which of these Workflows have faults armed, for the UI's labels."""
    result: dict[str, list[str]] = {}
    for workflow_id in workflow_ids:
        for kind in ("model_rate_limit", "github_rate_limit"):
            if int(await _client().get(_key(queue, workflow_id, kind)) or 0) > 0:
                result.setdefault(workflow_id, []).append(kind)
    return result


async def clear_slot(queue: str) -> None:
    """Every fault armed on a session slot, for its reset."""
    async for key in _client().scan_iter(f"faults:{queue}:*"):
        await _client().delete(key)


async def break_canary(queue: str, sha: str, seconds: int) -> float:
    """Make the canary commit's assets fail for a while. Keyed by commit, so the fault ends by
    itself when the canary is promoted or rolled back. Returns when it ends (epoch seconds)."""
    until = time.time() + seconds
    await _client().set(f"faults:{queue}:canary:{sha}", until, ex=seconds)
    return until


async def canary_fault(queue: str, sha: str | None) -> float | None:
    """When the canary's fault ends, or None if it isn't broken."""
    if not sha:
        return None
    raw = await _client().get(f"faults:{queue}:canary:{sha}")
    return float(raw) if raw else None


# A provider outage is a switch, not a count: every model call to the provider fails until the
# presenter turns it off, and the Activities' Retry Policy keeps backing off meanwhile. The TTL is
# the safety net for one that is forgotten.


def _outage_key(queue: str, provider: Provider) -> str:
    return f"faults:{queue}:provider_outage:{provider}"


async def set_outage(queue: str, provider: Provider, on: bool) -> float | None:
    """Turn a provider outage on or off for a slot. Returns when it clears by itself, if on."""
    if not on:
        await _client().delete(_outage_key(queue, provider))
        return None
    until = time.time() + FAULT_TTL_SECONDS
    await _client().set(_outage_key(queue, provider), until, ex=FAULT_TTL_SECONDS)
    return until


async def outage(queue: str, provider: Provider) -> bool:
    return bool(await _client().exists(_outage_key(queue, provider)))


async def outages(queue: str) -> dict[str, float]:
    """Provider -> when its outage clears by itself, for the providers that are down."""
    found = await _client().mget([_outage_key(queue, provider) for provider in PROVIDERS])
    return {provider: float(raw) for provider, raw in zip(PROVIDERS, found) if raw}
