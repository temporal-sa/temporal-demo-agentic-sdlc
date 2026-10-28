"""The durable Pydantic AI agents, one per role, built once when the worker imports this module.

Pydantic AI's Temporal integration needs each ``TemporalAgent`` built at import time so its
Activities (model requests) can be registered on the worker with ``AgentPlugin``. Every
agent registers every model this worker has a key for; each run picks its model by key.
Worker-only: building the registry constructs provider clients, which read API keys.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import TemporalAgent
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig

from agentic_sdlc.agents import harness_ext
from agentic_sdlc.agents.registry import build_registry
from agentic_sdlc.agents.tools import CODER_TOOLS, READ_ONLY_TOOLS
from agentic_sdlc.models import Plan, Review

MODELS = build_registry()

# One model call per Activity. Heartbeats (harness_ext.DemoModel) notice a dead worker in
# about 10 seconds, and the call is retried on another worker. Rate limits, 5xx errors and a
# provider that is down retry with backoff (2, 4, 8, 16, then every 30 seconds) for up to half an
# hour, so an outage ends when the provider is back and not when a retry count runs out. A
# rejected request (most 4xx) does not retry.
MODEL_ACTIVITY = ActivityConfig(
    start_to_close_timeout=timedelta(minutes=10),
    heartbeat_timeout=timedelta(seconds=10),
    retry_policy=RetryPolicy(
        initial_interval=timedelta(seconds=2),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=30),
        non_retryable_error_types=["ProviderRejected"],
    ),
    schedule_to_close_timeout=timedelta(minutes=30),
)


class CoderOutput(BaseModel):
    summary: str = Field(description="What you changed and why, for the pull request description.")
    tests_passed: bool = Field(description="True only if your last run_tests call passed.")


PLANNER_INSTRUCTIONS = """\
You are the planning agent in an automated software delivery pipeline for a small static web
storefront (HTML, CSS, vanilla JavaScript ES modules, node:test tests, no dependencies).

Read the issue, look at the repository with your read-only tools, and produce a plan:
- a short summary a human reviewer can approve or push back on,
- concrete acceptance criteria,
- one to three subtasks that can be coded IN PARALLEL on separate branches by separate agents.
  Subtasks must not edit the same lines of the same file; prefer splitting logic (src/cart.js
  plus its tests) from presentation (index.html, styles.css, src/app.js). Use one subtask
  when the change is small.

Be economical: read only the files you need."""

CODER_INSTRUCTIONS = """\
You are a coding agent in an automated software delivery pipeline. You work on your own git
branch of a small static web storefront (HTML, CSS, vanilla JavaScript ES modules, node:test).
Read AGENTS.md first. Then implement ONLY your subtask.

- Every write_file/edit_file call is committed and pushed to your branch immediately.
- Keep business logic in src/cart.js with tests in tests/; keep DOM code in src/app.js.
- Prefer edit_file for small changes. Don't add dependencies.
- Run run_tests before you finish. If tests fail, fix them and run again.
- run_tests installs the dependencies first. When a dependency upgrade breaks things, read the
  dependency's changelog at node_modules/<name>/CHANGELOG.md and follow its migration notes;
  adapt our code to the new version rather than pinning the old one.
- Finish with a short summary of what you changed, file by file, and why."""

DIAGNOSE_INSTRUCTIONS = """\
A dependency upgrade on your branch breaks the build. Work out why before changing anything:
run run_tests (it installs dependencies), read the dependency's changelog at
node_modules/<name>/CHANGELOG.md, and read the code that uses the dependency. Report what changed
in the dependency (cite the changelog), what broke and how it failed, the root cause in our code,
and the fix, file by file. Don't edit files in this turn."""

REVIEWER_INSTRUCTIONS = """\
You are a code-review agent in an automated delivery pipeline. You review a pull request
diff for a small static web storefront against the approved plan.

Approve when the change meets the acceptance criteria, keeps tests meaningful, and has no
obvious bug. Request changes only for real problems, each with the file, the line in the new
version of the file when it's about one line, and a specific, actionable comment. Don't
nitpick style."""

SECURITY_INSTRUCTIONS = """\
You are a security-review agent in an automated delivery pipeline. Review a pull request diff
for a static web storefront for security problems only: DOM injection (innerHTML with
untrusted data), unsafe use of eval or Function, secrets in code, and data sent to third
parties. Approve when there is nothing material; otherwise request changes with specific,
actionable comments."""


def _agent(name: str, *, instructions: str, output_type, tools) -> TemporalAgent:
    toolset, tool_config = harness_ext.workspace_toolset(tools, id=f"{name}-tools")
    return TemporalAgent(
        Agent(
            MODELS[next(iter(MODELS))],
            instructions=instructions,
            output_type=output_type,
            deps_type=harness_ext.DemoDeps,
            toolsets=[toolset],
        ),
        name=name,
        models=MODELS,
        event_stream_handler=harness_ext.event_stream_handler,
        model_activity_config=MODEL_ACTIVITY,
        tool_activity_config=tool_config,
    )


PLANNER = _agent("planner", instructions=PLANNER_INSTRUCTIONS, output_type=Plan, tools=READ_ONLY_TOOLS)
CODER = _agent("coder", instructions=CODER_INSTRUCTIONS, output_type=CoderOutput, tools=CODER_TOOLS)
REVIEWER = _agent("reviewer", instructions=REVIEWER_INSTRUCTIONS, output_type=Review, tools=READ_ONLY_TOOLS)
SECURITY_REVIEWER = _agent(
    "security-reviewer", instructions=SECURITY_INSTRUCTIONS, output_type=Review, tools=READ_ONLY_TOOLS
)

ALL_AGENTS = [PLANNER, CODER, REVIEWER, SECURITY_REVIEWER]
