"""The agents, each a temporal-agent-harness agent Workflow.

An agent is a long-lived Workflow that accepts messages (``@agent.accepts`` handlers). Its model
calls and tool calls are Activities, so a worker crash mid-turn loses nothing that already
finished: the turn continues on another worker from its last completed step. The pipeline
starts one agent per role as a child Workflow and sends it messages; agent events (model
output, tool calls, approvals) stream to the UI through Redis.

Versioning: a versioned worker (worker.py) runs every Workflow Pinned by default, so an agent
finishes on the build its run started on.
"""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from pydantic_ai.usage import UsageLimits

    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import AgentConfig, ToolApprovalPolicy
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
    from temporal_agent_harness.harness.redis_event_stream import RedisEventStream

    from agentic_sdlc.agents.harness_ext import DemoDeps
    from agentic_sdlc.agents.registry import settings_for
    from agentic_sdlc.agents.runtime import CODER, PLANNER, REVIEWER, SECURITY_REVIEWER
    from agentic_sdlc.models import (
        AgentInit,
        Clarification,
        ClarifyReply,
        Triage,
        TriageReply,
        TriageRequest,
        Autonomy,
        CodeReply,
        CodeRequest,
        PlanReply,
        PlanRequest,
        ReviewReply,
        ReviewRequest,
        Usage,
    )

CLARIFY_INSTRUCTIONS = """\
Before planning, decide whether the issue leaves any decision open that would change what gets
built: placement, wording, edge cases, behaviour on bad input. If so, ask up to three short,
specific questions a developer can answer in a sentence each. Don't ask about things you can
decide sensibly yourself or learn from the code. If nothing material is open, ask nothing."""

TRIAGE_INSTRUCTIONS = """\
Classify the developer's comment against the approved plan. It is feedback when the change can
be made within the plan and its acceptance criteria (a bug, wording, style, a missed criterion).
It is a scope change when it adds, removes or changes a requirement the plan didn't cover, so
the plan has to be re-approved. Don't use any tools."""

# Caps per turn so one runaway agent can't spend the run's budget on its own.
CODER_LIMITS = UsageLimits(request_limit=40)
READER_LIMITS = UsageLimits(request_limit=15)


def approval_policy(autonomy: Autonomy) -> ToolApprovalPolicy:
    """The autonomy dial. Reading, searching, testing and editing the agent's own branch never
    need a person. ``run_command`` runs arbitrary shell over agent-written code, so in
    supervised mode a human approves each call."""
    allowed = {"write_file", "edit_file"} | ({"run_command"} if autonomy == "auto" else set())
    return ToolApprovalPolicy(auto_approve_inherently_safe=True, auto_approve_tools=frozenset(allowed))


def usage_of(result, model_key: str) -> Usage:
    """Tokens for a turn. Cost is filled in by the pipeline's agent-turn Activity."""
    usage = result.usage() if callable(result.usage) else result.usage
    return Usage(
        model=model_key,
        requests=usage.requests,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
    )


class _AgentBase:
    def _setup(self, config: AgentConfig, data: AgentInit) -> None:
        self._init = data
        self._history = []
        self._runner = AgentWorkflowRunner(
            config,
            stream=RedisEventStream(),
            approval_policy_default=approval_policy(data.autonomy),
        )

    def _deps(self) -> DemoDeps:
        return DemoDeps(runner=self._runner, workspace=self._init.workspace)


