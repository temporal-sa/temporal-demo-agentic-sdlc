"""The worker: the pipeline Workflow, the agent Workflows and every Activity, on the copy's task queue
and on each leased session slot's queue (see slots.py).

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
from temporalio.client import Client
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
from agentic_sdlc.slots import SLOT_WORKER_LIMITS, Slot, SlotActivities
from agentic_sdlc.temporal_client import connect

log = logging.getLogger("agentic_sdlc.worker")


def build_pipeline_worker(client: Client, task_queue: str, *, graceful_shutdown: timedelta, **limits) -> Worker:
    """The pipeline Worker for one task queue: the copy's own queue, or a session slot's.

    Every queue it polls joins the same Worker Deployment Version, so a slot's runs are Pinned
    like any other. ``limits`` are Worker options (pollers, cache size) for slot Workers, of
    which one process hosts many.
    """
    deployment = None
    if config.BUILD_ID:
        deployment = WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name=config.DEPLOYMENT_NAME, build_id=config.BUILD_ID),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        )
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[IssueToProduction, DependencyRepair, PullRequestReview, Environments,
                   PlannerAgent, CoderAgent, ReviewerAgent],
        activities=[method for cls in ACTIVITY_CLASSES for method in cls(client).activities()],
        plugins=[AgentPlugin(agent) for agent in ALL_AGENTS],
        deployment_config=deployment,
        workflow_runner=workflow_runner(),
        graceful_shutdown_timeout=graceful_shutdown,
        **limits,
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    client = await connect(tools=ALL_TOOLS)

    config.check_label_length()

    # The copy's own queue: it registers this version with the Worker Deployment and runs the
    # GitHub Activities. In-flight calls finish; the pod's 600 second termination grace period
    # is the hard stop.
    worker = build_pipeline_worker(client, config.TASK_QUEUE, graceful_shutdown=timedelta(seconds=540))

    # Session slots (slots.py). Their pipeline Workers are hosted by an Activity that runs here, and
    # they stop at once, on a crash or a shutdown: what they were running is retried elsewhere.
    slots = SlotActivities(client, lambda queue: build_pipeline_worker(
        client, queue, graceful_shutdown=timedelta(0), **SLOT_WORKER_LIMITS))
    host_grace = timedelta(seconds=2)
    control = Worker(client, task_queue=config.HOST_QUEUE, workflows=[Slot], workflow_runner=workflow_runner(),
                     activities=slots.housekeeping_activities(), graceful_shutdown_timeout=host_grace)
    hosts = Worker(client, task_queue=config.version_host_queue(config.PIPELINE_VERSION),
                   activities=slots.host_activities(), max_concurrent_activities=config.MAX_SESSION_SLOTS + 5,
                   graceful_shutdown_timeout=host_grace)
    everything = (worker, control, hosts)

    # The catalog starts this as the container's PID 1 (no init), and PID 1 ignores SIGTERM unless
    # a handler is installed; without this the pod sits out its whole grace period and is killed.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: [asyncio.create_task(w.shutdown()) for w in everything])
    heartbeat = asyncio.create_task(_announce_liveness())
    log.info(
        "worker ready: task_queue=%s slots=%d pipeline=%s build_id=%s",
        config.TASK_QUEUE, config.SESSION_SLOTS, config.PIPELINE_VERSION, config.BUILD_ID or "(unversioned)",
    )
    try:
        await asyncio.gather(*(w.run() for w in everything))
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
