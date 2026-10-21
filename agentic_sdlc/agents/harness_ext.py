"""Small gap-fillers for temporal-agent-harness (temporal-sa fork @ f12f194) and Pydantic AI 2.53.

Every workaround for a library gap lives here, each with the reason, so an upgrade has one
file to re-check. The patterns come from temporal-demo-advanced-ai-agent's harness_ext.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from pydantic_ai import FunctionToolset, Tool
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import AgentStreamEvent, ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext
from temporalio import activity
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.ai_sdks.pydantic_ai_harness import (
    HarnessDeps,
    PydanticAIStreamObserver,
    _resolve_stream_context,  # reads the turn context off deps, typed or dict
)

from agentic_sdlc.models import Workspace

HEARTBEAT_EVERY_SECONDS = 2.0
_RETRYABLE_4XX = {408, 409, 429}


class DemoDeps(HarnessDeps):
    """Run deps: the harness runner (from HarnessDeps) plus the branch this agent works on,
    which the workspace tools receive as an injected parameter the model never sees."""

    workspace: Workspace | None = None


# -- Usage: keep OpenAI and Gemini token counts ---------------------------------------------


def _install_usage_extract_fix() -> None:
    """``RequestUsage.extract`` builds ``cls(**usage.__dict__)`` and swallows any error, so the
    ``output_reasoning_tokens`` field newer genai-prices returns makes every OpenAI and Gemini
    usage come back as zero (pydantic_ai/usage.py). Keep the known fields instead."""
    from genai_prices.data_snapshot import get_snapshot
    from pydantic_ai.usage import RequestUsage

    if getattr(RequestUsage.extract, "__demo_fix__", False):
        return
    known = {f.name for f in dataclasses.fields(RequestUsage)}
    original = RequestUsage.extract.__func__

    def extract(cls, data, *, provider, provider_url, provider_fallback, api_flavor="default", details=None):
        details = dict(details or {})
        for provider_id, api_url in [(None, provider_url), (provider, None), (provider_fallback, None)]:
            try:
                found = get_snapshot().find_provider(None, provider_id, api_url)
                _ref, usage = found.extract_usage(data, api_flavor=api_flavor)
            except Exception:  # noqa: BLE001 - same fallthrough as upstream
                continue
            values = {k: v for k, v in usage.__dict__.items() if v is not None}
            if reasoning := values.pop("output_reasoning_tokens", None):
                details.setdefault("reasoning_tokens", reasoning)
            return cls(**{k: v for k, v in values.items() if k in known}, details=details)
        return original(cls, data, provider=provider, provider_url=provider_url,
                        provider_fallback=provider_fallback, api_flavor=api_flavor, details=details)

    extract.__demo_fix__ = True  # type: ignore[attr-defined]
    RequestUsage.extract = classmethod(extract)  # type: ignore[method-assign]


_install_usage_extract_fix()


# -- The model Activity: heartbeats, honest retries, injected rate limits --------------------


class DemoModel(WrapperModel):
    """Wraps each provider model so its Temporal Activity heartbeats for the whole request.

    Pydantic AI's model Activity has no heartbeat, so a worker that dies mid-call would only be
    noticed at the start-to-close timeout. A background heartbeat lets a 10 s heartbeat timeout
    catch it and retry on another worker. Provider 4xx errors other than 408/409/429 are
    non-retryable: a bad request won't succeed on attempt two.
    """

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        async with heartbeating():
            try:
                return await super().request(messages, model_settings, model_request_parameters)
            except ModelHTTPError as exc:
                raise _classify(exc) from exc

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        await _maybe_inject_rate_limit()
        async with heartbeating():
            try:
                async with self.wrapped.request_stream(
                    messages, model_settings, model_request_parameters, run_context
                ) as response:
                    yield response
            except ModelHTTPError as exc:
                raise _classify(exc) from exc


async def _maybe_inject_rate_limit() -> None:
    """Demo fault: the presenter armed a rate limit for this agent (agentic_sdlc.faults).
    The attempt fails as a provider 429 would, and the Activity's Retry Policy retries it."""
    from agentic_sdlc import faults

    if activity.in_activity() and await faults.take(activity.info().workflow_id or "", "model_rate_limit"):
        raise ApplicationError(
            "Injected: model provider returned 429 Too Many Requests", type="InjectedRateLimit"
        )


def _classify(exc: ModelHTTPError) -> Exception:
    if 400 <= exc.status_code < 500 and exc.status_code not in _RETRYABLE_4XX:
        return ApplicationError(
            f"The model provider rejected the request ({exc.status_code}).",
            {"status_code": exc.status_code, "model": exc.model_name},
            type="ProviderRejected",
            non_retryable=True,
        )
    return exc


@asynccontextmanager
async def heartbeating() -> AsyncIterator[None]:
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


# -- Streaming handler with working usage ----------------------------------------------------


async def event_stream_handler(ctx: RunContext[Any], stream: AsyncIterable[AgentStreamEvent]) -> None:
    """The harness's handler, except it reads ``usage`` as the property it is in Pydantic AI
    2.53. The harness calls ``stream.usage()``, which raises and is swallowed, so every
    ``model_interaction_ended`` would go out without token counts."""
    context = _resolve_stream_context(ctx)
    if context is None or not isinstance(stream, StreamedResponse):
        async for _ in stream:
            pass
        return
    async with PydanticAIStreamObserver(context, model=getattr(stream, "model_name", None)) as observer:
        async for event in stream:
            await observer.on_event(event)
        usage = stream.usage
        observer.set_usage(usage() if callable(usage) else usage)


# -- Workspace tools: inject the run's branch per call -------------------------------------


def workspace_toolset(
    tools: Sequence[Callable[..., Awaitable[Any]]], *, id: str
) -> tuple[FunctionToolset[Any], dict[str, dict[str, Any]]]:
    """Like the harness's ``build_harness_toolset``, but the ``workspace`` injection comes from
    each run's deps rather than being fixed when the module loads. One module-level agent
    then serves every branch. The returned config runs the tools in the Workflow, where the
    harness applies the approval policy and dispatches activity-backed tools.
    """
    adapted = [_workspace_tool(tool) for tool in tools]
    return FunctionToolset(adapted, id=id), {id: {tool.name: False for tool in adapted}}


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