@agent.defn(name="PlannerAgent")
class PlannerAgent(_AgentBase):
    @agent.init
    def __init__(self, config: AgentConfig, data: AgentInit) -> None:
        self._setup(config, data)

    @agent.accepts
    async def clarify(self, request: PlanRequest) -> ClarifyReply:
        """Ask the developer about decisions the issue leaves open, before planning."""
        result = await PLANNER.run(
            f"Issue: {request.issue_title}\n\n{request.issue_body}",
            instructions=CLARIFY_INSTRUCTIONS,
            output_type=Clarification,
            deps=self._deps(),
            message_history=self._history,
            model=self._init.model.key,
            model_settings=settings_for(self._init.model),
            usage_limits=READER_LIMITS,
        )
        self._history = result.all_messages()
        return ClarifyReply(clarification=result.output, usage=usage_of(result, self._init.model.key))

    @agent.accepts
    async def triage(self, request: TriageRequest) -> TriageReply:
        """Decide whether a person's comment fits the approved plan or changes its scope."""
        prompt = (
            f"Approved plan: {request.plan.summary}\nAcceptance criteria:\n- "
            + "\n- ".join(request.plan.acceptance_criteria)
            + f"\n\nComment from a developer on the pull request:\n{request.comment}"
        )
        result = await PLANNER.run(
            prompt,
            instructions=TRIAGE_INSTRUCTIONS,
            output_type=Triage,
            deps=self._deps(),
            model=self._init.model.key,
            model_settings=settings_for(self._init.model),
            usage_limits=UsageLimits(request_limit=3),
        )
        return TriageReply(triage=result.output, usage=usage_of(result, self._init.model.key))

    @agent.accepts
    async def plan(self, request: PlanRequest) -> PlanReply:
        """Turn an issue into a plan of one to three parallel subtasks."""
        prompt = f"Issue: {request.issue_title}\n\n{request.issue_body}"
        if request.clarifications:
            prompt += f"\n\nThe developer answered your questions:\n{request.clarifications}"
        if request.feedback:
            prompt += f"\n\nRevise your previous plan. The developer's feedback:\n{request.feedback}"
        result = await PLANNER.run(
            prompt,
            deps=self._deps(),
            message_history=self._history,
            model=self._init.model.key,
            model_settings=settings_for(self._init.model),
            usage_limits=READER_LIMITS,
        )
        self._history = result.all_messages()
        return PlanReply(plan=result.output, usage=usage_of(result, self._init.model.key))


@agent.defn(name="CoderAgent")
class CoderAgent(_AgentBase):
    @agent.init
    def __init__(self, config: AgentConfig, data: AgentInit) -> None:
        self._setup(config, data)

    @agent.accepts
    async def code(self, request: CodeRequest) -> CodeReply:
        """Implement one subtask on this agent's branch, or address feedback on it."""
        if request.feedback:
            prompt = f"Address this feedback on your subtask, then run the tests:\n{request.feedback}"
        else:
            prompt = (
                f"Issue: {request.issue_title}\nApproved plan: {request.plan_summary}\n\n"
                f"Your subtask ({request.subtask.id}): {request.subtask.title}\n"
                f"{request.subtask.description}\nLikely files: {', '.join(request.subtask.files) or 'your call'}"
            )
        result = await CODER.run(
            prompt,
            deps=self._deps(),
            message_history=self._history,
            model=self._init.model.key,
            model_settings=settings_for(self._init.model),
            usage_limits=CODER_LIMITS,
        )
        self._history = result.all_messages()
        return CodeReply(
            summary=result.output.summary,
            tests_passed=result.output.tests_passed,
            usage=usage_of(result, self._init.model.key),
        )


@agent.defn(name="ReviewerAgent")
class ReviewerAgent(_AgentBase):
    @agent.init
    def __init__(self, config: AgentConfig, data: AgentInit) -> None:
        self._setup(config, data)

    @agent.accepts
    async def review(self, request: ReviewRequest) -> ReviewReply:
        """Review a pull request diff against the approved plan."""
        reviewer = SECURITY_REVIEWER if request.focus == "security" else REVIEWER
        prompt = (
            f"Issue: {request.issue_title}\nPlan: {request.plan.summary}\n"
            f"Acceptance criteria:\n- " + "\n- ".join(request.plan.acceptance_criteria)
            + f"\n\nDiff:\n```diff\n{request.diff}\n```"
        )
        result = await reviewer.run(
            prompt,
            deps=self._deps(),
            model=self._init.model.key,
            model_settings=settings_for(self._init.model),
            usage_limits=READER_LIMITS,
        )
        return ReviewReply(review=result.output, usage=usage_of(result, self._init.model.key))
