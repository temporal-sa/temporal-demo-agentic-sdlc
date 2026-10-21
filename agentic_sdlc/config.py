"""Runtime settings shared by the web and worker processes, read from the environment.

Nothing here reads a developer's Temporal config file: address, namespace and credentials
come from TEMPORAL_* variables only (see temporal_client.connect).
"""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Load .env.local for local runs. Hosted deployments set real environment variables,
    which win over the file."""
    path = Path(__file__).resolve().parent.parent / ".env.local"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if value:
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

# The catalog gives every crashable workspace its own queue (`agentic-sdlc-<workspace>`)
# through this one variable. Every Workflow and Activity in a run inherits it.
TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "agentic-sdlc")

# One worker runs per pipeline version (build IDs v1 and v2), in the catalog and locally. The
# Worker Deployment is named after the task queue, so each crashable workspace has its own and
# its rollouts stay inside it (see versions.py).
DEPLOYMENT_NAME = os.environ.get("TEMPORAL_DEPLOYMENT_NAME") or f"{TASK_QUEUE}-pipeline"
BUILD_ID = os.environ.get("TEMPORAL_WORKER_BUILD_ID", "")
PIPELINE_VERSION = os.environ.get("PIPELINE_VERSION", "v1")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
# Crashable clones share one Redis per clone namespace, but keep the queue in the prefix so
# a shared Redis can never mix two workspaces' agent events.
REDIS_KEY_PREFIX = f"agent-events:{TASK_QUEUE}"

GITHUB_SANDBOX_REPO = os.environ.get("GITHUB_SANDBOX_REPO", "temporal-sa/agentic-sdlc-storefront")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
BASELINE_REF = os.environ.get("GITHUB_BASELINE_REF", "baseline")

# Where the worker keeps git checkouts. A cache only: every agent edit is pushed, so a new
# worker rebuilds a checkout from the branch.
WORKDIR_ROOT = Path(os.environ.get("WORKDIR_ROOT", Path(__file__).resolve().parent.parent / ".workdirs"))

# Cost guards for a hosted demo where every run makes real model calls.
DEFAULT_BUDGET_USD = float(os.environ.get("RUN_BUDGET_USD", "3.00"))
MAX_ACTIVE_RUNS_PER_USER = int(os.environ.get("MAX_ACTIVE_RUNS_PER_USER", "2"))

TRUST_AUTH_HEADERS = os.environ.get("TRUST_AUTH_HEADERS", "").lower() == "true"
SESSION_SECRET = os.environ.get("SESSION_SECRET", "local-dev-secret-change-me")
DEV_USER = os.environ.get("DEV_USER", "local-presenter")

# Local only: the supervisor started by scripts/dev listens here for "kill the worker".
SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "")


def temporal_ui_url(namespace: str) -> str:
    """Base URL for this namespace in the Temporal UI, for links from the demo view."""
    if url := os.environ.get("TEMPORAL_UI_URL"):
        return url.rstrip("/")
    if os.environ.get("TEMPORAL_ADDRESS", "").endswith(".tmprl.cloud:7233"):
        return f"https://cloud.temporal.io/namespaces/{namespace}"
    return f"http://localhost:8233/namespaces/{namespace}"
