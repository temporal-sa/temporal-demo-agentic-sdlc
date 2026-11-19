"""The console's API with two browsers: who gets a slot, and what one can see and do to another.

Each browser is an HTTP client with its own cookie jar, talking to the real app. Behind it are a
real Temporal server, a real Redis and the real Slot Workflow and reset Activity; only GitHub and
the pipeline Workers are fakes (the agent-events test runs a real agent on a fake model). What's under test is isolation: a browser sees, changes and
crashes only the slot it holds, and polling never keeps a slot alive.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import subprocess
import time
import uuid
from collections import Counter
from datetime import timedelta

import httpx
import pytest
from redis.asyncio import Redis
from temporalio import activity, workflow
from temporalio.client import WithStartWorkflowOperation, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentic_sdlc import config, faults
from agentic_sdlc.activities.github import CleanupInput, ScenarioInput, ScenarioStarted
from agentic_sdlc.models import RunRequest, RunState
from agentic_sdlc.pipeline.base import GITHUB_REFS
from agentic_sdlc.pipeline.environments import DeployRequest, Environments
from agentic_sdlc.sandbox import workflow_runner
from agentic_sdlc.slots import HostSlotInput, Slot, SlotActivities
from agentic_sdlc.web import sessions
from agentic_sdlc.web.app import app, state

pytestmark = pytest.mark.skipif(shutil.which("redis-server") is None, reason="needs redis-server")


@workflow.defn(name="IssueToProduction", sandboxed=False)
class FakeRun:
    @workflow.run
    async def run(self, request: RunRequest) -> None:
        await workflow.wait_condition(lambda: False)


class Fakes:
    def __init__(self) -> None:
        self.hosting: Counter[str] = Counter()  # slot queue -> hosted Workers
        self.scenarios: list[ScenarioInput] = []
        self.cleanups: list[CleanupInput] = []


@pytest.fixture
async def redis_url():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen(["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"redis://127.0.0.1:{port}"
    redis = Redis.from_url(url)
    for _ in range(50):
        with contextlib.suppress(Exception):
            if await redis.ping():
                break
        await asyncio.sleep(0.1)
    yield url
    await redis.aclose()
    server.terminate()
    server.wait(timeout=10)


@pytest.fixture
async def demo(redis_url, monkeypatch):
    """The app wired to a real Temporal server and Redis, for a copy of the demo with two slots."""
    copy = f"t{uuid.uuid4().hex[:6]}"
    for name, value in {"TASK_QUEUE": copy, "SESSION_SLOTS": 2, "REDIS_URL": redis_url, "HOST_QUEUE": f"{copy}-host",
                        "GITHUB_WEBHOOK_SECRET": "test-secret", "GITHUB_SANDBOX_REPO": "temporal-sa/test"}.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(faults, "_redis", None)
    sessions._lookups.clear()
    state.view_cache.clear()
    state.env_cache.clear()

    fakes = Fakes()

    @activity.defn(name="host_slot")
    async def host_slot(input: HostSlotInput) -> None:
        fakes.hosting[input.queue] += 1
        try:
            while True:
                activity.heartbeat()
                await asyncio.sleep(0.2)
        finally:
            fakes.hosting[input.queue] -= 1

    @activity.defn(name="open_issue_for_copy")
    async def open_issue(input: ScenarioInput) -> ScenarioStarted:
        fakes.scenarios.append(input)
        return ScenarioStarted(url="https://github.com/temporal-sa/test/issues/1", kind="issue")

    @activity.defn(name="cleanup_runs")
    async def cleanup_runs(input: CleanupInput) -> int:
        fakes.cleanups.append(input)
        return 0

    async with await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter,
                                                      search_attributes=[GITHUB_REFS]) as env:
        client = env.client
        state.temporal = client
        state.redis = Redis.from_url(redis_url)
        slots = SlotActivities(client, lambda queue: None)  # only its reset Activity is used
        async with contextlib.AsyncExitStack() as stack:
            for worker in (
                Worker(client, task_queue=config.host_queue(copy), workflows=[Slot], workflow_runner=workflow_runner(),
                       activities=slots.housekeeping_activities()),
                Worker(client, task_queue=config.version_host_queue("v1", copy), activities=[host_slot]),
                Worker(client, task_queue=config.version_host_queue("v2", copy), activities=[host_slot]),
                Worker(client, task_queue=copy, activities=[open_issue, cleanup_runs]),
                *(Worker(client, task_queue=config.slot_queue(n, copy), workflows=[Environments, FakeRun],
                         workflow_runner=workflow_runner()) for n in (1, 2)),
            ):
                await stack.enter_async_context(worker)
            yield client, fakes, copy
        await state.redis.aclose()


def browser() -> httpx.AsyncClient:
    """A fresh browser: its own cookie jar."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://console")


