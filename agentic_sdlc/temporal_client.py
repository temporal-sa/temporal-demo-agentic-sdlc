"""One way to connect to Temporal, used by the worker and the web server."""

from __future__ import annotations

import os

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.envconfig import ClientConfig

from temporal_agent_kit.plugin import AgentKitPlugin


async def connect(*, tools=()) -> Client:
    """Connect with the Pydantic AI and Agent Kit plugins (Agent Kit last, as it requires).

    Large-payload offload is off unless PAYLOAD_BUCKET is set: the web and worker run in
    separate pods, so the kit's local-filesystem default couldn't be read back across
    them. Payloads stay small because tool output is capped (agents/tools.py).
    """
    offload = None
    if bucket := os.environ.get("PAYLOAD_BUCKET"):
        from temporal_agent_kit.utils.large_payload import s3_payload_storage

        offload = await s3_payload_storage(bucket=bucket)

    connect_config = ClientConfig.load_client_connect_config(disable_file=True)
    connect_config.setdefault("target_host", "localhost:7233")
    connect_config.setdefault("namespace", "default")
    return await Client.connect(
        **connect_config,
        plugins=[
            PydanticAIPlugin(),
            AgentKitPlugin(tools=tools, large_payload_offload=offload),
        ],
    )
