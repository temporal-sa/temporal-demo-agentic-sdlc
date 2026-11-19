"""Small gap-fillers for Temporal Agent Kit 0.12.1 and Pydantic AI 2.71.

Every workaround for a library gap lives here, each with the reason, so an upgrade has one
file to re-check. The patterns come from temporal-demo-advanced-ai-agent's harness_ext.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from pydantic_ai import FunctionToolset, Tool
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext
from temporalio import activity
from temporalio.exceptions import ApplicationError

from temporal_agent_kit.ai_sdks.pydantic_ai import KitDeps

from agentic_sdlc.models import Workspace

HEARTBEAT_EVERY_SECONDS = 2.0
_RETRYABLE_4XX = {408, 409, 429}


class DemoDeps(KitDeps):
    """Run deps: the kit's runner (from KitDeps) plus the branch this agent works on,
    which the workspace tools receive as an injected parameter the model never sees."""

    workspace: Workspace | None = None


# -- The model Activity: honest retries, injected faults ------------------------------------


class DemoModel(WrapperModel):
    """Wraps each provider model for the demo's faults and retry classification.

    Faults armed from the console fail an attempt as the provider would. Provider 4xx errors
    other than 408/409/429 are non-retryable: a bad request won't succeed on attempt two.
    Heartbeats come from Pydantic AI's ``TemporalDurability``, which beats every model Activity
    at half its heartbeat timeout (runtime.MODEL_ACTIVITY sets 10 s).
    """

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        await _maybe_inject_failure(self.wrapped)
        try:
            return await super().request(messages, model_settings, model_request_parameters)
        except ModelHTTPError as exc:
            if (rejected := _rejected(exc)) is None:
                raise
            raise rejected from exc

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        await _maybe_inject_failure(self.wrapped)
        try:
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as response:
                yield response
        except ModelHTTPError as exc:
            if (rejected := _rejected(exc)) is None:
                raise
            raise rejected from exc


PROVIDER_NAMES = {"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google"}


def provider_of(model: Any) -> str:
    """The console's name for a model's provider (Pydantic AI calls Google's ``google-gla`` or ``google-vertex``)."""
    system = str(model.system)
    return "google" if system.startswith("google") else system


async def _maybe_inject_failure(model: Any) -> None:
    """Demo faults armed from the console (agentic_sdlc.faults), for the slot this Activity runs on.
    An attempt fails as the provider would fail it, and the Activity's Retry Policy retries it:
    a provider outage fails every call to that provider until it is switched off, and a rate
    limit fails the next few calls of one agent."""
    from agentic_sdlc import faults

    if not activity.in_activity():
        return
    info = activity.info()
    provider = provider_of(model)
    if provider in faults.PROVIDERS and await faults.outage(info.task_queue, provider):
        raise ApplicationError(
            f"Injected: {PROVIDER_NAMES[provider]} API returned 503 Service Unavailable", type="InjectedProviderOutage"
        )
    if await faults.take(info.task_queue, info.workflow_id or "", "model_rate_limit"):
        raise ApplicationError(
            "Injected: model provider returned 429 Too Many Requests", type="InjectedRateLimit"
        )


def _rejected(exc: ModelHTTPError) -> ApplicationError | None:
    """A non-retryable error for a request the provider won't accept on any attempt, or None to
    re-raise the provider's error as it is (raising it "from" itself would make its cause chain a
    loop, which Temporal can't encode as a failure)."""
    if 400 <= exc.status_code < 500 and exc.status_code not in _RETRYABLE_4XX:
        return ApplicationError(
            f"The model provider rejected the request ({exc.status_code}).",
            {"status_code": exc.status_code, "model": exc.model_name},
            type="ProviderRejected",
            non_retryable=True,
        )
    return None


# -- Tool Activities: heartbeats ------------------------------------------------------------


@asynccontextmanager
async def heartbeating() -> AsyncIterator[None]:
    """Heartbeat while a tool body runs (tools.py). The kit's activity tools don't heartbeat on
    their own, so without this a worker that dies mid-tool is only noticed at the timeout."""
    if not activity.in_activity():
        yield
        return

    async def tick() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(HEARTBEAT_EVERY_SECONDS)

    ticker = asyncio.create_task(tick())
    try:
        yield
    finally:
        ticker.cancel()


# -- Workspace tools: inject the run's branch per call -------------------------------------


def workspace_toolset(tools: Sequence[Callable[..., Awaitable[Any]]], *, id: str) -> FunctionToolset[Any]:
    """Like the kit's ``build_kit_toolset``, but the ``workspace`` injection comes from
    each run's deps rather than being fixed when the module loads. One module-level agent
    then serves every branch. ``temporal: False`` keeps the tools in the Workflow, where the
    kit applies the approval policy and dispatches activity-backed tools.
    """
    return FunctionToolset([_workspace_tool(tool) for tool in tools], id=id, metadata={"temporal": False})


def _workspace_tool(tool_callable: Callable[..., Awaitable[Any]]) -> Tool[Any]:
    probe = Tool(tool_callable, takes_ctx=False)
    name = probe.name

    async def invoke(ctx: RunContext[DemoDeps], **kwargs: Any) -> Any:
        try:
            return await ctx.deps.runner.run_tool(
                ctx.tool_call_id, tool_callable, injections={"workspace": ctx.deps.workspace}, **kwargs
            )
        except Exception as exc:  # noqa: BLE001 - a failed or denied tool is a result the model can react to
            return f"Tool {name!r} failed: {exc}"

    return Tool.from_schema(
        invoke,
        name=name,
        description=probe.description,
        json_schema=probe.function_schema.json_schema,
        takes_ctx=True,
    )