async def start_scenario(browser: httpx.AsyncClient) -> httpx.Response:
    return await browser.post("/api/scenarios", json={"kind": "issue", "title": "Free shipping", "body": "Please"})


async def slot_of(browser: httpx.AsyncClient) -> dict | None:
    return (await browser.get("/api/config")).json()["slot"]


async def until(condition, timeout: float = 15.0) -> None:
    async with asyncio.timeout(timeout):
        while not await condition():
            await asyncio.sleep(0.1)


async def test_a_browser_holds_no_slot_until_it_starts_a_scenario(demo):
    client, _fakes, copy = demo
    async with browser() as maya:
        assert await slot_of(maya) is None
        overview = (await maya.get("/api/overview")).json()
        assert (overview["runs"], overview["workers"], overview["slot"]) == ([], [], None)
        assert [w async for w in client.list_workflows(f"WorkflowId STARTS_WITH '{copy}.slot-'")] == []
        assert (await maya.post("/api/slot/crash")).status_code == 409  # nothing to crash yet


async def test_scenarios_take_different_slots_and_are_labeled_for_them(demo):
    _client, fakes, copy = demo
    async with browser() as maya, browser() as sam:
        assert (await start_scenario(maya)).status_code == 200
        assert (await start_scenario(sam)).status_code == 200
        assert (await slot_of(maya))["queue"] == config.slot_queue(1, copy)
        assert (await slot_of(sam))["queue"] == config.slot_queue(2, copy)
        assert [s.owner_label for s in fakes.scenarios] == [f"agentic-sdlc:{config.slot_queue(n, copy)}" for n in (1, 2)]
        # Starting another scenario keeps the slot a browser already holds.
        assert (await start_scenario(maya)).status_code == 200
        assert (await slot_of(maya))["n"] == 1


async def test_a_browser_cannot_see_or_reach_another_browsers_runs(demo):
    client, _fakes, copy = demo
    async with browser() as maya, browser() as sam:
        await start_scenario(maya)
        await start_scenario(sam)
        mine = config.slot_queue(1, copy)
        await client.start_workflow(
            "IssueToProduction", RunRequest(run_id="issue-12", kind="issue", repo="x", title="Free shipping", body=""),
            id=f"{mine}.gh.issue-12", task_queue=mine,
            memo={"run": RunState(run_id="issue-12", kind="issue", version="", title="Free shipping", budget_usd=3.0,
                                  started_at=time.time(), steps=[]).model_dump(mode="json")})

        async def listed(who):
            await asyncio.sleep(0)  # let Visibility catch up below
            return [r["run_id"] for r in (await who.get("/api/overview")).json()["runs"]]

        await until(lambda: _is(listed(maya), ["issue-12"]))
        assert await listed(sam) == []
        assert (await maya.get("/api/runs/issue-12")).status_code == 200
        assert (await sam.get("/api/runs/issue-12")).status_code == 404
        assert (await sam.post("/api/runs/issue-12/stop")).status_code == 404
        assert (await sam.post("/api/faults", json={"kind": "model_rate_limit", "workflow_id": f"{mine}.gh.issue-12"})
                ).status_code == 404
        # And a browser with no slot at all sees nothing.
        async with browser() as stranger:
            assert (await stranger.get("/api/runs/issue-12")).status_code == 404
            assert (await stranger.get("/api/overview")).json()["runs"] == []
        await client.get_workflow_handle(f"{mine}.gh.issue-12").terminate()


async def _is(awaitable, expected) -> bool:
    return await awaitable == expected


async def test_crashing_one_browsers_worker_leaves_the_other_running(demo):
    _client, fakes, copy = demo
    async with browser() as maya, browser() as sam:
        await start_scenario(maya)
        await start_scenario(sam)
        mine, theirs = config.slot_queue(1, copy), config.slot_queue(2, copy)
        await until(lambda: _true(fakes.hosting[mine] == 2 and fakes.hosting[theirs] == 2))

        crashed = (await maya.post("/api/slot/crash")).json()["slot"]
        assert crashed["crashed"] and crashed["auto_restore_at"]
        assert fakes.hosting[mine] == 0 and fakes.hosting[theirs] == 2
        assert (await slot_of(maya))["crashed"] and not (await slot_of(sam))["crashed"]

        restored = (await maya.post("/api/slot/restore")).json()["slot"]
        assert not restored["crashed"]
        await until(lambda: _true(fakes.hosting[mine] == 2))


async def _true(value: bool) -> bool:
    return value


