"""Which pipeline version new runs get: the Worker Deployment's current version.

One worker runs per pipeline version (build IDs v1 and v2), all on the same task queue, and
all of them stay up. Making a version current is one call to Temporal: new runs start on it,
and runs already in flight stay pinned to the version they started on. The console drives this
itself, the way the pizza Worker Versioning demo does, so a presenter can roll out v2 live in
the catalog without a redeploy.

The deployment name defaults to `<task queue>-pipeline`, so a crashable workspace (which gets its own
queue) has its own deployment and its rollout never touches the shared demo.
"""

from __future__ import annotations

from temporalio.api.workflowservice.v1 import (
    DescribeWorkerDeploymentRequest,
    SetWorkerDeploymentCurrentVersionRequest,
)
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from agentic_sdlc import config

KNOWN_VERSIONS = ("v1", "v2")


async def describe(client: Client) -> dict:
    """The current version and every version that has registered, with its drainage status."""
    try:
        response = await client.workflow_service.describe_worker_deployment(
            DescribeWorkerDeploymentRequest(namespace=client.namespace, deployment_name=config.DEPLOYMENT_NAME)
        )
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            return {"deployment": config.DEPLOYMENT_NAME, "current": None, "versions": []}
        raise
    info = response.worker_deployment_info
    routing = info.routing_config
    current = routing.current_deployment_version.build_id or None
    versions = [
        {
            "build_id": s.deployment_version.build_id,
            "current": s.deployment_version.build_id == current,
            "drainage": _drainage(s.drainage_status),
        }
        for s in info.version_summaries
    ]
    return {"deployment": config.DEPLOYMENT_NAME, "current": current,
            "versions": sorted(versions, key=lambda v: v["build_id"])}


async def set_current(client: Client, build_id: str) -> dict:
    """Make ``build_id`` the version new runs start on. It must have a worker polling."""
    if build_id not in KNOWN_VERSIONS:
        raise ValueError(f"unknown version {build_id!r}")
    await client.workflow_service.set_worker_deployment_current_version(
        SetWorkerDeploymentCurrentVersionRequest(
            namespace=client.namespace,
            deployment_name=config.DEPLOYMENT_NAME,
            build_id=build_id,
            identity="agentic-sdlc-console",
        )
    )
    return await describe(client)


async def ensure_current(client: Client) -> str | None:
    """On a fresh deployment nothing is current yet, so no versioned worker would take new
    runs. Make v1 current once its worker has registered. Returns the current version."""
    state = await describe(client)
    if state["current"]:
        return state["current"]
    if any(v["build_id"] == "v1" for v in state["versions"]):
        return (await set_current(client, "v1"))["current"]
    return None


def _drainage(status: int) -> str:
    return {0: "", 1: "draining", 2: "drained"}.get(status, "")
