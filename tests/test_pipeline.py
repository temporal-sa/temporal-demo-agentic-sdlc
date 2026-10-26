"""Pipeline Workflow tests against a local Temporal dev server.

The agents and the outside world are replaced: fake agent Workflows stand in for the harness
agents, and Activities with the real names return canned answers. What's under test is the
orchestration: gates and the conversation with people, the parallel build, steering, the
test-fix loop, the budget guard, scope changes, the canary decision and the rollback saga.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter

import pytest
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentic_sdlc.activities.agent_turn import AgentTurn, AgentTurnResult, TurnStats
from agentic_sdlc.activities.checks import TestRun
from agentic_sdlc.activities.deploy import CheckInput, CheckResult, DeployCall
from agentic_sdlc.activities.github import (
    BranchesInput,
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
    PullRequestInput,
    RevertInput,
    ReviewInput,
    RunRepo,
)
from agentic_sdlc.models import GateDecision, Plan, RunRequest, RunState, Subtask, Usage, Workspace
from agentic_sdlc.pipeline.environments import EnvState
from agentic_sdlc.pipeline.workflow import CommentRequest, GateResponse, IssueToProduction, SteerRequest
from agentic_sdlc.sandbox import workflow_runner

PLAN = Plan(
    summary="Add a discount code.",
    acceptance_criteria=["ORBIT10 takes 10% off"],
    subtasks=[Subtask(id="logic", title="Discount logic", description="cart.js"),
              Subtask(id="ui", title="Discount field", description="app.js")],
)


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
        self.github: list[HumanComment] = []  # comments people have left, returned by fetch_comments
        self.pr_comments: list[str] = []

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
            if input.issue_number != 1:
                world.pr_comments.append(input.body)
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

        @activity.defn(name="close_pull_request")
        async def close_pull_request(_: ClosePrInput) -> None:
            world.calls["close_pr"] += 1

        @activity.defn(name="fetch_comments")
        async def fetch_comments(_: CommentsInput) -> list[HumanComment]:
            world.calls["fetch_comments"] += 1
            return list(world.github)

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

        return [prepare_run, create_branches, comment_on_issue, merge_branches, open_pull_request, close_pull_request,
                fetch_comments, get_diff, post_review, merge_pull_request, revert_merge, run_agent_turn,
                run_branch_tests, apply_deployment, check_environment]


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


# What a person does at a gate: ("approve", text), ("reject", text), ("console", text),
# ("github", text) or ("wait", None) to say nothing and let a review window close.
def default_policy(gate: str, occurrence: int):
    return ("wait", None) if gate == "merge" else ("approve", None)


async def run_pipeline(client: Client, world: World, *, policy=default_policy, autonomy="auto", budget=5.0,
                       steer: tuple[str, str] | None = None) -> RunState:
    task_queue = f"test-{uuid.uuid4().hex[:6]}"
    async with Worker(client, task_queue=task_queue, workflows=[IssueToProduction, *FAKE_AGENTS],
                      activities=world.activities(), workflow_runner=workflow_runner()):
        handle = await client.start_workflow(
            IssueToProduction.run,
            RunRequest(run_id="t1", owner="o", issue_title="Discount", issue_body="ORBIT10", autonomy=autonomy,
                       budget_usd=budget, canary_bake_seconds=2, canary_check_seconds=1,
                       review_window_seconds=2, github_poll_seconds=1),
            id=f"run-{uuid.uuid4().hex[:6]}", task_queue=task_queue,
        )
        driving = asyncio.create_task(_act(handle, world, policy, steer))
        try:
            return await handle.result()
        finally:
            driving.cancel()


async def _act(handle: WorkflowHandle, world: World, policy, steer) -> None:
    answered: set[tuple[str, float]] = set()
    occurrences: Counter[str] = Counter()
    steered = steer is None
    while True:
        await asyncio.sleep(0.2)
        state = await handle.query(IssueToProduction.state)
        if not steered and any(s.key == steer[0] and s.status == "active" for s in state.steps):
            await handle.execute_update(IssueToProduction.steer, SteerRequest(lane=steer[0], text=steer[1]))
            steered = True
        gate = state.gate
        if not gate or (gate.key, gate.waiting_since) in answered:
            continue
        answered.add((gate.key, gate.waiting_since))
        occurrences[gate.key] += 1
        action, text = policy(gate.key, occurrences[gate.key])
        if action in ("approve", "reject"):
            decision = GateDecision(approve=action == "approve", feedback=text, raise_budget_usd=5)
            await handle.execute_update(IssueToProduction.decide, GateResponse(gate=gate.key, decision=decision))
        elif action == "console":
            await handle.execute_update(IssueToProduction.comment, CommentRequest(text=text))
        elif action == "github":
            n = len(world.github) + 1
            world.github.append(HumanComment(id=f"issue:{n}", number=1, author="maya", body=text,
                                             url=f"https://x/c/{n}", created_at="2099-01-01T00:00:00Z"))


def gate_log(log: list[str], inner=default_policy):
    def policy(gate, occurrence):
        log.append(gate)
        return inner(gate, occurrence)
    return policy


def payload(world: World, handler: str, nth: int = 0) -> dict:
    return [p for h, p in world.payloads if h == handler][nth]


# -- The lifecycle ------------------------------------------------------------------------


async def test_happy_path_ships_through_plan_and_production_gates(env):
    world = World()
    gates: list[str] = []
    state = await run_pipeline(env.client, world, policy=gate_log(gates))
    assert state.status == "completed"
    assert world.deploys == ["staging", "canary", "promote"]
    assert world.calls["revert"] == 0
    # The planner checks for questions, then two coders work in parallel, then the reviewer.
    assert sorted(world.handlers) == sorted(
        ["planner:clarify", "planner:plan", "coder-logic:code", "coder-ui:code", "reviewer-quality:review"])
    assert gates == ["plan", "merge", "production"]
    assert next(s for s in state.steps if s.key == "gate:clarify").status == "skipped"  # no questions
    assert "Nobody commented" in next(s for s in state.steps if s.key == "gate:merge").note  # window closed
    assert {p.criterion for p in state.proofs} >= {"D2", "D3", "D6"}


async def test_unhealthy_canary_runs_the_rollback_saga(env):
    world = World(canary_healthy=False)
    state = await run_pipeline(env.client, world)
    assert state.status == "rolled_back"
    assert world.deploys == ["staging", "canary", "rollback"]
    assert world.calls["revert"] == 1 and world.calls["reopen"] == 1
    assert any(p.criterion == "D4" for p in state.proofs)
    assert next(s for s in state.steps if s.key == "canary").status == "failed"


async def test_failing_branch_tests_go_back_to_the_coder(env):
    world = World(failing_test_runs=1)
    state = await run_pipeline(env.client, world)
    assert state.status == "completed"
    assert sum(1 for h in world.handlers if h.endswith(":code")) == 3  # two coders, one fix turn


async def test_budget_guard_pauses_until_a_person_raises_it(env):
    gates: list[str] = []
    world = World(turn_cost=0.6)
    state = await run_pipeline(env.client, world, policy=gate_log(gates), budget=1.0)
    assert "budget" in gates
    assert state.budget_usd > 1.0
    assert state.status == "completed"


async def test_supervised_autonomy_waits_for_merge_approval(env):
    gates: list[str] = []
    approve = lambda gate, n: ("approve", None)  # noqa: E731
    await run_pipeline(env.client, World(), policy=gate_log(gates, approve), autonomy="supervised")
    assert gates == ["plan", "merge", "production"]


# -- People steering the agents -------------------------------------------------------------


async def test_clarifying_questions_answered_in_the_console_reach_the_plan(env):
    world = World(questions=["Where does the code field go?"])
    gates: list[str] = []

    def policy(gate, n):
        return ("approve", "Under the cart totals") if gate == "clarify" else default_policy(gate, n)

    state = await run_pipeline(env.client, world, policy=gate_log(gates, policy))
    assert gates[0] == "clarify"
    assert payload(world, "planner:plan")["clarifications"] == "Under the cart totals"
    assert state.conversation[0].kind == "answer" and state.conversation[0].source == "console"
    assert state.status == "completed"


async def test_clarifying_questions_answered_on_the_issue_reach_the_plan(env):
    world = World(questions=["Case sensitive?"])

    def policy(gate, n):
        return ("github", "No, ignore case") if gate == "clarify" else default_policy(gate, n)

    state = await run_pipeline(env.client, world, policy=policy)
    assert payload(world, "planner:plan")["clarifications"] == "No, ignore case"
    answer = state.conversation[0]
    assert (answer.source, answer.author, answer.kind, answer.status) == ("github", "maya", "answer", "addressed")


async def test_plan_comment_on_the_issue_revises_the_plan(env):
    world = World()

    def policy(gate, n):
        if gate == "plan" and n == 1:
            return ("github", "Keep it to one subtask")
        return default_policy(gate, n)

    state = await run_pipeline(env.client, world, policy=policy)
    assert world.handlers.count("planner:plan") == 2
    assert payload(world, "planner:plan", 1)["feedback"] == "Keep it to one subtask"
    assert any(p.criterion == "D7" for p in state.proofs)


async def test_rejected_plan_goes_back_to_the_planner(env):
    world = World()

    def policy(gate, n):
        return ("reject", "smaller") if gate == "plan" and n == 1 else default_policy(gate, n)

    state = await run_pipeline(env.client, world, policy=policy)
    assert state.status == "completed"
    assert world.handlers.count("planner:plan") == 2


async def test_pr_feedback_on_github_is_addressed_and_re_reviewed(env):
    world = World()

    def policy(gate, n):
        if gate == "merge" and n == 1:
            return ("github", "The error message should say the code is invalid")
        return default_policy(gate, n)

    state = await run_pipeline(env.client, world, policy=policy)
    assert state.status == "completed"
    assert "planner:triage" in world.handlers
    assert payload(world, "coder-integration:code")["subtask"]["id"] == "pr-feedback"
    assert world.handlers.count("reviewer-quality:review") == 2  # reviewed again after the fix
    assert any("Addressed by the coding agent" in body for body in world.pr_comments)
    item = state.conversation[0]
    assert (item.kind, item.status, item.source) == ("feedback", "addressed", "github")
    assert world.calls["merge"] == 1 and world.calls["close_pr"] == 0
    assert any(p.criterion == "D7" for p in state.proofs)


async def test_scope_change_on_the_pr_goes_back_to_planning(env):
    world = World()
    gates: list[str] = []

    def policy(gate, n):
        if gate == "merge" and n == 1:
            return ("console", "Also support a FREESHIP code")
        return default_policy(gate, n)

    state = await run_pipeline(env.client, world, policy=gate_log(gates, policy))
    assert state.status == "completed"
    assert world.calls["close_pr"] == 1 and world.calls["open_pr"] == 2
    assert state.plan_revision == 2
    assert gates == ["plan", "merge", "plan", "merge", "production"]  # the plan needed approval again
    assert "FREESHIP" in payload(world, "planner:plan", 1)["feedback"]
    assert "coder-logic-r2:code" in world.handlers  # fresh agents on fresh branches
    assert state.conversation[0].kind == "scope_change"
    assert any(p.criterion == "D7" for p in state.proofs)


async def test_merge_rejection_needs_a_reason_and_becomes_feedback(env):
    world = World()
    refused: list[str] = []

    def policy(gate, n):
        if gate == "merge" and n == 1:
            return ("reject", "Rename the button to Apply code")
        if gate == "merge":
            return ("approve", None)
        return default_policy(gate, n)

    task_queue = f"test-{uuid.uuid4().hex[:6]}"
    async with Worker(env.client, task_queue=task_queue, workflows=[IssueToProduction, *FAKE_AGENTS],
                      activities=world.activities(), workflow_runner=workflow_runner()):
        handle = await env.client.start_workflow(
            IssueToProduction.run,
            RunRequest(run_id="t1", owner="o", issue_title="D", issue_body="x", autonomy="supervised",
                       github_poll_seconds=1, canary_bake_seconds=1, canary_check_seconds=1),
            id=f"run-{uuid.uuid4().hex[:6]}", task_queue=task_queue)
        while (await handle.query(IssueToProduction.state)).gate is None or \
                (await handle.query(IssueToProduction.state)).gate.key != "merge":
            if (g := (await handle.query(IssueToProduction.state)).gate) is not None and g.key == "plan":
                await handle.execute_update(IssueToProduction.decide, GateResponse(gate="plan", decision=GateDecision(approve=True)))
            await asyncio.sleep(0.2)
        try:
            await handle.execute_update(IssueToProduction.decide, GateResponse(gate="merge", decision=GateDecision(approve=False)))
        except WorkflowUpdateFailedError as err:
            refused.append(err.cause.message)
        driving = asyncio.create_task(_act(handle, world, policy, None))
        try:
            state = await handle.result()
        finally:
            driving.cancel()
    assert refused and "Say what should change" in refused[0]
    assert payload(world, "coder-integration:code")["subtask"]["description"].endswith("Rename the button to Apply code")
    assert state.status == "completed"


async def test_rejecting_production_stops_without_touching_production(env):
    world = World()

    def policy(gate, n):
        return ("reject", "Not before the holiday freeze") if gate == "production" else default_policy(gate, n)

    state = await run_pipeline(env.client, world, policy=policy)
    assert state.status == "stopped"
    assert world.deploys == ["staging"]
    assert next(s for s in state.steps if s.key == "canary").status == "skipped"


async def test_steering_a_coder_mid_task_adds_a_turn_without_restarting(env):
    world = World(code_delay=1.5)
    state = await run_pipeline(env.client, world, steer=("code:logic", "Use a constant for the 10%"))
    turns = [p for h, p in world.payloads if h == "coder-logic:code"]
    assert len(turns) == 2
    assert "Use a constant for the 10%" in turns[1]["feedback"]
    steer = next(f for f in state.conversation if f.kind == "steer")
    assert steer.status == "addressed"
    assert any(p.criterion == "D7" for p in state.proofs)
    assert state.status == "completed"


async def test_comments_are_refused_when_no_one_is_listening(env):
    world = World(code_delay=1.5)
    task_queue = f"test-{uuid.uuid4().hex[:6]}"
    async with Worker(env.client, task_queue=task_queue, workflows=[IssueToProduction, *FAKE_AGENTS],
                      activities=world.activities(), workflow_runner=workflow_runner()):
        handle = await env.client.start_workflow(
            IssueToProduction.run, RunRequest(run_id="t1", owner="o", issue_title="D", issue_body="x"),
            id=f"run-{uuid.uuid4().hex[:6]}", task_queue=task_queue)
        await asyncio.sleep(0.5)
        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update(IssueToProduction.steer, SteerRequest(lane="code:nope", text="hi"))
        await handle.terminate()


async def test_overlapping_canaries_promote_their_own_commits(env):
    """Two runs in one session: the second canary takes the slot before the first promotes."""
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