async def test_reset_frees_the_slot_and_the_next_scenario_takes_one_again(demo):
    client, fakes, copy = demo
    async with browser() as maya:
        await start_scenario(maya)
        first = (await slot_of(maya))["queue"]
        assert (await maya.post("/api/reset")).status_code == 200
        assert await slot_of(maya) is None
        assert (await maya.get("/api/overview")).json()["slot"] is None
        # The Workflow clears the slot in its own time: Workers stopped, GitHub items closed, then done.
        handle = client.get_workflow_handle(config.slot_workflow_id(1, copy))
        await handle.result()
        assert fakes.hosting[first] == 0 and len(fakes.cleanups) == 1
        assert (await start_scenario(maya)).status_code == 200
        assert await slot_of(maya) is not None


async def test_when_every_slot_is_taken_the_next_browser_is_told_so(demo):
    async with browser() as one, browser() as two, browser() as three:
        assert (await start_scenario(one)).status_code == 200
        assert (await start_scenario(two)).status_code == 200
        refused = await start_scenario(three)
        assert refused.status_code == 503 and "All 2 sessions are in use" in refused.json()["detail"]
        assert await slot_of(three) is None


async def test_polling_does_not_extend_a_lease_but_actions_do(demo, monkeypatch):
    client, _fakes, copy = demo
    monkeypatch.setattr(config, "SESSION_IDLE_MINUTES", 4 / 60)
    async with browser() as watcher, browser() as worker:
        await start_scenario(watcher)
        await start_scenario(worker)
        started = time.monotonic()
        while time.monotonic() - started < 7:
            await watcher.get("/api/overview")  # the console's polling
            await watcher.get("/api/config")
            if time.monotonic() - started > 2:  # never as long as the idle window apart
                await worker.post("/api/faults/outage", json={"provider": "anthropic", "on": False})
            await asyncio.sleep(0.5)

        async def watcher_slot_cleared() -> bool:
            return await _status(client, copy, 1) != WorkflowExecutionStatus.RUNNING

        await until(watcher_slot_cleared)
        assert await slot_of(watcher) is None, "the watcher only polled, so its slot expired"
        assert await slot_of(worker) is not None, "the other browser kept acting, so it kept its slot"


async def _status(client, copy: str, n: int):
    return (await client.get_workflow_handle(config.slot_workflow_id(n, copy)).describe()).status


async def test_a_provider_outage_is_one_browsers(demo):
    async with browser() as maya, browser() as sam:
        await start_scenario(maya)
        await start_scenario(sam)
        down = await maya.post("/api/faults/outage", json={"provider": "anthropic", "on": True})
        assert down.status_code == 200 and down.json()["until"] > time.time()
        assert list((await maya.get("/api/overview")).json()["outages"]) == ["anthropic"]
        assert (await sam.get("/api/overview")).json()["outages"] == {}
        await maya.post("/api/faults/outage", json={"provider": "anthropic", "on": False})
        assert (await maya.get("/api/overview")).json()["outages"] == {}


