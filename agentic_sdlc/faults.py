"""Presenter-injected faults: demo machinery, kept out of the Workflow code on purpose.

The presenter arms a fault for one Workflow (a coding agent, or the pipeline). The next
Activity attempts that Workflow runs fail as if the provider had rate limited them, and
Temporal's Retry Policy carries the work through. Faults live in Redis with a TTL, so a
forgotten one expires on its own, and reset clears them.
"""

from __future__ import annotations

from typing import Literal

from redis.asyncio import Redis

from agentic_sdlc import config

FaultKind = Literal["model_rate_limit", "github_rate_limit"]
FAULT_TTL_SECONDS = 600

_redis: Redis | None = None


def _client() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(config.REDIS_URL)
    return _redis


def _key(workflow_id: str, kind: FaultKind) -> str:
    return f"faults:{config.TASK_QUEUE}:{workflow_id}:{kind}"


async def arm(workflow_id: str, kind: FaultKind, attempts: int = 2) -> None:
    await _client().set(_key(workflow_id, kind), attempts, ex=FAULT_TTL_SECONDS)


async def take(workflow_id: str, kind: FaultKind) -> bool:
    """Consume one injected failure for this Workflow, if any are armed."""
    key = _key(workflow_id, kind)
    remaining = await _client().decr(key)
    if remaining < 0:
        await _client().delete(key)
        return False
    return True


async def armed(workflow_ids: list[str]) -> dict[str, list[str]]:
    """Which of these Workflows have faults armed, for the UI's labels."""
    result: dict[str, list[str]] = {}
    for workflow_id in workflow_ids:
        for kind in ("model_rate_limit", "github_rate_limit"):
            if int(await _client().get(_key(workflow_id, kind)) or 0) > 0:
                result.setdefault(workflow_id, []).append(kind)
    return result


async def clear(workflow_ids: list[str]) -> None:
    for workflow_id in workflow_ids:
        await _client().delete(_key(workflow_id, "model_rate_limit"), _key(workflow_id, "github_rate_limit"))
