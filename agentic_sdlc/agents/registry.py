"""The models agents can use, per provider.

``allowlist`` is key-free so the web server can show the pickers. ``build_registry`` is
worker-only: constructing a model builds a provider client, which reads the API key.
Provider SDK retries are off everywhere, so every retry is a visible Temporal Activity
attempt instead of a hidden loop inside one.
"""

from __future__ import annotations

import os

from agentic_sdlc.models import ModelChoice, Provider

# Override per environment with AGENT_MODELS_<PROVIDER>: a comma-separated list whose first
# entry is that provider's default.
DEFAULT_ALLOWLIST: dict[Provider, tuple[str, ...]] = {
    "anthropic": ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"),
    "openai": ("gpt-5.5", "gpt-5.4-mini"),
    "google": ("gemini-3.5-flash", "gemini-3.1-pro-preview"),
}

_KEY_ENV: dict[Provider, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def allowlist(*, require_keys: bool = True) -> dict[Provider, tuple[str, ...]]:
    """The allowed models per provider. The worker passes ``require_keys`` and gets only
    providers it holds a key for. The web server holds no LLM keys, so it reads the
    ENABLED_PROVIDERS list the deployment sets instead."""
    enabled = {p.strip() for p in os.environ.get("ENABLED_PROVIDERS", "anthropic,openai,google").split(",")}
    result: dict[Provider, tuple[str, ...]] = {}
    for provider, defaults in DEFAULT_ALLOWLIST.items():
        if provider not in enabled:
            continue
        if require_keys and not any(os.environ.get(name) for name in _KEY_ENV[provider]):
            continue
        override = os.environ.get(f"AGENT_MODELS_{provider.upper()}", "")
        result[provider] = tuple(n.strip() for n in override.split(",") if n.strip()) or defaults
    return result


def is_allowed(choice: ModelChoice, *, require_keys: bool = True) -> bool:
    return choice.model in allowlist(require_keys=require_keys).get(choice.provider, ())


def _build(provider: Provider, name: str):
    if provider == "anthropic":
        from anthropic import AsyncAnthropic
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicModel(name, provider=AnthropicProvider(anthropic_client=AsyncAnthropic(max_retries=0)))
    if provider == "openai":
        from openai import AsyncOpenAI
        from pydantic_ai.models.openai import OpenAIResponsesModel
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIResponsesModel(name, provider=OpenAIProvider(openai_client=AsyncOpenAI(max_retries=0)))
    if provider == "google":
        from google import genai
        from google.genai.types import HttpOptions, HttpRetryOptions
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        client = genai.Client(
            api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"),
            http_options=HttpOptions(retry_options=HttpRetryOptions(attempts=1)),
        )
        return GoogleModel(name, provider=GoogleProvider(client=client))
    raise ValueError(f"unknown provider {provider!r}")


def build_registry():
    """Every allowed model this worker has a key for, keyed ``provider:model``."""
    from agentic_sdlc.agents.harness_ext import DemoModel

    registry = {
        f"{provider}:{name}": DemoModel(_build(provider, name))
        for provider, names in allowlist().items()
        for name in names
    }
    if not registry:
        raise RuntimeError("No LLM provider keys are set: set ANTHROPIC_API_KEY, OPENAI_API_KEY or GEMINI_API_KEY.")
    return registry


def settings_for(choice: ModelChoice):
    """Per-provider settings. Claude 5.x needs adaptive thinking with an effort level, which
    the generic ``thinking`` setting doesn't send in the Pydantic AI version we pin."""
    from pydantic_ai.settings import ModelSettings

    if choice.provider == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModelSettings

        return AnthropicModelSettings(
            max_tokens=16_000,
            anthropic_thinking={"type": "adaptive", "display": "summarized"},
            anthropic_effort="medium",
            anthropic_cache_instructions=True,
            anthropic_cache_tool_definitions=True,
        )
    if choice.provider == "openai":
        from pydantic_ai.models.openai import OpenAIResponsesModelSettings

        return OpenAIResponsesModelSettings(max_tokens=16_000, thinking="medium", openai_reasoning_summary="auto")
    return ModelSettings(max_tokens=16_000, thinking="medium")
