"""Session slots: one browser's private corner of a shared demo.

A browser that starts a scenario leases a slot, a task queue from a small reused pool
(``<copy>--s03``). The slot's runs, environments, faults and Workers are all scoped to that queue,
so what one person does in the catalog never shows up on another person's screen.

Each slot is owned by one ``Slot`` Workflow, which holds the lease, hosts the slot's Workers and
keeps the idle clock:

- A claim is an Update whose validator rejects it when someone else holds the slot. Updates to one
  Workflow run one at a time, so two browsers can't both win.
- Only demo actions extend the lease (``touch``, and the webhook's ``github_activity``). The
  console's polling never does.
- When the lease idles out, or its owner resets, the Workflow stops the Workers, clears the slot's
  runs and faults, closes its GitHub items, and completes. A free slot has no running Workflow.
- "Crashing" the slot cancels the Activities that host its Workers. The Workers stop at once and
  whatever they were running is retried by the next Worker, which is the point of the beat.

The Workflow and ``reset_slot`` run on an unversioned host queue. ``host_slot`` runs on one queue
per pipeline version, because a process serves one version.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

# The Activities below talk to Redis and run Workers; the Workflow sandbox shouldn't load those.
with workflow.unsafe.imports_passed_through():
    from redis.asyncio import Redis
    from temporalio.client import Client
    from temporalio.service import RPCError
    from temporalio.worker import Worker

    from agentic_sdlc import config
    from agentic_sdlc.activities.github import CleanupInput
    from agentic_sdlc.versions import KNOWN_VERSIONS

log = logging.getLogger(__name__)

# A fresh Workflow nobody claimed (the claim was rejected) doesn't linger.
UNCLAIMED_TIMEOUT = timedelta(minutes=5)
HEARTBEAT_TIMEOUT = timedelta(seconds=5)
# One process hosts many slots' Workers, so each polls lightly.
SLOT_WORKER_LIMITS = dict(max_concurrent_workflow_task_polls=2, max_concurrent_activity_task_polls=2,
                          max_cached_workflows=20)


class SlotState(BaseModel):
    phase: Literal["free", "owned", "resetting"] = "free"
    owner: str | None = None  # the browser session holding the lease
    claimed_at: float = 0.0  # items and runs from before this belong to a previous lease
    last_action_at: float = 0.0
    crashed: bool = False
    crashed_at: float | None = None
    auto_restore_at: float | None = None


class SlotInput(BaseModel):
    queue: str  # the slot's task queue
    repo: str  # the GitHub repo its scenarios live in
    state: SlotState | None = None  # carried across continue-as-new


class HostSlotInput(BaseModel):
    queue: str


class ResetSlotInput(BaseModel):
    queue: str


@workflow.defn
class Slot:
    @workflow.init
    def __init__(self, input: SlotInput) -> None:
        self._input = input
        self._s = input.state or SlotState()
        self._hosts: list[workflow.ActivityHandle] = []
        self._hosting_lock = asyncio.Lock()
        self._changed = False
        self._finished = False

    @workflow.run
    async def run(self, input: SlotInput) -> None:
        self._publish()
        while True:
            if self._finished:
                await workflow.wait_condition(workflow.all_handlers_finished)
                if self._finished:  # a claim that arrived while the Workflow was winding down un-finishes it
                    return
                continue
            await self._reconcile_hosting()
            self._changed = False
            try:
                await workflow.wait_condition(lambda: self._changed or self._finished, timeout=self._until_next_deadline())
            except TimeoutError:
                pass
            now = workflow.now().timestamp()
            s = self._s
            if s.phase == "owned":
                if s.crashed and s.auto_restore_at and now >= s.auto_restore_at:
                    self._set_crashed(False)
                if now >= s.last_action_at + config.SESSION_IDLE_MINUTES * 60:
                    self._begin_reset()
            elif s.phase == "free" and not self._changed and not s.claimed_at:
                self._finished = True  # nobody claimed it
            if self._s.phase == "resetting":
                await self._finish_reset()
            if workflow.info().is_continue_as_new_suggested() and not self._finished and s.phase == "owned":
                await self._stop_hosting()
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(SlotInput(queue=input.queue, repo=input.repo, state=self._s))

    # -- Lease -------------------------------------------------------------------------------

    @workflow.update
    async def claim(self, sid: str) -> SlotState:
        now = workflow.now().timestamp()
        if self._s.phase == "free":
            self._s = SlotState(phase="owned", owner=sid, claimed_at=now, last_action_at=now)
            self._finished = False
        else:
            self._s.last_action_at = now  # the owner claiming again is an action
        await self._reconcile_hosting()
        self._poke()
        return self._s

    @claim.validator
    def _validate_claim(self, sid: str) -> None:
        if self._s.phase == "resetting":
            raise ApplicationError("This slot is being cleared.", type="SlotBusy")
        if self._s.phase == "owned" and self._s.owner != sid:
            raise ApplicationError("This slot is in use.", type="SlotBusy")

    @workflow.update
    def touch(self, sid: str) -> SlotState:
        self._s.last_action_at = workflow.now().timestamp()
        self._poke()
        return self._s

    @touch.validator
    def _validate_touch(self, sid: str) -> None:
        self._require_owner(sid)

    @workflow.signal
    def github_activity(self, at: float) -> None:
        """GitHub activity on the slot's issues and PRs is an action too."""
        if self._s.phase == "owned":
            self._s.last_action_at = workflow.now().timestamp()
            self._poke()

    @workflow.update
    def release(self, sid: str) -> SlotState:
        """Give the slot back. This returns at once with the slot marked as being cleared, which
        is all the owner needs: it no longer holds the slot, and nobody can claim it until the
        Workers are stopped and the runs and GitHub items are cleaned up."""
        self._begin_reset()
        return self._s

    @release.validator
    def _validate_release(self, sid: str) -> None:
        self._require_owner(sid)

    # -- Crash and restore -------------------------------------------------------------------

    @workflow.update
    async def crash(self, sid: str) -> SlotState:
        """Stop the slot's Workers. Returns once they have stopped."""
        self._s.last_action_at = workflow.now().timestamp()
        self._set_crashed(True)
        await self._reconcile_hosting()
        return self._s

    @crash.validator
    def _validate_crash(self, sid: str) -> None:
        self._require_owner(sid)

    @workflow.update
    async def restore(self, sid: str) -> SlotState:
        self._s.last_action_at = workflow.now().timestamp()
        self._set_crashed(False)
        await self._reconcile_hosting()
        return self._s

    @restore.validator
    def _validate_restore(self, sid: str) -> None:
        self._require_owner(sid)

    # -- Internals ---------------------------------------------------------------------------

    def _require_owner(self, sid: str) -> None:
        if self._s.phase != "owned" or self._s.owner != sid:
            raise ApplicationError("This session doesn't hold the slot.", type="NotOwner")

    def _set_crashed(self, crashed: bool) -> None:
        now = workflow.now().timestamp()
        self._s.crashed = crashed
        self._s.crashed_at = now if crashed else None
        self._s.auto_restore_at = now + config.CRASH_AUTO_RESTORE_SECONDS if crashed else None
        self._poke()

    def _poke(self) -> None:
        self._changed = True
        self._publish()

    def _publish(self) -> None:
        workflow.upsert_memo({"slot": self._s.model_dump(mode="json")})

    def _until_next_deadline(self) -> timedelta | None:
        now = workflow.now().timestamp()
        s = self._s
        if s.phase == "free":
            return UNCLAIMED_TIMEOUT
        if s.phase != "owned":
            return None
        deadlines = [s.last_action_at + config.SESSION_IDLE_MINUTES * 60]
        if s.crashed and s.auto_restore_at:
            deadlines.append(s.auto_restore_at)
        return timedelta(seconds=max(0.0, min(deadlines) - now))

    async def _reconcile_hosting(self) -> None:
        want = self._s.phase == "owned" and not self._s.crashed
        if want and not self._hosts:
            await self._start_hosting()
        elif not want and self._hosts:
            await self._stop_hosting()

    async def _start_hosting(self) -> None:
        async with self._hosting_lock:
            if self._hosts:
                return
            copy, _ = config.parse_slot_queue(self._input.queue)
            self._hosts = [
                workflow.start_activity(
                    "host_slot", HostSlotInput(queue=self._input.queue),
                    task_queue=config.version_host_queue(version, copy),
                    start_to_close_timeout=timedelta(days=1), heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    # If the process hosting the Workers dies, its replacement hosts them again.
                    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=5)),
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
                for version in KNOWN_VERSIONS
            ]

    async def _stop_hosting(self) -> None:
        async with self._hosting_lock:
            hosts, self._hosts = self._hosts, []
            for host in hosts:
                host.cancel()
            await asyncio.gather(*hosts, return_exceptions=True)

    def _begin_reset(self) -> None:
        if self._s.phase == "owned":
            self._s.phase = "resetting"
            self._poke()

    async def _finish_reset(self) -> None:
        """Stop the Workers, stop the slot's runs, close its GitHub items, free the slot."""
        copy, _ = config.parse_slot_queue(self._input.queue)
        await self._stop_hosting()
        cutoff = workflow.now().timestamp()
        await workflow.execute_activity(
            "reset_slot", ResetSlotInput(queue=self._input.queue), task_queue=config.host_queue(copy),
            start_to_close_timeout=timedelta(minutes=5), heartbeat_timeout=timedelta(seconds=60),
        )
        try:
            await workflow.execute_activity(
                "cleanup_runs",
                CleanupInput(repo=self._input.repo, owner_label=f"{config.OWNER_LABEL_PREFIX}{self._input.queue}",
                             branch_prefix=f"runs/{self._input.queue}/", created_before=cutoff),
                task_queue=copy, start_to_close_timeout=timedelta(minutes=5), heartbeat_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityError:
            # GitHub being down mustn't strand the slot. What it leaves behind can't start runs for
            # the next lease, because only items opened after the claim do.
            workflow.logger.warning("Couldn't clean up %s on GitHub", self._input.queue)
        self._s = SlotState(claimed_at=cutoff)  # free; claimed_at keeps the unclaimed timeout from firing
        self._finished = True
        self._publish()
        self._changed = True


class SlotActivities:
    """Hosting a slot's Workers, and clearing a slot's runs and faults."""

    def __init__(self, client: Client, build_worker: Callable[[str], Worker]) -> None:
        self._client = client
        self._build_worker = build_worker
        self._redis = Redis.from_url(config.REDIS_URL)

    def host_activities(self) -> list:
        return [self.host_slot]

    def housekeeping_activities(self) -> list:
        return [self.reset_slot]

    @activity.defn
    async def host_slot(self, input: HostSlotInput) -> None:
        """Poll the slot's task queue from this process until cancelled. Cancelling is the crash:
        the Workers stop at once, and what they were running is retried by the next Worker."""
        key = f"workers:{input.queue}:{config.PIPELINE_VERSION}"
        worker = self._build_worker(input.queue)
        running = asyncio.create_task(worker.run())
        announce = asyncio.create_task(self._announce(key))
        try:
            while True:
                activity.heartbeat()
                done, _ = await asyncio.wait({running}, timeout=1)
                if done:
                    running.result()
                    raise ApplicationError("The slot's Worker stopped by itself.", type="WorkerStopped")
        finally:
            announce.cancel()
            # The console shows the crash now, not when the key's TTL runs out.
            with suppress(Exception):
                await self._redis.delete(key)
            await worker.shutdown()
            await asyncio.gather(running, announce, return_exceptions=True)

    async def _announce(self, key: str) -> None:
        info = json.dumps({"pid": os.getpid(), "version": config.PIPELINE_VERSION,
                           "build_id": config.BUILD_ID or None, "started_at": time.time()})
        while True:
            await self._redis.set(key, info, ex=6)
            await asyncio.sleep(2)

    @activity.defn
    async def reset_slot(self, input: ResetSlotInput) -> int:
        """Stop everything running on the slot (runs, their agents, its environments) and clear
        its faults and settings. Returns how many Workflows it stopped."""
        from agentic_sdlc import faults

        stopped = 0
        query = f"WorkflowId STARTS_WITH '{input.queue}.gh.' AND ExecutionStatus = 'Running'"
        async for execution in self._client.list_workflows(query):
            with suppress(RPCError):
                await self._client.get_workflow_handle(execution.id).terminate("Slot reset")
            stopped += 1
            activity.heartbeat()
        await faults.clear_slot(input.queue)
        await self._redis.delete(f"run-settings:{input.queue}")
        return stopped
