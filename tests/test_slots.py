"""Session slot tests: the lease, its expiry, and crashing and restoring a slot's Workers.

The Activities that host Workers, clear a slot and clean up GitHub are replaced by fakes with the
real names, so what's under test is the Slot Workflow: who may hold a slot, when it lets go, and
what it asks the rest of the system to do. Idle and auto-restore times are shortened through config.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter

import pytest
from temporalio import activity
from temporalio.client import Client, WithStartWorkflowOperation, WorkflowExecutionStatus, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentic_sdlc import config
from agentic_sdlc.activities.github import CleanupInput
from agentic_sdlc.sandbox import workflow_runner
from agentic_sdlc.slots import HostSlotInput, ResetSlotInput, Slot, SlotInput, SlotState

REPO = "temporal-sa/test"


class World:
    """What the fake Activities saw."""

    def __init__(self) -> None:
        self.hosting: Counter[str] = Counter()  # host queue -> Workers currently hosted on it
        self.resets: list[str] = []
        self.cleanups: list[CleanupInput] = []
        self.reset_delay = 0.0


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter) as env:
        yield env


@pytest.fixture
def copy() -> str:
    return f"t{uuid.uuid4().hex[:6]}"


@pytest.fixture
async def world(env, copy, monkeypatch):
    world = World()

    @activity.defn(name="host_slot")
    async def host_slot(input: HostSlotInput) -> None:
        queue = activity.info().task_queue
        world.hosting[queue] += 1
        try:
            while True:
                activity.heartbeat()
                await asyncio.sleep(0.2)
        finally:
            world.hosting[queue] -= 1

    @activity.defn(name="reset_slot")
    async def reset_slot(input: ResetSlotInput) -> int:
        await asyncio.sleep(world.reset_delay)
        world.resets.append(input.queue)
        return 0

    @activity.defn(name="cleanup_runs")
    async def cleanup_runs(input: CleanupInput) -> int:
        world.cleanups.append(input)
        return 0

    workers = [
        Worker(env.client, task_queue=config.host_queue(copy), workflows=[Slot], activities=[reset_slot],
               workflow_runner=workflow_runner()),
        Worker(env.client, task_queue=config.version_host_queue("v1", copy), activities=[host_slot]),
        Worker(env.client, task_queue=config.version_host_queue("v2", copy), activities=[host_slot]),
        Worker(env.client, task_queue=copy, activities=[cleanup_runs]),
    ]
    async with workers[0], workers[1], workers[2], workers[3]:
        yield world


async def claim(client: Client, copy: str, sid: str, n: int = 1) -> SlotState:
    queue = config.slot_queue(n, copy)
    start = WithStartWorkflowOperation(
        Slot.run, SlotInput(queue=queue, repo=REPO), id=config.slot_workflow_id(n, copy),
        task_queue=config.host_queue(copy), id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
    return await client.execute_update_with_start_workflow(Slot.claim, sid, start_workflow_operation=start)


def slot(client: Client, copy: str, n: int = 1):
    return client.get_workflow_handle(config.slot_workflow_id(n, copy))


async def until(condition, timeout: float = 15.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.1)


async def status(handle) -> WorkflowExecutionStatus:
    return (await handle.describe()).status


async def test_a_second_session_cannot_take_a_held_slot(env, copy, world):
    state = await claim(env.client, copy, "alice")
    assert (state.phase, state.owner) == ("owned", "alice")
    with pytest.raises(WorkflowUpdateFailedError) as err:
        await claim(env.client, copy, "bob")
    assert "in use" in str(err.value.cause)
    # Claiming again is how the owner keeps their slot, and doesn't start more Workers.
    assert (await claim(env.client, copy, "alice")).owner == "alice"
    await until(lambda: world.hosting[config.version_host_queue("v1", copy)] == 1)
    assert world.hosting[config.version_host_queue("v1", copy)] == 1


async def test_a_claim_starts_a_worker_for_each_pipeline_version(env, copy, world):
    await claim(env.client, copy, "alice")
    await until(lambda: world.hosting[config.version_host_queue("v1", copy)] == 1
                and world.hosting[config.version_host_queue("v2", copy)] == 1)


async def test_only_the_owner_can_act_on_a_slot(env, copy, world):
    await claim(env.client, copy, "alice")
    for update in (Slot.touch, Slot.crash, Slot.restore, Slot.release):
        with pytest.raises(WorkflowUpdateFailedError):
            await slot(env.client, copy).execute_update(update, "mallory")


async def test_an_idle_slot_is_reset_and_freed(env, copy, world, monkeypatch):
    monkeypatch.setattr(config, "SESSION_IDLE_MINUTES", 0.05)  # 3 seconds
    await claim(env.client, copy, "alice")
    await slot(env.client, copy).result()
    assert world.resets == [config.slot_queue(1, copy)]
    [cleanup] = world.cleanups
    assert cleanup.owner_label == f"agentic-sdlc:{config.slot_queue(1, copy)}"
    assert cleanup.branch_prefix == f"runs/{config.slot_queue(1, copy)}/"
    assert cleanup.created_before is not None
    assert sum(world.hosting.values()) == 0
    # The slot is free for the next person, with nothing left over from the last.
    state = await claim(env.client, copy, "bob")
    assert (state.owner, state.crashed) == ("bob", False)


async def test_an_action_postpones_expiry(env, copy, world, monkeypatch):
    monkeypatch.setattr(config, "SESSION_IDLE_MINUTES", 4 / 60)
    await claim(env.client, copy, "alice")
    await asyncio.sleep(2.5)
    await slot(env.client, copy).execute_update(Slot.touch, "alice")
    await asyncio.sleep(2.5)  # 5 seconds after the claim, 2.5 after the touch
    assert await status(slot(env.client, copy)) == WorkflowExecutionStatus.RUNNING
    await slot(env.client, copy).result()  # and it does expire, 4 seconds after the touch
    assert world.resets


async def test_github_activity_counts_as_an_action(env, copy, world, monkeypatch):
    monkeypatch.setattr(config, "SESSION_IDLE_MINUTES", 4 / 60)
    await claim(env.client, copy, "alice")
    await asyncio.sleep(2.5)
    await slot(env.client, copy).signal(Slot.github_activity, 0.0)
    await asyncio.sleep(2.5)
    assert await status(slot(env.client, copy)) == WorkflowExecutionStatus.RUNNING


async def test_release_resets_and_frees_the_slot(env, copy, world):
    await claim(env.client, copy, "alice")
    state = await slot(env.client, copy).execute_update(Slot.release, "alice")
    assert state.phase == "resetting", "release returns at once; the clearing happens after"
    await slot(env.client, copy).result()
    assert world.resets == [config.slot_queue(1, copy)]
    assert len(world.cleanups) == 1
    assert sum(world.hosting.values()) == 0


async def test_crash_stops_the_workers_and_restore_brings_them_back(env, copy, world):
    await claim(env.client, copy, "alice")
    hosts = [config.version_host_queue(v, copy) for v in ("v1", "v2")]
    await until(lambda: all(world.hosting[h] == 1 for h in hosts))

    state = await slot(env.client, copy).execute_update(Slot.crash, "alice")
    assert state.crashed and state.auto_restore_at
    assert all(world.hosting[h] == 0 for h in hosts), "crash returns once the Workers have stopped"
    assert state.phase == "owned", "a crashed slot is still the owner's"

    state = await slot(env.client, copy).execute_update(Slot.restore, "alice")
    assert not state.crashed
    await until(lambda: all(world.hosting[h] == 1 for h in hosts))


async def test_a_forgotten_crash_restores_itself(env, copy, world, monkeypatch):
    monkeypatch.setattr(config, "CRASH_AUTO_RESTORE_SECONDS", 2)
    await claim(env.client, copy, "alice")
    await slot(env.client, copy).execute_update(Slot.crash, "alice")
    await until(lambda: world.hosting[config.version_host_queue("v1", copy)] == 1, timeout=10)
    state = SlotState.model_validate(await (await slot(env.client, copy).describe()).memo_value("slot"))
    assert not state.crashed


async def test_a_slot_being_cleared_cannot_be_claimed(env, copy, world):
    world.reset_delay = 3
    await claim(env.client, copy, "alice")
    releasing = asyncio.create_task(slot(env.client, copy).execute_update(Slot.release, "alice"))
    await asyncio.sleep(1)
    with pytest.raises(WorkflowUpdateFailedError) as err:
        await claim(env.client, copy, "bob")
    assert "cleared" in str(err.value.cause)
    await releasing
    await slot(env.client, copy).result()
    assert (await claim(env.client, copy, "bob")).owner == "bob"
