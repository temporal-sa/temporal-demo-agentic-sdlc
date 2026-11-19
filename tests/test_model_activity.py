"""The model Activity under the kit's durability capability: which provider failures retry, and
how fast a call is picked up again when its worker dies.

Real agent Workflows, Pydantic AI agents, ``DemoModel`` and capability, on a local dev server; only
the model is the scripted fake (fake_model.py). The pipeline's agent-turn Activity sends the turn.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.client import ActivityFailureError, Client
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import fake_model
from agentic_sdlc import config, faults
from agentic_sdlc.activities.agent_turn import AgentTurn, AgentTurnActivities, AgentTurnResult
from agentic_sdlc.agents import runtime, workflows
from agentic_sdlc.agents.tools import ALL_TOOLS
from agentic_sdlc.models import AgentInit, ModelChoice, PlanRequest
from agentic_sdlc.sandbox import workflow_runner
from agentic_sdlc.temporal_client import connect
from temporal_agent_kit.agent_protocol import AgentConfig

FAKE_WORKER = Path(__file__).resolve().parent / "fake_worker.py"

pytestmark = pytest.mark.skipif(shutil.which("redis-server") is None, reason="needs redis-server")


@pytest.fixture
async def client(monkeypatch, redis_url):
    # DemoModel reads the console's faults from Redis on every model call.
    monkeypatch.setattr(config, "REDIS_URL", redis_url)
    monkeypatch.setattr(faults, "_redis", None)
    monkeypatch.setenv("REDIS_URL", redis_url)  # for the worker process
    async with await WorkflowEnvironment.start_local() as env:
        monkeypatch.setenv("TEMPORAL_ADDRESS", env.client.service_client.config.target_host)
        yield await connect(tools=ALL_TOOLS)


def agent_worker(client: Client, queue: str, *, with_turns: bool = False) -> Worker:
    """The agents on ``queue``, and with ``with_turns`` the agent-turn Activity as well."""
    return Worker(client, task_queue=queue, workflows=[workflows.PlannerAgent],
                  activities=AgentTurnActivities(client).activities() if with_turns else [],
                  plugins=[AgentPlugin(agent) for agent in runtime.ALL_AGENTS], workflow_runner=workflow_runner())


async def start_planner(client: Client, queue: str) -> str:
    agent_id = f"planner-{uuid.uuid4().hex[:6]}"
    await client.start_workflow("PlannerAgent", args=[AgentConfig(), AgentInit(
        run_id="issue-1", model=ModelChoice(provider="google", model="fake"))], id=agent_id, task_queue=queue)
    return agent_id


async def clarify(client: Client, agent_id: str, queue: str, *, attempts: int = 1) -> AgentTurnResult:
    """One agent turn through the agent-turn Activity, as the pipeline sends it."""
    return await client.execute_activity(
        "run_agent_turn",
        AgentTurn(workflow_id=agent_id, handler="clarify", update_id=f"{agent_id}:clarify:1",
                  payload=PlanRequest(issue_title="Free shipping", issue_body="Over $75").model_dump(mode="json")),
        id=f"{agent_id}-turn", task_queue=queue, result_type=AgentTurnResult,
        schedule_to_close_timeout=timedelta(seconds=90), retry_policy=RetryPolicy(maximum_attempts=attempts))


class ModelCall:
    """One model Activity from the agent's history. Its Started event is recorded with the final
    attempt, carrying the failure that caused the last retry."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.attempt = 0
        self.last_failure = ""
        self.failure_type = ""  # how the final attempt failed, if it did
        self.non_retryable = False


async def model_calls(client: Client, agent_id: str) -> list[ModelCall]:
    calls: dict[int, ModelCall] = {}
    async for event in client.get_workflow_handle(agent_id).fetch_history_events():
        if event.HasField("activity_task_scheduled_event_attributes"):
            name = event.activity_task_scheduled_event_attributes.activity_type.name
            if "model_request" in name:
                calls[event.event_id] = ModelCall(name)
        elif event.HasField("activity_task_started_event_attributes"):
            started = event.activity_task_started_event_attributes
            if call := calls.get(started.scheduled_event_id):
                call.attempt, call.last_failure = started.attempt, started.last_failure.message
        elif event.HasField("activity_task_failed_event_attributes"):
            failed = event.activity_task_failed_event_attributes
            if call := calls.get(failed.scheduled_event_id):
                info = failed.failure.application_failure_info
                call.failure_type, call.non_retryable = info.type, info.non_retryable
    return list(calls.values())