async def test_breaking_the_canary_answers_at_once_and_only_affects_that_slots_canary(demo):
    client, _fakes, copy = demo
    async with browser() as maya, browser() as sam:
        await start_scenario(maya)
        await start_scenario(sam)
        mine = config.slot_queue(1, copy)
        assert (await maya.post("/api/environments/break-canary", json={})).status_code == 409  # no canary yet

        start = WithStartWorkflowOperation(Environments.run, None, id=f"{mine}.gh.environments", task_queue=mine,
                                           id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
        await client.execute_update_with_start_workflow(
            Environments.deploy, DeployRequest(action="canary", run_id="issue-12", sha="abc1234"),
            start_workflow_operation=start)
        state.env_cache.clear()

        broken = await maya.post("/api/environments/break-canary", json={"seconds": 60})
        assert broken.status_code == 200 and broken.json()["canary_sha"] == "abc1234"
        overview = (await maya.get("/api/overview")).json()
        assert overview["canary_fault"]["sha"] == "abc1234" and overview["canary_fault"]["until"] > time.time()
        assert (await sam.get("/api/overview")).json()["canary_fault"] is None
        # The canary's assets now fail, as the pipeline's checks and the audience's browsers see.
        failing = await maya.get(f"/apps/{mine}/canary/src/app.js")
        assert failing.status_code == 503 and "Injected fault" in failing.text
        assert (await sam.post("/api/environments/break-canary", json={})).status_code == 409


async def test_the_storefront_only_serves_this_copys_slots(demo):
    async with browser() as anyone:
        assert (await anyone.get("/apps/other-copy--s01/prod/")).status_code == 404
        assert (await anyone.get("/apps/agentic-sdlc/prod/")).status_code == 404  # not a slot queue


# -- Agent events --------------------------------------------------------------------------


async def _fake_model_stream(_messages, _info):
    """A model that streams a sentence, then answers through Pydantic AI's output tool."""
    from pydantic_ai.models.function import DeltaToolCall

    for chunk in ("Reading the issue. ", "Nothing is ", "left open."):
        yield chunk
    yield {0: DeltaToolCall(name="final_result", json_args='{"questions": []}')}


def _real_agents_on_a_fake_model(monkeypatch):
    """The real agent Workflows and Pydantic AI agents, with one fake model in the registry
    instead of the provider models (which need API keys). The registry is built when
    agents.runtime is first imported, so nothing may import it before this."""
    import sys

    from pydantic_ai.models.function import FunctionModel

    from agentic_sdlc.agents import kit_ext, registry

    assert "agentic_sdlc.agents.runtime" not in sys.modules, "agents.runtime was imported before its registry was faked"
    monkeypatch.setattr(registry, "build_registry",
                        lambda: {"google:fake": kit_ext.DemoModel(FunctionModel(stream_function=_fake_model_stream))})
    from agentic_sdlc.agents import runtime, workflows

    return runtime, workflows


async def test_an_agents_events_stream_to_the_console_from_its_workflow(demo, monkeypatch):
    """A real agent turn, sent the way the pipeline sends it (the agent-turn Activity): its model
    output reaches the run's SSE stream, read from the agent Workflow's own stream, the Activity
    gets the agent's answer from that stream, and Redis holds none of it."""
    import uvicorn
    from pydantic_ai.durable_exec.temporal import AgentPlugin
    from temporal_agent_kit.agent_protocol import AgentConfig

    from agentic_sdlc.activities.agent_turn import AgentTurn, AgentTurnActivities, AgentTurnResult
    from agentic_sdlc.agents.tools import ALL_TOOLS
    from agentic_sdlc.models import AgentInit, AgentRef, ModelChoice, PlanRequest
    from agentic_sdlc.temporal_client import connect

    runtime, workflows = _real_agents_on_a_fake_model(monkeypatch)
    client, _fakes, copy = demo
    monkeypatch.setenv("TEMPORAL_ADDRESS", client.service_client.config.target_host)
    agent_client = await connect(tools=ALL_TOOLS)
    agent_queue = f"{copy}-agents"

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(server.serve())
    events: list[dict] = []

    async def read_stream(maya: httpx.AsyncClient) -> None:
        async with maya.stream("GET", "/api/runs/issue-7/stream") as response:
            kind = None
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    kind = line.removeprefix("event: ")
                elif line.startswith("data: ") and kind == "agent":
                    events.append(json.loads(line.removeprefix("data: ")))

    try:
        await until(lambda: _true(server.started))
        async with (
            Worker(agent_client, task_queue=agent_queue, workflows=[workflows.PlannerAgent],
                   activities=AgentTurnActivities(agent_client).activities(),
                   plugins=[AgentPlugin(agent) for agent in runtime.ALL_AGENTS], workflow_runner=workflow_runner()),
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=None) as maya,
        ):
            assert (await start_scenario(maya)).status_code == 200
            mine = config.slot_queue(1, copy)
            agent_id = f"{mine}.gh.issue-7.planner"
            await agent_client.start_workflow(
                "PlannerAgent", args=[AgentConfig(), AgentInit(run_id="issue-7", model=ModelChoice(provider="google", model="fake"))],
                id=agent_id, task_queue=agent_queue)
            run = RunState(run_id="issue-7", kind="issue", version="", title="Free shipping", budget_usd=3.0,
                           started_at=time.time(), steps=[],
                           agents=[AgentRef(role="planner", workflow_id=agent_id, model="google:fake")])
            await client.start_workflow("IssueToProduction", RunRequest(run_id="issue-7", kind="issue", repo="x",
                                                                        title="Free shipping", body=""),
                                        id=f"{mine}.gh.issue-7", task_queue=mine, memo={"run": run.model_dump(mode="json")})
            reading = asyncio.create_task(read_stream(maya))

            turn = await agent_client.execute_activity(
                "run_agent_turn",
                AgentTurn(workflow_id=agent_id, handler="clarify", update_id=f"{agent_id}:clarify:1",
                          payload=PlanRequest(issue_title="Free shipping", issue_body="Over $75").model_dump(mode="json")),
                id=f"{agent_id}-turn-1", task_queue=agent_queue, result_type=AgentTurnResult,
                schedule_to_close_timeout=timedelta(seconds=60))
            assert turn.output["clarification"] == {"questions": []}
            assert turn.stats.model_calls == 1

            await until(lambda: _true(any(e["type"] == "turn_end" for e in events)))
            reading.cancel()
            types = [e["type"] for e in events]
            assert {"model_interaction_started", "reply_delta", "model_interaction_ended", "message_handler_end"} <= set(types)
            assert "".join(e["text"] for e in events if e["type"] == "reply_delta") == "Reading the issue. Nothing is left open."
            assert {e["agent_workflow_id"] for e in events} == {agent_id}
            assert [key async for key in state.redis.scan_iter("agent-events:*")] == []
            await agent_client.get_workflow_handle(agent_id).signal("close")
    finally:
        server.should_exit = True
        await serving
