"""Runtime settings shared by the web and worker processes, read from the environment.

Nothing here reads a developer's Temporal config file: address, namespace and credentials
come from TEMPORAL_* variables only (see temporal_client.connect).
"""

from __future__ import annotations

import os
import re
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

BASE_TASK_QUEUE = os.environ.get("BASE_TASK_QUEUE", "agentic-sdlc")
OWNER_LABEL_PREFIX = "agentic-sdlc:"

# -- Session slots -------------------------------------------------------------------------
#
# A browser that starts a scenario leases a slot: a task queue from a small reused pool, polled
# by Workers that live inside the v1 and v2 worker processes (slots.py). The copy's own queue is
# never leased: it registers both versions and runs the GitHub Activities. Cloud allows 100 task
# queues per Worker Deployment Version, so the pool is capped at 99.
MAX_SESSION_SLOTS = 99
# A crashable workspace is one person's, so it gets a small pool.
SESSION_SLOTS = int(os.environ.get("SESSION_SLOTS") or (8 if TASK_QUEUE == BASE_TASK_QUEUE else 2))
SESSION_IDLE_MINUTES = float(os.environ.get("SESSION_IDLE_MINUTES", "120"))
# A crash that nobody undoes restores itself, so a forgotten one can't strand a run.
CRASH_AUTO_RESTORE_SECONDS = float(os.environ.get("CRASH_AUTO_RESTORE_SECONDS", "300"))
if not 1 <= SESSION_SLOTS <= MAX_SESSION_SLOTS:
    raise RuntimeError(f"SESSION_SLOTS must be 1 to {MAX_SESSION_SLOTS}, not {SESSION_SLOTS}")

_SLOT_QUEUE = re.compile(r"(?P<copy>.+)--s(?P<n>[0-9]{2})")
# GitHub caps a label's name at 50 characters.
GITHUB_LABEL_MAX = 50


def slot_queue(n: int, copy: str | None = None) -> str:
    return f"{copy or TASK_QUEUE}--s{n:02d}"


def parse_slot_queue(queue: str) -> tuple[str, int] | None:
    """The (copy queue, slot number) a slot queue was made from, or None if it isn't one."""
    found = _SLOT_QUEUE.fullmatch(queue)
    return (found["copy"], int(found["n"])) if found and int(found["n"]) >= 1 else None


# The Slot Workflows and their housekeeping run on an unversioned queue; each pipeline version's
# process hosts its slot Workers from its own queue, since a process serves one version.
HOST_QUEUE = f"{TASK_QUEUE}-host"


def host_queue(copy: str) -> str:
    return f"{copy}-host"


def version_host_queue(version: str, copy: str | None = None) -> str:
    return f"{copy or TASK_QUEUE}-host-{version}"


def slot_workflow_id(n: int, copy: str | None = None) -> str:
    return f"{copy or TASK_QUEUE}.slot-{n:02d}"


def check_label_length() -> None:
    """Fail at startup, not on the first scenario, if a slot's owner label wouldn't fit on GitHub."""
    longest = f"{OWNER_LABEL_PREFIX}{slot_queue(SESSION_SLOTS)}"
    if len(longest) > GITHUB_LABEL_MAX:
        raise RuntimeError(f"Owner label {longest!r} is {len(longest)} characters; GitHub allows {GITHUB_LABEL_MAX}. "
                           "Use a shorter TEMPORAL_TASK_QUEUE.")


REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
# Crashable clones share one Redis per clone namespace, but keep the queue in the prefix so
# a shared Redis can never mix two workspaces' agent events.
REDIS_KEY_PREFIX = f"agent-events:{TASK_QUEUE}"

GITHUB_SANDBOX_REPO = os.environ.get("GITHUB_SANDBOX_REPO", "temporal-sa/agentic-sdlc-storefront")

# Every copy of the demo (the hosted one, each crashable workspace, each laptop) shares one
# GitHub repo. Issues and PRs carry a label naming the copy that owns them, and each copy only
# acts on its own. A laptop sets OWNER_LABEL (scripts/dev.py does); the hosted copy and its
# workspaces derive theirs from the task queue.
OWNER_LABEL =os.environ.get("OWNER_LABEL") or f"{OWNER_LABEL_PREFIX}{TASK_QUEUE}"
# The hosted copy also handles unlabeled items and starts workspace-owned runs on their queues,
# since GitHub delivers webhooks to the hosted URL only.
IS_LOCAL_COPY = bool(os.environ.get("OWNER_LABEL"))
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
MAX_ACTIVE_GITHUB_RUNS = int(os.environ.get("MAX_ACTIVE_GITHUB_RUNS", "3"))
# Across all of a copy's slots: every run makes real model calls on the shared provider keys.
MAX_ACTIVE_RUNS_ALL_SLOTS = int(os.environ.get("MAX_ACTIVE_RUNS_ALL_SLOTS", "12"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
BASELINE_REF = os.environ.get("GITHUB_BASELINE_REF", "baseline")

# Where the worker keeps git checkouts. A cache only: every agent edit is pushed, so a new
# worker rebuilds a checkout from the branch.
WORKDIR_ROOT = Path(os.environ.get("WORKDIR_ROOT", Path(__file__).resolve().parent.parent / ".workdirs"))

# Cost guards for a hosted demo where every run makes real model calls.
DEFAULT_BUDGET_USD = float(os.environ.get("RUN_BUDGET_USD", "3.00"))

# Local only: the supervisor started by scripts/dev listens here for "kill the worker".
SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "")


def temporal_ui_url(namespace: str) -> str:
    """Base URL for this namespace in the Temporal UI, for links from the demo view."""
    if url := os.environ.get("TEMPORAL_UI_URL"):
        return url.rstrip("/")
    if os.environ.get("TEMPORAL_ADDRESS", "").endswith(".tmprl.cloud:7233"):
        return f"https://cloud.temporal.io/namespaces/{namespace}"
    return f"http://localhost:8233/namespaces/{namespace}"