async def test_a_rejected_request_is_not_retried(client, monkeypatch):
    """A provider's 400 becomes ProviderRejected (DemoModel), which the capability's normalized
    retry policy still treats as non-retryable: one attempt, and the turn fails."""
    monkeypatch.setattr(fake_model.SCRIPT, "behavior", "reject")
    monkeypatch.setattr(fake_model.SCRIPT, "calls", 0)
    queue = f"q-{uuid.uuid4().hex[:6]}"
    async with agent_worker(client, queue, with_turns=True):
        agent_id = await start_planner(client, queue)
        with pytest.raises(ActivityFailureError):
            await clarify(client, agent_id, queue)
    [call] = await model_calls(client, agent_id)
    assert (call.attempt, call.failure_type, call.non_retryable) == (1, "ProviderRejected", True)
    assert fake_model.SCRIPT.calls == 1


async def test_an_unavailable_provider_is_retried(client, monkeypatch):
    """A provider's 503 is retried by the model Activity's Retry Policy, and the turn completes."""
    monkeypatch.setattr(fake_model.SCRIPT, "behavior", "unavailable_once")
    monkeypatch.setattr(fake_model.SCRIPT, "calls", 0)
    queue = f"q-{uuid.uuid4().hex[:6]}"
    async with agent_worker(client, queue, with_turns=True):
        agent_id = await start_planner(client, queue)
        turn = await clarify(client, agent_id, queue)
    assert turn.output["clarification"] == {"questions": []}
    [call] = await model_calls(client, agent_id)
    assert call.attempt == 2
    # The failure is recorded, not lost to a cause chain that loops (DemoModel used to raise the
    # provider's error from itself). With its stack traces it is about 8 KB, and the dev server
    # records "Failure exceeds size limit." as the retried attempt's last failure instead.
    assert "recursion" not in call.last_failure
    assert fake_model.SCRIPT.calls == 2


async def test_a_model_call_on_a_killed_worker_is_retried_on_another_within_seconds(client, monkeypatch):
    """The capability heartbeats the model Activity, so when its worker is killed mid-call the
    10 second heartbeat timeout fails the attempt and another worker retries it. Without heartbeats
    nothing would notice until the 10 minute start-to-close timeout."""
    monkeypatch.setattr(fake_model.SCRIPT, "behavior", "answer")  # for the worker in this process
    agents, turns = f"agents-{uuid.uuid4().hex[:6]}", f"turns-{uuid.uuid4().hex[:6]}"
    doomed = subprocess.Popen([sys.executable, str(FAKE_WORKER), agents], stdout=subprocess.PIPE, text=True,
                              env={**os.environ, "FAKE_MODEL_BEHAVIOR": "hang"})
    try:
        assert doomed.stdout.readline().strip() == "ready"
        async with Worker(client, task_queue=turns, activities=AgentTurnActivities(client).activities()):
            agent_id = await start_planner(client, agents)
            turn = asyncio.create_task(clarify(client, agent_id, turns))

            async def model_call_running() -> bool:
                pending = (await client.get_workflow_handle(agent_id).describe()).raw_description.pending_activities
                return any("model_request" in a.activity_type.name and a.last_started_time.seconds for a in pending)

            async with asyncio.timeout(30):
                while not await model_call_running():
                    await asyncio.sleep(0.2)
            await asyncio.sleep(1)  # let it heartbeat at least once
            doomed.send_signal(signal.SIGKILL)
            doomed.wait()
            killed_at = time.monotonic()

            async with agent_worker(client, agents):
                result = await turn
            recovered_in = time.monotonic() - killed_at
    finally:
        if doomed.poll() is None:
            doomed.kill()

    assert result.output["clarification"] == {"questions": []}
    [call] = await model_calls(client, agent_id)
    assert call.attempt == 2 and "heartbeat" in call.last_failure.lower()
    # 10 s heartbeat timeout, then the Retry Policy's 2 s first interval.
    assert recovered_in < 20, f"the model call took {recovered_in:.1f}s to recover"
    print(f"\nmodel activity {call.name!r} recovered on another worker {recovered_in:.1f}s after the kill")
