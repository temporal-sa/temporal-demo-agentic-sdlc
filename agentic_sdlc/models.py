"""Data shared by the pipeline Workflow, the agents, the Activities and the web server.

Key-free and import-light: the web server imports this module, and so does Workflow code,
so it must not construct model clients or touch the environment.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Provider = Literal["anthropic", "openai", "google"]
Autonomy = Literal["supervised", "auto"]
StepStatus = Literal["pending", "active", "waiting", "retrying", "completed", "failed", "skipped"]


class ModelChoice(BaseModel):
    provider: Provider = "anthropic"
    model: str = "claude-sonnet-5-5"

    @property
    def key(self) -> str:
        """The key the worker's model registry uses: ``provider:model``."""
        return f"{self.provider}:{self.model}"


class RoleModels(BaseModel):
    """Which model each agent role uses. The reviewer defaults to a different provider from
    the coder, so a second model family checks the first one's work."""

    planner: ModelChoice = Field(default_factory=lambda: ModelChoice(model="claude-opus-5-5"))
    coder: ModelChoice = Field(default_factory=ModelChoice)
    reviewer: ModelChoice = Field(default_factory=lambda: ModelChoice(provider="openai", model="gpt-5.5"))


class Workspace(BaseModel):
    """A git branch an agent works on. The branch is the durable workspace: every edit is
    committed and pushed, so any worker can rebuild the checkout."""

    repo: str
    branch: str


class Usage(BaseModel):
    """Tokens and estimated cost of one agent turn. Cost is an estimate from the
    genai-prices database, not a provider bill."""

    model: str = ""
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            model=self.model or other.model,
            requests=self.requests + other.requests,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 6),
        )


# -- Agent inputs and outputs ------------------------------------------------------------


class AgentInit(BaseModel):
    """What every agent in a run starts with."""

    run_id: str
    model: ModelChoice
    workspace: Workspace | None = None
    autonomy: Autonomy = "auto"


class Subtask(BaseModel):
    """One unit of work a single coding agent does on its own branch."""

    id: str = Field(description="Short slug, e.g. 'pricing-logic'.")
    title: str
    description: str = Field(description="What to change and how to know it's done.")
    files: list[str] = Field(default_factory=list, description="Files this subtask is expected to touch.")


class Plan(BaseModel):
    """The planner's proposal, which a human approves before any code is written."""

    summary: str = Field(description="Two or three sentences a reviewer can approve or push back on.")
    acceptance_criteria: list[str]
    subtasks: list[Subtask] = Field(
        description="One to three independent subtasks that can be coded in parallel on separate branches."
    )


class PlanRequest(BaseModel):
    """Ask the planner for a plan, optionally with the reviewer's feedback on the last one."""

    issue_title: str
    issue_body: str
    feedback: str | None = None


class PlanReply(BaseModel):
    """The plan and what it cost to make."""

    plan: Plan
    usage: Usage = Field(default_factory=Usage)


class CodeRequest(BaseModel):
    """Ask a coder to implement its subtask, or to address feedback on it."""

    issue_title: str
    plan_summary: str
    subtask: Subtask
    feedback: str | None = Field(default=None, description="Review comments or test failures to address.")


class CodeReply(BaseModel):
    """What the coder changed, whether its tests passed, and what the turn cost."""

    summary: str
    tests_passed: bool
    usage: Usage = Field(default_factory=Usage)


class ReviewComment(BaseModel):
    """One actionable comment on one file."""

    file: str
    comment: str


class Review(BaseModel):
    """A reviewer's verdict on a pull request."""

    verdict: Literal["approve", "request_changes"]
    summary: str
    comments: list[ReviewComment] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    """Ask a reviewer to check a diff against the approved plan."""

    issue_title: str
    plan: Plan
    diff: str
    focus: Literal["quality", "security"] = "quality"


class ReviewReply(BaseModel):
    """The review and what it cost."""

    review: Review
    usage: Usage = Field(default_factory=Usage)


# -- The run, as the pipeline publishes it for the UI -------------------------------------


class Step(BaseModel):
    key: str
    label: str
    status: StepStatus = "pending"
    detail: str = ""
    note: str = ""
    attempt: int = 0
    started_at: float | None = None
    ended_at: float | None = None
    lane: str | None = None  # parallel coder lanes share a parent step
    agent_workflow_id: str | None = None


class Gate(BaseModel):
    key: Literal["plan", "merge", "production", "budget"]
    question: str
    context: str = ""
    waiting_since: float


class AgentRef(BaseModel):
    role: str
    workflow_id: str
    model: str
    lane: str | None = None


class Proof(BaseModel):
    """Evidence that a decision criterion was shown live, for the scorecard."""

    criterion: Literal["D1", "D2", "D3", "D4", "D5", "D6"]
    detail: str
    at: float


class RunState(BaseModel):
    run_id: str
    version: str
    title: str
    status: Literal["running", "waiting", "completed", "rolled_back", "failed", "cancelled"] = "running"
    steps: list[Step] = Field(default_factory=list)
    gate: Gate | None = None
    agents: list[AgentRef] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    budget_usd: float = 0.0
    issue_url: str | None = None
    pr_url: str | None = None
    revert_pr_url: str | None = None
    plan: Plan | None = None
    review: Review | None = None
    staging_sha: str | None = None
    canary_sha: str | None = None
    proofs: list[Proof] = Field(default_factory=list)
    started_at: float = 0.0
    ended_at: float | None = None


class RunRequest(BaseModel):
    run_id: str
    owner: str
    issue_title: str
    issue_body: str
    models: RoleModels = Field(default_factory=RoleModels)
    autonomy: Autonomy = "auto"
    budget_usd: float = 3.0
    canary_bake_seconds: int = 90
    canary_check_seconds: int = 15


class GateDecision(BaseModel):
    approve: bool
    feedback: str | None = None
    raise_budget_usd: float | None = None

