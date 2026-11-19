"""One scripted model in place of the provider models, which need API keys.

The agents' model registry is built when ``agentic_sdlc.agents.runtime`` is first imported, so
``install()`` has to run before anything imports it: conftest.py calls it for the test process, and
fake_worker.py for a worker process of its own. The real agents, ``DemoModel`` wrapper and
durability capability run unchanged on top of it.
"""

from __future__ import annotations

import asyncio
import os

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

KEY = "google:fake"
ANSWER_CHUNKS = ("Reading the issue. ", "Nothing is ", "left open.")


class Script:
    """What the next model calls do. ``behavior`` is one of:

    - ``answer``: stream ``ANSWER_CHUNKS``, then answer through Pydantic AI's output tool with an
      empty ``Clarification`` (the planner's ``clarify`` turn);
    - ``reject``: fail every call as a provider's 400 would;
    - ``unavailable_once``: fail the next call as a provider's 503 would, then answer;
    - ``hang``: never answer, as a call still in flight when its worker dies.
    """

    def __init__(self) -> None:
        self.behavior = os.environ.get("FAKE_MODEL_BEHAVIOR", "answer")
        self.calls = 0

    async def stream(self, _messages, _info):
        self.calls += 1
        if self.behavior == "reject":
            raise ModelHTTPError(400, "fake", {"error": "bad request"})
        if self.behavior == "unavailable_once":
            self.behavior = "answer"
            raise ModelHTTPError(503, "fake", {"error": "unavailable"})
        if self.behavior == "hang":
            await asyncio.sleep(3600)
        for chunk in ANSWER_CHUNKS:
            yield chunk
        yield {0: DeltaToolCall(name="final_result", json_args='{"questions": []}')}


SCRIPT = Script()


def install() -> None:
    import sys

    from agentic_sdlc.agents import kit_ext, registry

    if "agentic_sdlc.agents.runtime" in sys.modules:
        raise RuntimeError("agents.runtime was imported before its model registry was faked")
    registry.build_registry = lambda: {KEY: kit_ext.DemoModel(FunctionModel(stream_function=SCRIPT.stream))}
