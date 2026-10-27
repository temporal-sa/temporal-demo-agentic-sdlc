"""Run Workflow tests against a local Temporal dev server.

The agents and the outside world are replaced: fake agent Workflows stand in for the harness
agents, and Activities with the real names return canned answers. People act the way they do in
the demo, on GitHub: each comment reaches the run as the ``github_comment`` Signal the webhook
sends. What's under test is the orchestration: gates and commands, the conversation with people,
the parallel build, steering, the test-fix loop, the budget guard, scope changes, the canary and
the rollback saga, the dependency repair, and PR reviews.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter

import pytest
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentic_sdlc.activities.agent_turn import AgentTurn, AgentTurnResult, TurnStats
from agentic_sdlc.activities.checks import TestRun
from agentic_sdlc.activities.deploy import CheckInput, CheckResult, DeployCall
from agentic_sdlc.activities.github import (
    BranchesInput,
    BranchRef,
    ClosePrInput,
    CommentInput,
    CommentsInput,
    DiffInput,
    HumanComment,
    MergeInput,
    MergePrInput,
    MergeResult,
    PrepareRunInput,
    PullRequest,
    PullRequestInfo,
    PullRequestInput,
    PullRequestRef,
    RevertInput,
    ReviewInput,
    RunRepo,
    StatusInput,
    UpdatePrInput,
)
from agentic_sdlc.models import Plan, RunKind, RunRequest, RunState, Subtask, Usage, Workspace
from agentic_sdlc.pipeline.base import GITHUB_REFS
from agentic_sdlc.pipeline.dependency import DependencyRepair
from agentic_sdlc.pipeline.environments import EnvState
from agentic_sdlc.pipeline.review import PullRequestReview
from agentic_sdlc.pipeline.workflow import IssueToProduction
from agentic_sdlc.sandbox import workflow_runner

PLAN = Plan(
    summary="Add a discount code.",
    acceptance_criteria=["ORBIT10 takes 10% off"],
    subtasks=[Subtask(id="logic", title="Discount logic", description="cart.js"),
              Subtask(id="ui", title="Discount field", description="app.js")],
)
ISSUE = 1
BUMP_PR = 40
PERSON_PR = 50


class World:
    """What the fake outside world does, and what it was asked to do."""

    def __init__(self, *, canary_healthy=True, failing_test_runs=0, review="approve", turn_cost=0.10,
                 questions=(), code_delay=0.0):
        self.canary_healthy = canary_healthy
        self.failing_test_runs = failing_test_runs
        self.review = review
        self.turn_cost = turn_cost
        self.questions = list(questions)
        self.code_delay = code_delay
        self.calls: Counter[str] = Counter()
        self.deploys: list[str] = []
        self.handlers: list[str] = []
        self.payloads: list[tuple[str, dict]] = []
        self.on_github: list[HumanComment] = []  # comments whose webhook never arrived; only a catch-up finds them
        self.posts: list[tuple[int, str]] = []  # (issue or PR number, body) the run commented
        self.authors: list[tuple[str, str]] = []  # (agent label, body) of each comment
        self.checks: list[str] = []  # the agent's commit status, in order
        self.pr_body = ""
        self.reverts: list[RevertInput] = []

    def activities(self):
        world = self

        @activity.defn(name="prepare_run")
        async def prepare_run(input: PrepareRunInput) -> RunRepo:
            world.calls["prepare_run"] += 1
            return RunRepo(repo="o/r", issue_number=input.issue_number, issue_url=f"https://x/issues/{input.issue_number}",
                           base_branch="runs/x/main", base_sha="abc")

        @activity.defn(name="create_branches")
        async def create_branches(_: BranchesInput) -> None:
            world.calls["create_branches"] += 1

        @activity.defn(name="comment_on_issue")
        async def comment_on_issue(input: CommentInput) -> None:
            world.posts.append((input.issue_number, input.body))
            world.authors.append((input.agent, input.body))
            if input.reopen:
                world.calls["reopen"] += 1

        @activity.defn(name="merge_branches")
        async def merge_branches(_: MergeInput) -> MergeResult:
            return MergeResult(sha="def", conflicts=[])

        @activity.defn(name="open_pull_request")
        async def open_pull_request(_: PullRequestInput) -> PullRequest:
            world.calls["open_pr"] += 1
            number = 1 + world.calls["open_pr"]
            return PullRequest(number=number, url=f"https://x/pull/{number}", head_sha="def")

        @activity.defn(name="get_pull_request")
        async def get_pull_request(ref: PullRequestRef) -> PullRequestInfo:
            if ref.number == BUMP_PR:
                return PullRequestInfo(number=ref.number, url=f"https://x/pull/{ref.number}",
                                       title="Bump orbit-money from 1.0.0 to 2.0.0", body="Bumps orbit-money.",
                                       author="github-actions[bot]", head_branch="dependency-bump/orbit-money-2.0.0-1",
                                       head_sha="b1", base_branch="main", from_fork=False)
            return PullRequestInfo(number=ref.number, url=f"https://x/pull/{ref.number}",
                                   title="Add a bulk discount", body="10% off 3+ items", author="maya",
                                   head_branch="review-sample/1", head_sha="s1", base_branch="main", from_fork=False)

        @activity.defn(name="set_commit_status")
        async def set_commit_status(input: StatusInput) -> None:
            world.checks.append(input.state)

        @activity.defn(name="update_pull_request")
        async def update_pull_request(input: UpdatePrInput) -> None:
            world.pr_body = input.body

        @activity.defn(name="close_pull_request")
        async def close_pull_request(_: ClosePrInput) -> None:
            world.calls["close_pr"] += 1

        @activity.defn(name="fetch_comments")
        async def fetch_comments(_: CommentsInput) -> list[HumanComment]:
            world.calls["fetch_comments"] += 1
            return list(world.on_github)

        @activity.defn(name="get_diff")
        async def get_diff(_: DiffInput) -> str:
            return "+ discount"

        @activity.defn(name="post_review")
        async def post_review(_: ReviewInput) -> None:
            world.calls["post_review"] += 1

        @activity.defn(name="merge_pull_request")
        async def merge_pull_request(_: MergePrInput) -> str:
            world.calls["merge"] += 1
            return "merged123"

        @activity.defn(name="revert_merge")
        async def revert_merge(input: RevertInput) -> PullRequest:
            world.calls["revert"] += 1
            world.reverts.append(input)
            return PullRequest(number=3, url="https://x/pull/3", head_sha="rev")

        @activity.defn(name="branch_head")
        async def branch_head(_: BranchRef) -> str:
            world.calls["branch_head"] += 1
            return f"adjusted{world.calls['branch_head']}"

        @activity.defn(name="run_agent_turn")
        async def run_agent_turn(turn: AgentTurn) -> AgentTurnResult:
            role = turn.workflow_id.rsplit(".", 1)[-1]
            world.handlers.append(f"{role}:{turn.handler}")
            world.payloads.append((f"{role}:{turn.handler}", turn.payload))
            usage = Usage(model="anthropic:claude-sonnet-5-5", requests=2, input_tokens=100, output_tokens=50,
                          cost_usd=world.turn_cost).model_dump()
            if turn.handler == "clarify":
                output = {"clarification": {"questions": world.questions}, "usage": usage}
            elif turn.handler == "triage":
                comment = turn.payload["comment"]
                kind = "scope_change" if "also" in comment.lower() else "feedback"
                output = {"triage": {"kind": kind, "reason": f"classified as {kind}"}, "usage": usage}
            elif turn.handler == "plan":
                output = {"plan": PLAN.model_dump(), "usage": usage}
            elif turn.handler == "diagnose":
                output = {"diagnosis": {"what_changed": "2.0 takes integer cents; roundCents is gone",
                                        "what_broke": "cart.js imports roundCents", "root_cause": "cart.js uses the 1.x API",
                                        "fix_plan": "Use toCents in cart.js"}, "usage": usage}
            elif turn.handler == "code":
                if world.code_delay and turn.payload.get("feedback") is None:
                    await asyncio.sleep(world.code_delay)
                output = {"summary": "done", "tests_passed": True, "usage": usage}
            else:
                verdict, world.review = world.review, "approve"  # request changes at most once
                output = {"review": {"verdict": verdict, "summary": "looks fine", "comments": []}, "usage": usage}
            return AgentTurnResult(output=output, cost_usd=world.turn_cost, attempt=1, stats=TurnStats(model_calls=2))

        @activity.defn(name="run_branch_tests")
        async def run_branch_tests(_: Workspace) -> TestRun:
            world.calls["tests"] += 1
            if world.failing_test_runs > 0:
                world.failing_test_runs -= 1
                return TestRun(passed=False, output="not ok 1 - SyntaxError: does not provide an export named 'roundCents'")
            return TestRun(passed=True, output="ok")

        @activity.defn(name="apply_deployment")
        async def apply_deployment(call: DeployCall) -> EnvState:
            world.deploys.append(call.request.action)
            return EnvState()

        @activity.defn(name="check_environment")
        async def check_environment(input: CheckInput) -> CheckResult:
            healthy = input.env == "staging" or world.canary_healthy
            return CheckResult(healthy=healthy, probes=10, failed_probes=0 if healthy else 6, detail="checked")

        return [prepare_run, create_branches, comment_on_issue, merge_branches, open_pull_request, get_pull_request,
                set_commit_status, update_pull_request, close_pull_request, fetch_comments, get_diff, post_review,
                merge_pull_request, revert_merge, branch_head, run_agent_turn, run_branch_tests, apply_deployment, check_environment]


class _FakeAgentBase:
    def __init__(self) -> None:
        self.closed = False

    async def _wait(self) -> None:
        await workflow.wait_condition(lambda: self.closed)

    @workflow.signal
    def close(self) -> None:
        self.closed = True


@workflow.defn(name="PlannerAgent", sandboxed=False)
class FakePlanner(_FakeAgentBase):
    @workflow.run
    async def run(self, *_args) -> None:
        await self._wait()


@workflow.defn(name="CoderAgent", sandboxed=False)
class FakeCoder(_FakeAgentBase):
    @workflow.run
    async def run(self, *_args) -> None:
        await self._wait()


@workflow.defn(name="ReviewerAgent", sandboxed=False)
class FakeReviewer(_FakeAgentBase):
    @workflow.run
    async def run(self, *_args) -> None:
        await self._wait()


FAKE_AGENTS = [FakePlanner, FakeCoder, FakeReviewer]
RUNS = {"issue": IssueToProduction, "dependency": DependencyRepair, "review": PullRequestReview}


@pytest.fixture
async def env():
    # A real dev server with the search attribute the webhook finds runs by.
    async with await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter,
                                                      search_attributes=[GITHUB_REFS]) as env:
        yield env


# What people say on GitHub when a gate opens: a comment's text, several comments, or None to
# say nothing (a review window closes by itself; a conversation goes quiet).
def default_policy(gate: str, occurrence: int) -> str | None:
    return {"plan": "/approve", "production": "/ship", "budget": "/continue"}.get(gate)


class Person:
    """Comments on GitHub; the webhook delivers each one to the run as a Signal."""

    def __init__(self, handle: WorkflowHandle, home: int):
        self.handle, self.home, self.said = handle, home, 0

    async def comment(self, text: str, number: int | None = None) -> None:
        self.said += 1
        await self.handle.signal("github_comment", HumanComment(
            id=f"issue:{self.said}", number=number or self.home, author="maya", body=text,
            url=f"https://x/c/{self.said}", created_at="2099-01-01T00:00:00Z"))


async def run_scenario(client: Client, world: World, *, kind: RunKind = "issue", policy=default_policy,
                       autonomy="auto", budget=5.0, steer: str | None = None, pr_number: int | None = None,
                       idle_close_hours: float = 24.0, close_pr_when_quiet: bool = True) -> RunState:
    task_queue = f"test-{uuid.uuid4().hex[:6]}"
    home = ISSUE if kind == "issue" else pr_number
    async with Worker(client, task_queue=task_queue, workflows=[*RUNS.values(), *FAKE_AGENTS],
                      activities=world.activities(), workflow_runner=workflow_runner()):
        handle = await client.start_workflow(
            RUNS[kind].run,
            RunRequest(run_id=f"{kind}-{home}", kind=kind, repo="o/r", title="Discount", body="ORBIT10",
                       issue_number=ISSUE if kind == "issue" else None, pr_number=pr_number, autonomy=autonomy,
                       budget_usd=budget, canary_bake_seconds=2, canary_check_seconds=1, review_window_seconds=2,
                       idle_close_hours=idle_close_hours),
            id=f"{task_queue}.gh.{kind}-{home}", task_queue=task_queue,
        )
        driving = asyncio.create_task(_act(handle, Person(handle, home), policy, steer, close_pr_when_quiet))
        try:
            # A failing Workflow task retries forever; fail the test instead of hanging.
            return await asyncio.wait_for(handle.result(), timeout=60)
        finally:
            driving.cancel()


async def _act(handle: WorkflowHandle, person: Person, policy, steer: str | None, close_pr_when_quiet: bool) -> None:
    seen: set[tuple[str, float]] = set()
    occurrences: Counter[str] = Counter()
    steered = steer is None
    while True:
        await asyncio.sleep(0.2)
        state: RunState = await handle.query("state", result_type=RunState)
        if not steered and state.gate is None and any(s.lane and s.status == "active" for s in state.steps):
            await person.comment(steer)
            steered = True
        gate = state.gate
        if not gate or (gate.key, gate.waiting_since) in seen:
            continue
        seen.add((gate.key, gate.waiting_since))
        occurrences[gate.key] += 1
        text = policy(gate.key, occurrences[gate.key])
        number = int(gate.github_url.rsplit("/", 1)[-1]) if gate.github_url else None
        if text is not None:
            for comment in [text] if isinstance(text, str) else text:
                await person.comment(comment, number)
        elif gate.key == "conversation" and close_pr_when_quiet:
            await handle.signal("pull_request_closed", number)


def gate_log(log: list[str], inner=default_policy):
    def policy(gate, occurrence):
        log.append(gate)
        return inner(gate, occurrence)
    return policy


def payload(world: World, handler: str, nth: int = 0) -> dict:
    return [p for h, p in world.payloads if h == handler][nth]


def step(state: RunState, key: str):
    return next(s for s in state.steps if s.key == key)


# -- Issue to production: the lifecycle ----------------------------------------------------


async def test_happy_path_ships_through_approve_and_ship_on_github(env):
    world = World()
    gates: list[str] = []
    state = await run_scenario(env.client, world, policy=gate_log(gates))
    assert state.status == "completed"
    assert world.deploys == ["staging", "canary", "promote"]
    assert world.calls["revert"] == 0
    # The planner checks for questions, then two coders work in parallel, then the reviewer.
    assert sorted(world.handlers) == sorted(
        ["planner:clarify", "planner:plan", "coder-logic:code", "coder-ui:code", "reviewer-quality:review"])
    assert gates == ["plan", "merge", "production"]
    assert step(state, "gate:clarify").status == "skipped"  # no questions
    assert "Nobody commented" in step(state, "gate:merge").note  # the review window closed by itself
    assert "/approve" in step(state, "gate:plan").note and "/ship" in step(state, "gate:production").note
    assert world.checks == ["pending", "success"]  # the agent's required check on the PR
    assert any("Reply `/approve`" in body for n, body in world.posts if n == ISSUE)
    assert [c.kind for c in state.conversation] == ["command", "command"]
    # Every comment says who wrote it: the planner its plan, the pipeline its gates.
    assert next(agent for agent, body in world.authors if "### 🗺️ Plan" in body) == "Planner agent"
    assert next(agent for agent, body in world.authors if "Staging passed" in body) == "Pipeline"
    assert {p.criterion for p in state.proofs} >= {"D2", "D3", "D6"}


async def test_webhook_finds_the_run_by_its_issue_and_pr(env):
    world = World()
    await run_scenario(env.client, world)
    found = [w async for w in env.client.list_workflows("AgenticSdlcGitHubRefs = 'pr:2'")]
    assert len(found) == 1
    assert {"issue:1", "pr:2"} <= set(found[0].typed_search_attributes.get(GITHUB_REFS) or [])


async def test_each_gate_reads_github_once_to_catch_up_and_never_polls(env):
    world = World()
    gates: list[str] = []
    await run_scenario(env.client, world, policy=gate_log(gates))
    assert world.calls["fetch_comments"] == len(gates)


async def test_a_comment_whose_webhook_was_lost_is_caught_up_when_the_gate_opens(env):
    world = World()
    world.on_github.append(HumanComment(id="issue:99", number=ISSUE, author="sam", body="Keep it to one subtask",
                                        url="https://x/c/99", created_at="2099-01-01T00:00:00Z"))
    state = await run_scenario(env.client, world)
    assert payload(world, "planner:plan", 1)["feedback"] == "Keep it to one subtask"
    # Later gates read it again; it's handled once.
    assert [c.id for c in state.conversation].count("issue:99") == 1


async def test_unhealthy_canary_runs_the_rollback_saga(env):
    world = World(canary_healthy=False)
    state = await run_scenario(env.client, world)
    assert state.status == "rolled_back"
    assert world.deploys == ["staging", "canary", "rollback"]
    assert world.calls["revert"] == 1 and world.calls["reopen"] == 1
    assert any(p.criterion == "D4" for p in state.proofs)
    assert step(state, "canary").status == "failed"


async def test_failing_branch_tests_go_back_to_the_coder(env):
    world = World(failing_test_runs=1)
    state = await run_scenario(env.client, world)
    assert state.status == "completed"
    assert sum(1 for h in world.handlers if h.endswith(":code")) == 3  # two coders, one fix turn


async def test_budget_guard_pauses_until_someone_replies_continue(env):
    gates: list[str] = []
    world = World(turn_cost=0.6)
    state = await run_scenario(env.client, world, policy=gate_log(gates), budget=1.0)
    assert "budget" in gates
    assert state.budget_usd > 1.0
    assert state.status == "completed"
    assert any("Reply `/continue`" in body for _, body in world.posts)


async def test_stop_at_the_budget_fails_the_run(env):
    world = World(turn_cost=0.6)

    def policy(gate, n):
        return "/stop" if gate == "budget" else default_policy(gate, n)

    with pytest.raises(Exception):
        await run_scenario(env.client, world, policy=policy, budget=1.0)


async def test_supervised_autonomy_waits_for_merge_on_the_pr(env):
    gates: list[str] = []

    def policy(gate, n):
        return "/merge" if gate == "merge" else default_policy(gate, n)

    state = await run_scenario(env.client, World(), policy=gate_log(gates, policy), autonomy="supervised")
    assert gates == ["plan", "merge", "production"]
    assert "/merge" in step(state, "gate:merge").note


async def test_a_command_the_gate_isnt_waiting_for_is_noted_not_obeyed(env):
    world = World()

    def policy(gate, n):
        if gate == "plan":
            return ["/ship", "/approve"]
        return default_policy(gate, n)

    gates: list[str] = []
    state = await run_scenario(env.client, world, policy=gate_log(gates, policy))
    noted = state.conversation[0]
    assert (noted.kind, noted.outcome) == ("noted", "Noted; the run is waiting for /approve")
    assert world.handlers.count("planner:plan") == 1  # still the same plan; /approve came later
    assert state.status == "completed"


# -- Issue to production: people steering the agents ----------------------------------------


async def test_clarifying_questions_answered_on_the_issue_reach_the_plan(env):
    world = World(questions=["Case sensitive?"])
    gates: list[str] = []

    def policy(gate, n):
        return "No, ignore case" if gate == "clarify" else default_policy(gate, n)

    state = await run_scenario(env.client, world, policy=gate_log(gates, policy))
    assert gates[0] == "clarify"
    assert payload(world, "planner:plan")["clarifications"] == "No, ignore case"
    answer = state.conversation[0]
    assert (answer.author, answer.kind, answer.status) == ("maya", "answer", "addressed")
    assert any("Case sensitive?" in body for _, body in world.posts)


async def test_skip_lets_the_planner_decide(env):
    world = World(questions=["Case sensitive?"])

    def policy(gate, n):
        return "/skip" if gate == "clarify" else default_policy(gate, n)

    state = await run_scenario(env.client, world, policy=policy)
    assert payload(world, "planner:plan")["clarifications"] is None
    assert state.status == "completed"


async def test_a_plan_comment_on_the_issue_revises_the_plan(env):
    world = World()

    def policy(gate, n):
        if gate == "plan" and n == 1:
            return "Keep it to one subtask"
        return default_policy(gate, n)

    state = await run_scenario(env.client, world, policy=policy)
    assert world.handlers.count("planner:plan") == 2
    assert payload(world, "planner:plan", 1)["feedback"] == "Keep it to one subtask"
    assert state.conversation[0].kind == "plan_feedback"
    assert any(p.criterion == "D7" for p in state.proofs)


async def test_pr_feedback_is_addressed_and_re_reviewed(env):
    world = World()

    def policy(gate, n):
        if gate == "merge" and n == 1:
            return "The error message should say the code is invalid"
        return default_policy(gate, n)

    state = await run_scenario(env.client, world, policy=policy)
    assert state.status == "completed"
    assert "planner:triage" in world.handlers
    assert payload(world, "coder-integration:code")["subtask"]["id"] == "pr-feedback"
    assert world.handlers.count("reviewer-quality:review") == 2  # reviewed again after the fix
    assert any("Addressed:" in body for n, body in world.posts if n == 2)
    item = next(c for c in state.conversation if c.kind == "feedback")
    assert item.status == "addressed"
    assert world.checks == ["pending", "success", "pending", "success"]
    assert world.calls["merge"] == 1 and world.calls["close_pr"] == 0


async def test_a_scope_change_on_the_pr_goes_back_to_planning(env):
    world = World()
    gates: list[str] = []

    def policy(gate, n):
        if gate == "merge" and n == 1:
            return "Also support a FREESHIP code"
        return default_policy(gate, n)

    state = await run_scenario(env.client, world, policy=gate_log(gates, policy))
    assert state.status == "completed"
    assert world.calls["close_pr"] == 1 and world.calls["open_pr"] == 2
    assert state.plan_revision == 2
    assert gates == ["plan", "merge", "plan", "merge", "production"]  # the plan needed /approve again
    assert "FREESHIP" in payload(world, "planner:plan", 1)["feedback"]
    assert "coder-logic-r2:code" in world.handlers  # fresh agents on fresh branches
    assert next(c for c in state.conversation if c.kind == "scope_change")
    assert any(p.criterion == "D7" for p in state.proofs)


async def test_stop_at_production_keeps_production_untouched(env):
    world = World()

    def policy(gate, n):
        return "/stop Not before the holiday freeze" if gate == "production" else default_policy(gate, n)

    state = await run_scenario(env.client, world, policy=policy)
    assert state.status == "stopped"
    assert world.deploys == ["staging"]
    assert step(state, "canary").status == "skipped"
    assert any("Not before the holiday freeze" in body for _, body in world.posts)


async def test_a_comment_at_staging_is_adjusted_and_redeployed(env):
    world = World()
    gates: list[str] = []

    def policy(gate, n):
        if gate == "production" and n == 1:
            return "The discount banner is too small"
        return default_policy(gate, n)

    state = await run_scenario(env.client, world, policy=gate_log(gates, policy))
    assert state.status == "completed"
    assert world.deploys == ["staging", "staging", "canary", "promote"]
    assert payload(world, "coder-staging:code")["subtask"]["id"] == "staging-feedback"
    assert gates == ["plan", "merge", "production", "production"]  # asked for /ship again
    assert world.calls["open_pr"] == 1 and world.calls["merge"] == 1  # straight to staging, no new PR
    assert state.staging_sha == state.canary_sha == "adjusted1"  # the canary ships the adjusted commit
    item = next(c for c in state.conversation if c.kind == "feedback")
    assert item.stage == "production" and item.status == "addressed"
    assert step(state, "adjust").status == "completed"
    assert any("Adjusted:" in body for n, body in world.posts if n == ISSUE)


async def test_a_scope_change_at_staging_is_answered_not_built(env):
    world = World()
    gates: list[str] = []

    def policy(gate, n):
        if gate == "production" and n == 1:
            return "Also add a FREESHIP code"
        return default_policy(gate, n)

    state = await run_scenario(env.client, world, policy=gate_log(gates, policy))
    assert state.status == "completed"
    assert "coder-staging:code" not in world.handlers
    assert world.deploys == ["staging", "canary", "promote"]
    assert gates == ["plan", "merge", "production", "production"]
    assert any("Open an issue for it" in body for n, body in world.posts if n == ISSUE)
    assert next(c for c in state.conversation if c.kind == "scope_change").status == "addressed"
    assert step(state, "adjust").status == "skipped"


async def test_rollback_after_a_staging_adjustment_restores_the_run_base(env):
    world = World(canary_healthy=False)

    def policy(gate, n):
        if gate == "production" and n == 1:
            return "Make the error message friendlier"
        return default_policy(gate, n)

    state = await run_scenario(env.client, world, policy=policy)
    assert state.status == "rolled_back"
    assert world.deploys == ["staging", "staging", "canary", "rollback"]
    (revert,) = world.reverts
    assert revert.merge_sha == "adjusted1" and revert.restore_sha == "abc"  # undoes the merge and the adjustment


async def test_a_comment_on_the_issue_while_coders_work_steers_all_of_them(env):
    world = World(code_delay=1.5)
    state = await run_scenario(env.client, world, steer="Use a constant for the 10%")
    for coder in ("coder-logic:code", "coder-ui:code"):
        turns = [p for h, p in world.payloads if h == coder]
        assert len(turns) == 2
        assert "Use a constant for the 10%" in turns[1]["feedback"]
    steer = next(f for f in state.conversation if f.kind == "steer")
    assert steer.status == "addressed"
    assert any(p.criterion == "D7" for p in state.proofs)
    assert state.status == "completed"


# -- Dependency repair ----------------------------------------------------------------------


async def test_a_compatible_bump_gets_a_green_check_and_no_agents(env):
    world = World()
    state = await run_scenario(env.client, world, kind="dependency", pr_number=BUMP_PR)
    assert state.status == "completed"
    assert world.checks == ["pending", "success"]
    assert world.handlers == []
    assert world.pr_body == ""
    assert any("no code changes are needed" in body for n, body in world.posts if n == BUMP_PR)
    assert step(state, "diagnose").status == "skipped"


async def test_a_breaking_bump_is_diagnosed_fixed_explained_and_reviewed(env):
    world = World(failing_test_runs=1)
    gates: list[str] = []
    state = await run_scenario(env.client, world, kind="dependency", pr_number=BUMP_PR, policy=gate_log(gates))
    assert state.status == "completed"
    # Diagnose before touching anything, then fix on the bump branch, then a second model reviews.
    assert world.handlers == ["coder:diagnose", "coder:code", "reviewer-quality:review"]
    diagnose = payload(world, "coder:diagnose")
    assert (diagnose["package"], diagnose["from_version"], diagnose["to_version"]) == ("orbit-money", "1.0.0", "2.0.0")
    assert "roundCents" in diagnose["test_output"]
    assert payload(world, "coder:code")["subtask"]["description"] == "Use toCents in cart.js"  # the diagnosis's fix plan
    for section in ("## What changed in orbit-money 2.0.0", "## What broke", "## What we changed, and why",
                    "## How to verify", "Original description"):
        assert section in world.pr_body
    assert any("Why orbit-money 2.0.0 breaks the build" in body for n, body in world.posts if n == BUMP_PR)
    assert next(agent for agent, body in world.authors if "breaks the build" in body) == "Coding agent"
    assert world.checks == ["pending", "success"]
    assert state.diagnosis is not None
    assert gates == ["conversation"]  # then it listens to the PR until it closes


async def test_a_comment_on_the_repaired_bump_goes_to_a_coder_and_back_to_review(env):
    world = World(failing_test_runs=1)

    def policy(gate, n):
        return "Add a test for a zero total" if n == 1 else None

    state = await run_scenario(env.client, world, kind="dependency", pr_number=BUMP_PR, policy=policy)
    assert state.status == "completed"
    assert world.handlers.count("reviewer-quality:review") == 2
    assert payload(world, "coder:code", 1)["subtask"]["id"] == "pr-feedback"
    assert world.checks == ["pending", "success", "pending", "success"]


# -- PR review ------------------------------------------------------------------------------


async def test_a_pr_review_fails_the_check_then_fixes_on_request(env):
    world = World(review="request_changes")

    def policy(gate, n):
        return "Please fix the rounding" if n == 1 else None

    state = await run_scenario(env.client, world, kind="review", pr_number=PERSON_PR, policy=policy)
    assert state.status == "completed"
    assert world.handlers == ["reviewer-quality:review", "planner:triage", "coder:code", "reviewer-quality:review"]
    assert world.checks == ["pending", "failure", "pending", "success"]
    assert any("Addressed:" in body for n, body in world.posts if n == PERSON_PR)


async def test_an_out_of_scope_request_on_a_pr_is_answered_not_built(env):
    world = World()

    def policy(gate, n):
        return "Also add gift cards" if n == 1 else None

    state = await run_scenario(env.client, world, kind="review", pr_number=PERSON_PR, policy=policy)
    assert "coder:code" not in world.handlers
    assert any("outside this PR's scope" in body for _, body in world.posts)
    assert state.conversation[0].kind == "scope_change"


async def test_a_quiet_pr_is_closed_after_the_idle_period(env):
    world = World()
    state = await run_scenario(env.client, world, kind="review", pr_number=PERSON_PR, idle_close_hours=1 / 3600,
                               close_pr_when_quiet=False)
    assert state.status == "completed"
    assert world.calls["close_pr"] == 1


# -- Environments ---------------------------------------------------------------------------


async def test_overlapping_canaries_promote_their_own_commits(env):
    """Two runs in one copy: the second canary takes the slot before the first promotes."""
    from agentic_sdlc.pipeline.environments import DeployRequest, Environments

    task_queue = f"test-{uuid.uuid4().hex[:6]}"
    async with Worker(env.client, task_queue=task_queue, workflows=[Environments], workflow_runner=workflow_runner()):
        handle = await env.client.start_workflow(Environments.run, None, id=f"env-{uuid.uuid4().hex[:6]}", task_queue=task_queue)
        for action, sha, run in [("canary", "aaa", "r1"), ("canary", "bbb", "r2"), ("promote", "aaa", "r1")]:
            state = await handle.execute_update(Environments.deploy, DeployRequest(action=action, sha=sha, run_id=run))
        assert (state.stable_sha, state.canary_sha) == ("aaa", "bbb")
        state = await handle.execute_update(Environments.deploy, DeployRequest(action="promote", sha="bbb", run_id="r2"))
        assert (state.stable_sha, state.canary_sha) == ("bbb", None)
        # A late rollback for a canary that's gone changes nothing.
        state = await handle.execute_update(Environments.deploy, DeployRequest(action="rollback", sha="aaa", run_id="r1"))
        assert state.stable_sha == "bbb"
        await handle.terminate()
