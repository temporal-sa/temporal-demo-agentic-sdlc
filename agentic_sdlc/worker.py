"""The worker: the pipeline Workflow, the agent Workflows and every Activity, on one task queue.

Run it with ``python -m agentic_sdlc.worker``. It reads TEMPORAL_TASK_QUEUE (the catalog sets
a per-workspace value), and when TEMPORAL_WORKER_BUILD_ID is set it joins a Worker Deployment
with Pinned Workflows, so a run finishes on the version it started on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import timedelta

from pydantic_ai.durable_exec.temporal import AgentPlugin
from redis.asyncio import Redis
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

from agentic_sdlc import config
from agentic_sdlc.activities import ACTIVITY_CLASSES
from agentic_sdlc.agents.runtime import ALL_AGENTS
from agentic_sdlc.agents.tools import ALL_TOOLS
from agentic_sdlc.agents.workflows import CoderAgent, PlannerAgent, ReviewerAgent
from agentic_sdlc.pipeline.dependency import DependencyRepair
from agentic_sdlc.pipeline.environments import Environments
from agentic_sdlc.pipeline.review import PullRequestReview
from agentic_sdlc.pipeline.workflow import IssueToProduction
from agentic_sdlc.sandbox import workflow_runner
from agentic_sdlc.temporal_client import connect

log = logging.getLogger("agentic_sdlc.worker")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    client = await connect(tools=ALL_TOOLS)

    deployment = None
    if config.BUILD_ID:
        deployment = WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name=config.DEPLOYMENT_NAME, build_id=config.BUILD_ID),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        )

    activities = [method for cls in ACTIVITY_CLASSES for method in cls(client).activities()]
    worker = Worker(
        client,
        task_queue=config.TASK_QUEUE,
        workflows=[IssueToProduction, DependencyRepair, PullRequestReview, Environments,
                   PlannerAgent, CoderAgent, ReviewerAgent],
        activities=activities,
        plugins=[AgentPlugin(agent) for agent in ALL_AGENTS],
        deployment_config=deployment,
        workflow_runner=workflow_runner(),
        # In-flight model calls finish; the pod's 600 second termination grace period is the hard stop.
        graceful_shutdown_timeout=timedelta(seconds=540),
    )
    # The catalog starts this as the container's PID 1 (no init), and PID 1 ignores SIGTERM unless
    # a handler is installed; without this the pod sits out its whole grace period and is killed.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.shutdown()))
    heartbeat = asyncio.create_task(_announce_liveness())
    log.info(
        "worker ready: task_queue=%s pipeline=%s build_id=%s",
        config.TASK_QUEUE, config.PIPELINE_VERSION, config.BUILD_ID or "(unversioned)",
    )
    try:
        await worker.run()
    finally:
        heartbeat.cancel()


async def _announce_liveness() -> None:
    """Tell the console this worker is alive: a Redis key with a 6 second TTL, refreshed every
    two seconds. When the worker is killed the key expires and the console shows the gap."""
    redis = Redis.from_url(config.REDIS_URL)
    key = f"workers:{config.TASK_QUEUE}:{os.getpid()}"
    info = json.dumps({"pid": os.getpid(), "version": config.PIPELINE_VERSION,
                       "build_id": config.BUILD_ID or None, "started_at": time.time()})
    while True:
        await redis.set(key, info, ex=6)
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
