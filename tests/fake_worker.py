"""A worker process for the agents on the fake model, which a test can kill outright.

    python tests/fake_worker.py <task queue>

Reads TEMPORAL_ADDRESS and FAKE_MODEL_BEHAVIOR. Prints "ready" once it polls.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_model  # noqa: E402

fake_model.install()

from pydantic_ai.durable_exec.temporal import AgentPlugin  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from agentic_sdlc.agents import runtime, workflows  # noqa: E402
from agentic_sdlc.agents.tools import ALL_TOOLS  # noqa: E402
from agentic_sdlc.sandbox import workflow_runner  # noqa: E402
from agentic_sdlc.temporal_client import connect  # noqa: E402


async def main(task_queue: str) -> None:
    client = await connect(tools=ALL_TOOLS)
    async with Worker(client, task_queue=task_queue, workflows=[workflows.PlannerAgent],
                      plugins=[AgentPlugin(agent) for agent in runtime.ALL_AGENTS], workflow_runner=workflow_runner()):
        print("ready", flush=True)
        await asyncio.Event().wait()


asyncio.run(main(sys.argv[1]))
