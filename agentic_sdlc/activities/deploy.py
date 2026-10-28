"""Deploy and check Activities: change what the session's environments serve, then probe them."""

from __future__ import annotations

import asyncio
import os

import httpx
from pydantic import BaseModel
from temporalio import activity
from temporalio.client import Client, WithStartWorkflowOperation
from temporalio.common import WorkflowIDConflictPolicy

from agentic_sdlc.pipeline.environments import DeployRequest, Environments, EnvState

# How the worker reaches the web server that hosts the environments: a Kubernetes Service
# name in the catalog, localhost on a laptop.
WEB_INTERNAL_URL = os.environ.get("WEB_INTERNAL_URL", "http://localhost:8000")
CANARY_PROBES = 8
MAX_CANARY_ERROR_RATE = 0.05


class DeployCall(BaseModel):
    environments_id: str
    request: DeployRequest


class CheckInput(BaseModel):
    slot: str  # the slot's task queue, which names its storefront path
    environments_id: str
    env: str  # "staging" or "canary"


class CheckResult(BaseModel):
    healthy: bool
    probes: int
    failed_probes: int
    browser_requests: int = 0
    browser_errors: int = 0
    detail: str


class DeployActivities:
    def __init__(self, client: Client) -> None:
        self._client = client

    def activities(self) -> list:
        return [self.apply_deployment, self.check_environment]

    @activity.defn
    async def apply_deployment(self, call: DeployCall) -> EnvState:
        """Update-With-Start: the session's Environments entity is created on first deploy."""
        start = WithStartWorkflowOperation(
            Environments.run,
            None,
            id=call.environments_id,
            task_queue=activity.info().task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        return await self._client.execute_update_with_start_workflow(
            Environments.deploy, call.request, start_workflow_operation=start
        )

    @activity.defn
    async def check_environment(self, input: CheckInput) -> CheckResult:
        """Probe the storefront's page, data and scripts, and for a canary also count the
        errors real browsers reported. A failing check is a result, not an error: the pipeline
        decides whether to roll back."""
        base = f"{WEB_INTERNAL_URL}/apps/{input.slot}/{input.env}/"
        paths = ["", "products.json", "src/app.js", "src/cart.js", "styles.css"]
        rounds = CANARY_PROBES // len(paths) + 1 if input.env == "canary" else 1
        failures: list[str] = []
        probes = 0
        async with httpx.AsyncClient(timeout=10) as http:
            for _ in range(rounds):
                for path in paths:
                    probes += 1
                    try:
                        response = await http.get(base + path, headers={"X-Synthetic-Check": "1"})
                        if response.status_code != 200:
                            failures.append(f"{path or 'index.html'} → {response.status_code}")
                    except httpx.HTTPError as exc:
                        failures.append(f"{path or 'index.html'} → {type(exc).__name__}")
                activity.heartbeat()
                await asyncio.sleep(0.2)

        browser = EnvState()
        if input.env == "canary":
            description = await self._client.get_workflow_handle(input.environments_id).describe()
            memo = await description.memo_value("environments", default={})
            browser = EnvState.model_validate(memo)
        t = browser.canary_traffic
        browser_rate = t.errors / t.requests if t.requests else 0.0
        healthy = not failures and browser_rate <= MAX_CANARY_ERROR_RATE
        detail = (
            f"{probes - len(failures)}/{probes} probes passed"
            + (f"; browsers: {t.errors} errors in {t.requests} page loads" if input.env == "canary" else "")
            + (f"; failed: {', '.join(sorted(set(failures))[:4])}" if failures else "")
        )
        return CheckResult(healthy=healthy, probes=probes, failed_probes=len(failures),
                           browser_requests=t.requests, browser_errors=t.errors, detail=detail)
