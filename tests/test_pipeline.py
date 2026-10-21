"""Pipeline Workflow tests against a local Temporal dev server.

The agents and the outside world are replaced: fake agent Workflows stand in for the harness
agents, and Activities with the real names return canned answers. What's under test is the
orchestration: gates, the parallel build, the test-fix loop, the budget guard, the canary
decision and the rollback saga.
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
    CommentInput,
    DiffInput,
    MergeInput,
    MergePrInput,
    MergeResult,
    PrepareRunInput,
    PullRequest,
    PullRequestInput,
    RevertInput,
    ReviewInput,
    RunRepo,
)
from agentic_sdlc.models import Plan, RunRequest, RunState, Subtask, Usage, Workspace
from agentic_sdlc.pipeline.environments import EnvState
from agentic_sdlc.pipeline.workflow import GateResponse, IssueToProduction
from agentic_sdlc.sandbox import workflow_runner

PLAN = Plan(
    summary="Add a discount code.",
    acceptance_criteria=["ORBIT10 takes 10% off"],
    subtasks=[Subtask(id="logic", title="Discount logic", description="cart.js"),
              Subtask(id="ui", title="Discount field", description="app.js")],
)


class World:
    """What the fake outside world does, and what it was asked to do."""

    def __init__(self, *, canary_healthy=True, failing_test_runs=0, review="approve", turn_cost=0.10):
        self.canary_healthy = canary_healthy
        self.failing_test_runs = failing_test_runs
        self.review = review
        self.turn_cost = turn_cost
        self.calls: Counter[str] = Counter()
        self.deploys: list[str] = []
        self.handlers: list[str] = []

    def activities(self):
        world = self

        @activity.defn(name="prepare_run")
        async def prepare_run(_: PrepareRunInput) -> RunRepo:
            world.calls["prepare_run"] += 1
            return RunRepo(repo="o/r", issue_number=1, issue_url="https://x/issues/1", base_branch="runs/x/main", base_sha="abc")

        @activity.defn(name="create_branches")
        async def create_branches(_: BranchesInput) -> None:
            world.calls["create_branches"] += 1

        @activity.defn(name="comment_on_issue")
        async def comment_on_issue(input: CommentInput) -> None:
            world.calls["comment"] += 1
            if input.reopen:
                world.calls["reopen"] += 1

        @activity.defn(name="merge_branches")
        async def merge_branches(_: MergeInput) -> MergeResult:
            return MergeResult(sha="def", conflicts=[])

        @activity.defn(name="open_pull_request")
        async def open_pull_request(_: PullRequestInput) -> PullRequest:
            return PullRequest(number=2, url="https://x/pull/2", head_sha="def")

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
        async def revert_merge(_: RevertInput) -> PullRequest:
            world.calls["revert"] += 1
            return PullRequest(number=3, url="https://x/pull/3", head_sha="rev")

        @activity.defn(name="run_agent_turn")
        async def run_agent_turn(turn: AgentTurn) -> AgentTurnResult:
            world.handlers.append(f"{turn.workflow_id.rsplit('.', 1)[-1]}:{turn.handler}")
            usage = Usage(model="anthropic:claude-sonnet-5-5", requests=2, input_tokens=100, output_tokens=50,
                          cost_usd=world.turn_cost).model_dump()
            if turn.handler == "plan":
                output = {"plan": PLAN.model_dump(), "usage": usage}
            elif turn.handler == "code":
                output = {"summary": "done", "tests_passed": True, "usage": usage}
            else:
                verdict, world.review = world.review, "approve"  # request changes at most once
                output = {"review": {"verdict": verdict, "summary": "looks fine", "comments": []}, "usage": usage}
            return AgentTurnResult(output=output, cost_usd=world.turn_cost, attempt=1, stats=TurnStats(model_calls=2))

        @activity.defn(name="run_branch_tests")
        async def run_branch_tests(_: Workspace) -> TestRun:
            if world.failing_test_runs > 0:
                world.failing_test_runs -= 1
                return TestRun(passed=False, output="1 failing")
            return TestRun(passed=True, output="ok")

        @activity.defn(name="apply_deployment")
        async def apply_deployment(call: DeployCall) -> EnvState:
            world.deploys.append(call.request.action)
            return EnvState()

        @activity.defn(name="check_environment")
        async def check_environment(input: CheckInput) -> CheckResult:
            healthy = input.env == "staging" or world.canary_healthy
            return CheckResult(healthy=healthy, probes=10, failed_probes=0 if healthy else 6, detail="checked")

        return [prepare_run, create_branches, comment_on_issue, merge_branches, open_pull_request, get_diff,
                post_review, merge_pull_request, revert_merge, run_agent_turn, run_branch_tests, apply_deployment,
                check_environment]


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


@pytest.fixture
async def env():
    # A real dev server: the time-skipping test server doesn't support Workflow Updates well.
    async with await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter) as env:
        yield env


async def run_pipeline(client: Client, world: World, *, decide, autonomy="auto", budget=5.0) -> RunState:
    task_queue = f"test-{uuid.uuid4().hex[:6]}"
    async with Worker(client, task_queue=task_queue, workflows=[IssueToProduction, *FAKE_AGENTS],
                      activities=world.activities(), workflow_runner=workflow_runner()):
        handle = await client.start_workflow(
            IssueToProduction.run,
            RunRequest(run_id="t1", owner="o", issue_title="Discount", issue_body="ORBIT10", autonomy=autonomy,
                       budget_usd=budget, canary_bake_seconds=2, canary_check_seconds=1),
            id=f"run-{uuid.uuid4().hex[:6]}", task_queue=task_queue,
        )
        answering = asyncio.create_task(_answer_gates(handle, decide))
        try:
            return await handle.result()
        finally:
            answering.cancel()


async def _answer_gates(handle: WorkflowHandle, decide) -> None:
    answered: set[tuple[str, float]] = set()
    while True:
        await asyncio.sleep(0.2)
        state = await handle.query(IssueToProduction.state)
        gate = state.gate
        if gate and (gate.key, gate.waiting_since) not in answered:
            answered.add((gate.key, gate.waiting_since))
            await handle.execute_update(IssueToProduction.decide, GateResponse(gate=gate.key, decision=decide(gate.key)))


def approve_all(_gate):
    from agentic_sdlc.models import GateDecision

    return GateDecision(approve=True, raise_budget_usd=5)


async def test_happy_path_ships_through_plan_and_production_gates(env):
    world = World()
    state = await run_pipeline(env.client, world, decide=approve_all)
    assert state.status == "completed"
    assert world.deploys == ["staging", "canary", "promote"]
    assert world.calls["revert"] == 0
    # Two coders in parallel, one per subtask, plus the planner and the reviewer.
    assert sorted(world.handlers) == sorted(["planner:plan", "coder-logic:code", "coder-ui:code", "reviewer-quality:review"])
    assert {p.criterion for p in state.proofs} >= {"D2", "D3", "D6"}
    assert next(s for s in state.steps if s.key == "gate:merge").status == "skipped"  # auto merge


async def test_rejected_plan_goes_back_to_the_planner(env):
    from agentic_sdlc.models import GateDecision

    seen: list[str] = []

    def decide(gate):
        seen.append(gate)
        return GateDecision(approve=not (gate == "plan" and seen.count("plan") == 1), feedback="smaller")

    world = World()
    state = await run_pipeline(env.client, world, decide=decide)
    assert state.status == "completed"
    assert world.handlers.count("planner:plan") == 2


async def test_unhealthy_canary_runs_the_rollback_saga(env):
    world = World(canary_healthy=False)
    state = await run_pipeline(env.client, world, decide=approve_all)
    assert state.status == "rolled_back"
    assert world.deploys == ["staging", "canary", "rollback"]
    assert world.calls["revert"] == 1 and world.calls["reopen"] == 1
    assert any(p.criterion == "D4" for p in state.proofs)
    assert next(s for s in state.steps if s.key == "canary").status == "failed"


async def test_failing_branch_tests_go_back_to_the_coder(env):
    world = World(failing_test_runs=1)
    state = await run_pipeline(env.client, world, decide=approve_all)
    assert state.status == "completed"
    assert sum(1 for h in world.handlers if h.endswith(":code")) == 3  # two coders, one fix turn


async def test_budget_guard_pauses_until_a_person_raises_it(env):
    gates: list[str] = []

    def decide(gate):
        gates.append(gate)
        return approve_all(gate)

    world = World(turn_cost=0.6)
    state = await run_pipeline(env.client, world, decide=decide, budget=1.0)
    assert "budget" in gates
    assert state.budget_usd > 1.0
    assert state.status == "completed"


async def test_supervised_autonomy_adds_a_merge_gate(env):
    gates: list[str] = []

    def decide(gate):
        gates.append(gate)
        return approve_all(gate)

    await run_pipeline(env.client, World(), decide=decide, autonomy="supervised")
    assert gates == ["plan", "merge", "production"]
