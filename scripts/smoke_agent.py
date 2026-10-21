"""Smoke test: run one real CoderAgent turn on a scratch branch.

    uv run python scripts/smoke_agent.py anthropic:claude-sonnet-5-5
"""

import asyncio
import subprocess
import sys
import uuid

from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.worker import Worker

from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig

from agentic_sdlc import config
from agentic_sdlc.agents.runtime import ALL_AGENTS
from agentic_sdlc.agents.tools import ALL_TOOLS
from agentic_sdlc.agents.workflows import CoderAgent
from agentic_sdlc.models import AgentInit, CodeRequest, ModelChoice, Subtask, Workspace
from agentic_sdlc.temporal_client import EVENT_STREAM, connect


async def main(model_key: str) -> None:
    provider, _, model = model_key.partition(":")
    branch = f"runs/smoke/{uuid.uuid4().hex[:6]}/task-1"
    subprocess.run(["gh", "api", "-X", "POST", f"repos/{config.GITHUB_SANDBOX_REPO}/git/refs",
                    "-f", f"ref=refs/heads/{branch}", "-f",
                    "sha=" + subprocess.check_output(["gh", "api", f"repos/{config.GITHUB_SANDBOX_REPO}/git/ref/tags/baseline", "--jq", ".object.sha"], text=True).strip()],
                   check=True, capture_output=True)
    client = await connect(tools=ALL_TOOLS)
    tq = f"smoke-{uuid.uuid4().hex[:6]}"
    async with Worker(client, task_queue=tq, workflows=[CoderAgent], plugins=[AgentPlugin(a) for a in ALL_AGENTS]):
        wf_id = f"smoke-coder-{uuid.uuid4().hex[:6]}"
        init = AgentInit(run_id="smoke", model=ModelChoice(provider=provider, model=model),
                         workspace=Workspace(repo=config.GITHUB_SANDBOX_REPO, branch=branch))
        await client.start_workflow("CoderAgent", args=[AgentConfig(), init], id=wf_id, task_queue=tq)
        agent = AgentClient(client, wf_id, event_stream=EVENT_STREAM)
        req = CodeRequest(issue_title="Add a free-shipping threshold",
                          plan_summary="Orders of $75 or more ship free; others pay $6.",
                          subtask=Subtask(id="shipping-logic", title="Shipping cost in cart logic",
                                          description="Add shipping(cart) to src/cart.js returning 0 when subtotal >= 75 else 6, include shipping in totals(), and add tests.",
                                          files=["src/cart.js", "tests/cart.test.js"]))
        reply = await agent.submit_message("code", req.model_dump())
        print("accepted", reply.message_id)
        result = await agent.await_message(reply.message_id)
        print("RESULT", result.output or result.error)
        await client.get_workflow_handle(wf_id).signal("close")
    print("branch:", f"https://github.com/{config.GITHUB_SANDBOX_REPO}/commits/{branch}")


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "anthropic:claude-sonnet-5-5"))
