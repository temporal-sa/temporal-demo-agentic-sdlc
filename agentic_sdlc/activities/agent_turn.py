"""Send one message to an agent Workflow and wait for its answer.

Python Workflows can't send Updates to other Workflows, so the pipeline talks to its agents
through this Activity. The agent keeps running if this Activity's worker dies: the retry
re-sends with the same update ID (Temporal returns the original acceptance instead of
starting a second turn) and waits for the same message again.

The answer comes off the agent's own event stream (a Workflow Stream inside the agent Workflow):
the Activity reads from where its message was accepted until that message's handler ends.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.exceptions import ApplicationError

from temporal_agent_kit.agent_client import AgentClient
from temporal_agent_kit.agent_protocol import TURN_EVENTS_TOPIC, AgentEvent, AgentEventType, AgentMessageReply

HEARTBEAT_EVERY_SECONDS = 5
# Every stream read is an Update on the agent Workflow. Only the reply matters here, so read
# slowly rather than chasing every model delta.
REPLY_POLL_COOLDOWN = timedelta(seconds=1)


class AgentTurn(BaseModel):
    workflow_id: str
    handler: str
    payload: dict[str, Any]
    update_id: str


class RetriedCall(BaseModel):
    activity: str
    attempt: int
    last_failure: str


class TurnStats(BaseModel):
    """What the agent's Event History says happened during this turn."""

    model_calls: int = 0
    tool_calls: int = 0
    retried: list[RetriedCall] = []


class AgentTurnResult(BaseModel):
    output: dict[str, Any]
    cost_usd: float
    # More than 1 means a worker died while the turn ran and this Activity picked it back up.
    attempt: int
    stats: TurnStats


class AgentTurnActivities:
    def __init__(self, client: Client) -> None:
        self._client = client

    def activities(self) -> list:
        return [self.run_agent_turn]

    @activity.defn
    async def run_agent_turn(self, turn: AgentTurn) -> AgentTurnResult:
        agent = AgentClient(self._client, turn.workflow_id)
        heartbeat = asyncio.create_task(_heartbeat())
        try:
            accepted = await agent.submit_message(turn.handler, turn.payload, update_id=turn.update_id)
            output = await await_reply(self._client, turn.workflow_id, accepted)
        finally:
            heartbeat.cancel()
        usage = output.get("usage") or {}
        cost = estimate_cost(usage.get("model", ""), usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        output["usage"] = {**usage, "cost_usd": cost}
        stats = await self._turn_stats(turn)
        return AgentTurnResult(output=output, cost_usd=cost, attempt=activity.info().attempt, stats=stats)

    async def _turn_stats(self, turn: AgentTurn) -> TurnStats:
        """Count this turn's model and tool calls, and the ones that needed more than one
        attempt, from the agent's history. An Activity's Started event records its final
        attempt number and the failure that caused the last retry."""
        stats = TurnStats()
        in_turn = False
        scheduled: dict[int, str] = {}
        history = await self._client.get_workflow_handle(turn.workflow_id).fetch_history()
        for event in history.events:
            if event.HasField("workflow_execution_update_accepted_event_attributes"):
                accepted = event.workflow_execution_update_accepted_event_attributes
                in_turn = in_turn or accepted.accepted_request.meta.update_id == turn.update_id
            if not in_turn:
                continue
            if event.HasField("activity_task_scheduled_event_attributes"):
                name = event.activity_task_scheduled_event_attributes.activity_type.name
                scheduled[event.event_id] = name
                if "model_request" in name:
                    stats.model_calls += 1
                elif "event_stream_handler" not in name:
                    stats.tool_calls += 1
            elif event.HasField("activity_task_started_event_attributes"):
                started = event.activity_task_started_event_attributes
                if started.attempt > 1:
                    stats.retried.append(RetriedCall(
                        activity=scheduled.get(started.scheduled_event_id, "activity"),
                        attempt=started.attempt,
                        last_failure=started.last_failure.message[:200],
                    ))
        return stats


async def await_reply(
    client: Client, workflow_id: str, accepted: AgentMessageReply, *, poll_cooldown: timedelta = REPLY_POLL_COOLDOWN
) -> dict[str, Any]:
    """The output of one accepted message, read from the agent's stream from where it was
    accepted. Events are matched by the message's ID, as the kit's own subagent Activity does,
    because another message may share the turn."""
    stream = WorkflowStreamClient.create(client, workflow_id)
    async for item in stream.subscribe(TURN_EVENTS_TOPIC, from_offset=accepted.accepted_offset,
                                       result_type=AgentEvent, poll_cooldown=poll_cooldown):
        event: AgentEvent = item.data
        if event.turn_id != accepted.turn_id:
            continue
        if event.message_id == accepted.message_id:
            if event.event.type == AgentEventType.MESSAGE_HANDLER_END:
                return event.event.output
            if event.event.type == AgentEventType.MESSAGE_HANDLER_ERROR:
                raise ApplicationError(f"The agent's turn failed: {event.event.message}", type="AgentTurnFailed")
        if event.event.type == AgentEventType.TURN_END:
            break
    raise ApplicationError(f"The agent's turn {accepted.turn_number} ended without a reply", type="AgentTurnFailed")


async def _heartbeat() -> None:
    while True:
        activity.heartbeat()
        await asyncio.sleep(HEARTBEAT_EVERY_SECONDS)


def estimate_cost(model_key: str, input_tokens: int, output_tokens: int) -> float:
    """Estimated USD from the genai-prices database that ships with Pydantic AI. An estimate,
    not a bill: it ignores cache discounts and provider-specific rounding."""
    from genai_prices import Usage, calc_price

    provider, _, model = model_key.partition(":")
    try:
        price = calc_price(Usage(input_tokens=input_tokens, output_tokens=output_tokens),
                           model_ref=model, provider_id=provider)
    except Exception:  # noqa: BLE001 - an unpriced model costs "unknown", shown as $0
        return 0.0
    return round(float(price.total_price), 6)
