"""Environments: one long-lived entity Workflow per session that knows what is deployed where.

It holds the staging commit, the stable production commit, the production canary and its
traffic weight. The web server serves the storefront from these commits and reads them with
Describe (the memo), which costs no Actions and still answers while the worker is down.

Versioning: pipeline runs are Pinned and short-lived, but this entity lives as long as the
session. When a new Worker Deployment Version becomes current, it continues-as-new onto it
(Upgrade-on-Continue-as-New), carrying its state, so it never holds an old version alive.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field
from temporalio import workflow

IDLE_TIMEOUT = timedelta(hours=24)
MAX_HISTORY = 20


class Traffic(BaseModel):
    requests: int = 0
    errors: int = 0


class Deployment(BaseModel):
    action: str
    sha: str | None
    run_id: str
    at: float


class EnvState(BaseModel):
    staging_sha: str | None = None
    stable_sha: str | None = None
    canary_sha: str | None = None
    canary_weight: int = 0
    # Presenter fault: until this time the canary serves errors. Time-boxed and cleared on reset.
    canary_fault_until: float = 0.0
    canary_traffic: Traffic = Field(default_factory=Traffic)
    history: list[Deployment] = Field(default_factory=list)


class DeployRequest(BaseModel):
    action: Literal["staging", "canary", "promote", "rollback"]
    run_id: str
    sha: str | None = None
    weight: int = 20


class TrafficBatch(BaseModel):
    """Page loads and browser errors the web server saw for the canary since its last batch."""

    sha: str
    requests: int = 0
    errors: int = 0


@workflow.defn
class Environments:
    @workflow.init
    def __init__(self, state: EnvState | None = None) -> None:
        self._state = state or EnvState()
        self._last_change = workflow.now()

    @workflow.run
    async def run(self, state: EnvState | None = None) -> None:
        self._publish()
        while True:
            info = workflow.info()
            try:
                await workflow.wait_condition(
                    lambda: info.is_continue_as_new_suggested() or info.is_target_worker_deployment_version_changed(),
                    timeout=IDLE_TIMEOUT,
                )
            except TimeoutError:
                if workflow.now() - self._last_change >= IDLE_TIMEOUT:
                    return  # An abandoned session's environments close themselves.
                continue
            await workflow.wait_condition(workflow.all_handlers_finished)
            workflow.continue_as_new(
                self._state, initial_versioning_behavior=workflow.ContinueAsNewVersioningBehavior.AUTO_UPGRADE
            )

    @workflow.update
    def deploy(self, request: DeployRequest) -> EnvState:
        now = workflow.now().timestamp()
        s = self._state
        if request.action == "staging":
            s.staging_sha = request.sha
        elif request.action == "canary":
            s.canary_sha, s.canary_weight, s.canary_traffic = request.sha, request.weight, Traffic()
        elif request.action == "promote":
            # Promote the commit this run canaried. If another run's canary has since taken the
            # slot, leave that canary in place.
            s.stable_sha = request.sha
            if s.canary_sha == request.sha:
                s.canary_sha, s.canary_weight = None, 0
        elif request.action == "rollback" and s.canary_sha == request.sha:
            s.canary_sha, s.canary_weight, s.canary_fault_until = None, 0, 0.0
        s.history = [*s.history, Deployment(action=request.action, sha=request.sha, run_id=request.run_id, at=now)][
            -MAX_HISTORY:
        ]
        self._changed()
        return s

    @workflow.update
    def break_canary(self, seconds: int) -> EnvState:
        self._state.canary_fault_until = workflow.now().timestamp() + seconds
        self._changed()
        return self._state

    @workflow.update
    def reset(self) -> EnvState:
        self._state = EnvState()
        self._changed()
        return self._state

    @workflow.signal
    def record_traffic(self, batch: TrafficBatch) -> None:
        if batch.sha != self._state.canary_sha:
            return  # Late traffic for a canary that has already been promoted or rolled back.
        t = self._state.canary_traffic
        t.requests += batch.requests
        t.errors += batch.errors
        self._changed()

    @workflow.query
    def state(self) -> EnvState:
        return self._state

    def _changed(self) -> None:
        self._last_change = workflow.now()
        self._publish()

    def _publish(self) -> None:
        workflow.upsert_memo({"environments": self._state.model_dump(mode="json")})
