"""The Workflow sandbox configuration, shared by the worker and the tests."""

from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions


def workflow_runner() -> SandboxedWorkflowRunner:
    """Settings, data models and Activity definitions are shared with Workflow code rather than
    re-imported per run: they hold no Workflow state, and config reads the environment once,
    which the sandbox forbids during a Workflow."""
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "agentic_sdlc.config", "agentic_sdlc.models", "agentic_sdlc.activities",
            "agentic_sdlc.agents.tools", "agentic_sdlc.agents.harness_ext", "agentic_sdlc.agents.registry",
            "agentic_sdlc.agents.runtime", "agentic_sdlc.faults", "agentic_sdlc.temporal_client",
        )
    )
