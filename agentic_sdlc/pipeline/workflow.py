"""IssueToProduction: the agentic software delivery pipeline, as one Temporal Workflow.

Agents decide what to do inside a step. This Workflow decides the process: which agent works
when, what a human must approve, what to retry, when to stop spending, and how to undo a bad
release. Read ``run`` top to bottom; it is the whole lifecycle.

Every agent is a child Workflow (agents/workflows.py) whose model calls and tool calls are
Activities, so a crashed worker, a rate-limited provider or a two-hour wait for a person
loses no work. The UI reads this Workflow's progress from its memo with Describe.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.agent_protocol import AgentConfig

    from agentic_sdlc import config
    from agentic_sdlc.activities.agent_turn import AgentTurn, AgentTurnActivities, AgentTurnResult
    from agentic_sdlc.activities.deploy import CheckInput, CheckResult, DeployActivities, DeployCall
    from agentic_sdlc.activities.github import (
        BranchesInput,
        CommentInput,
        DiffInput,
        GitHubActivities,
        MergeInput,
        MergePrInput,
        PrepareRunInput,
        PullRequest,
        PullRequestInput,
        RevertInput,
        ReviewInput,
        RunRepo,
    )
    from agentic_sdlc.activities.checks import CheckActivities, TestRun
    from agentic_sdlc.models import (
        AgentInit,
        AgentRef,
        CodeReply,
        CodeRequest,
        Gate,
        GateDecision,
        ModelChoice,
        Plan,
        PlanReply,
        PlanRequest,
        Proof,
        ReviewReply,
        ReviewRequest,
        RunRequest,
        RunState,
        Step,
        Subtask,
        Usage,
        Workspace,
    )
    from agentic_sdlc.pipeline.environments import DeployRequest

# Each Worker Deployment Version starts with one PIPELINE_VERSION and never changes it, so a
# Pinned run always replays against the code path it started on. v2 adds a security-review
# agent. In a real project v2 would simply be the next commit; see README "Notes".
SECURITY_REVIEW = config.PIPELINE_VERSION == "v2"

# GitHub calls retry until GitHub recovers; a rejected request (bad input) does not.
GITHUB = dict(
    start_to_close_timeout=timedelta(minutes=2),
    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=10,
                             non_retryable_error_types=["GitHubRejected"]),
)
# An agent turn can take minutes. The heartbeat notices a dead worker in 15 seconds; the retry
# re-attaches to the same turn, which the agent kept running.
AGENT_TURN = dict(
    start_to_close_timeout=timedelta(minutes=30),
    heartbeat_timeout=timedelta(seconds=15),
    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5,
                             non_retryable_error_types=["AgentTurnFailed"]),
)
QUICK = dict(start_to_close_timeout=timedelta(minutes=3), retry_policy=RetryPolicy(maximum_attempts=5))

MAX_PLAN_ROUNDS = 3
MAX_TEST_FIX_ROUNDS = 2
CANARY_WEIGHT = 20
CANARY_CHECK_EVERY = timedelta(seconds=15)


class GateResponse(BaseModel):
    """A person's answer to the gate the run is waiting at."""

    gate: str
    decision: GateDecision


@workflow.defn
class IssueToProduction:
    @workflow.init
    def __init__(self, request: RunRequest) -> None:
        self._request = request
        info = workflow.info()
        self._id = info.workflow_id
        self._branches = f"runs/{info.task_queue}/{request.owner}/{request.run_id}"
        self._environments_id = f"{info.task_queue}.{request.owner}.environments"
        self._decision: GateDecision | None = None
        self._turns: dict[str, int] = {}
        self._state = RunState(
            run_id=request.run_id,
            version=config.PIPELINE_VERSION,
            title=request.issue_title,
            budget_usd=request.budget_usd,
            started_at=workflow.now().timestamp(),
            steps=_initial_steps(),
        )

    @workflow.run
    async def run(self, request: RunRequest) -> RunState:
        self._publish()
        try:
            repo = await self._open_issue()
            plan = await self._plan(repo)
            branches = await self._build(repo, plan)
            pr = await self._open_pull_request(repo, plan, branches)
            await self._review(repo, plan, pr)
            merge_sha = await self._merge(repo, pr)
            await self._deploy_to_staging(merge_sha)
            await self._gate("production", "Ship this change to production?",
                             f"Staging passed its checks. The canary starts at {CANARY_WEIGHT}% of traffic.")
            shipped = await self._canary(repo, merge_sha)
            await self._finish(repo, shipped)
        except ApplicationError as err:
            self._state.status = "failed"
            self._note_failure(str(err))
            raise
        finally:
            self._state.ended_at = workflow.now().timestamp()
            for step in self._state.steps:
                if step.status == "pending":
                    step.status = "skipped"
            self._publish()
            await self._close_agents()
        return self._state

    # -- Steps -------------------------------------------------------------------------------

    async def _open_issue(self) -> RunRepo:
        async with self._step("issue"):
            repo = await workflow.execute_activity_method(
                GitHubActivities.prepare_run,
                PrepareRunInput(run_id=self._request.run_id, branch_prefix=self._branches,
                                title=self._request.issue_title, body=self._request.issue_body),
                **GITHUB,
            )
            self._state.issue_url = repo.issue_url
            self._detail("issue", f"Issue #{repo.issue_number}")
            return repo

    async def _plan(self, repo: RunRepo) -> Plan:
        planner = await self._start_agent("planner", self._request.models.planner,
                                          Workspace(repo=repo.repo, branch=repo.base_branch))
        feedback: str | None = None
        for _round in range(MAX_PLAN_ROUNDS):
            async with self._step("plan"):
                reply = await self._turn(planner, "plan", PlanRequest(
                    issue_title=self._request.issue_title, issue_body=self._request.issue_body, feedback=feedback,
                ), PlanReply)
                plan = reply.plan
                plan.subtasks = plan.subtasks[:3]
                self._state.plan = plan
                self._detail("plan", f"{len(plan.subtasks)} subtask(s): " + ", ".join(s.title for s in plan.subtasks))
                await workflow.execute_activity_method(GitHubActivities.comment_on_issue, CommentInput(
                    repo=repo.repo, issue_number=repo.issue_number, body=_plan_comment(plan), labels=["agent-plan"],
                ), **GITHUB)
            decision = await self._gate("plan", "Approve this plan before any code is written?", plan.summary)
            if decision.approve:
                return plan
            feedback = decision.feedback or "Make the plan smaller and more specific."
        raise ApplicationError("The plan was rejected too many times.", non_retryable=True)

    async def _build(self, repo: RunRepo, plan: Plan) -> list[str]:
        """One coding agent per subtask, each on its own branch, all at once."""
        async with self._step("build"):
            branches = [f"{self._branches}/{s.id}" for s in plan.subtasks]
            await workflow.execute_activity_method(GitHubActivities.create_branches, BranchesInput(
                repo=repo.repo, from_sha=repo.base_sha, branches=branches,
            ), **GITHUB)
            self._state.steps.extend(Step(key=f"code:{s.id}", label=s.title, lane=s.id) for s in plan.subtasks)
            await asyncio.gather(*(
                self._code_subtask(repo, plan, subtask, branch) for subtask, branch in zip(plan.subtasks, branches)
            ))
            if len(plan.subtasks) > 1:
                self._prove("D3", f"{len(plan.subtasks)} coding agents worked in parallel on separate branches")
            return branches

    async def _code_subtask(self, repo: RunRepo, plan: Plan, subtask: Subtask, branch: str) -> None:
        lane = f"code:{subtask.id}"
        workspace = Workspace(repo=repo.repo, branch=branch)
        coder = await self._start_agent(f"coder-{subtask.id}", self._request.models.coder, workspace, lane=subtask.id)
        async with self._step(lane):
            reply = await self._turn(coder, "code", CodeRequest(
                issue_title=self._request.issue_title, plan_summary=plan.summary, subtask=subtask,
            ), CodeReply, lane=lane)
            # Don't take the agent's word for it: run the tests on the pushed branch.
            for _round in range(MAX_TEST_FIX_ROUNDS):
                tests = await workflow.execute_activity_method(CheckActivities.run_branch_tests, workspace, **QUICK)
                if tests.passed:
                    break
                self._detail(lane, "Tests failed on the branch; sending the failures back to the agent")
                reply = await self._turn(coder, "code", CodeRequest(
                    issue_title=self._request.issue_title, plan_summary=plan.summary, subtask=subtask,
                    feedback=f"The test suite fails on your branch:\n{tests.output[-4000:]}",
                ), CodeReply, lane=lane)
            else:
                raise ApplicationError(f"Subtask {subtask.id} still fails its tests.", non_retryable=True)
            self._detail(lane, reply.summary)

    async def _open_pull_request(self, repo: RunRepo, plan: Plan, branches: list[str]) -> PullRequest:
        async with self._step("pr"):
            integration = f"{self._branches}/integration"
            merged = await workflow.execute_activity_method(GitHubActivities.merge_branches, MergeInput(
                repo=repo.repo, base_sha=repo.base_sha, integration_branch=integration, branches=branches,
            ), **GITHUB)
            for branch in merged.conflicts:
                await self._resolve_conflict(repo, plan, integration, branch)
            pr = await workflow.execute_activity_method(GitHubActivities.open_pull_request, PullRequestInput(
                repo=repo.repo, head=integration, base=repo.base_branch,
                title=self._request.issue_title, body=_pr_body(plan, repo),
            ), **GITHUB)
            self._state.pr_url = pr.url
            self._detail("pr", f"PR #{pr.number}")
            return pr

    async def _resolve_conflict(self, repo: RunRepo, plan: Plan, integration: str, branch: str) -> None:
        """Two agents touched the same lines. A fresh coder re-applies one branch's change on
        top of the other's, on the integration branch."""
        diff = await workflow.execute_activity_method(
            GitHubActivities.get_diff, DiffInput(repo=repo.repo, base=repo.base_sha, head=branch), **GITHUB)
        resolver = await self._integration_coder(repo, integration)
        self._detail("pr", f"Merge conflict on {branch.rsplit('/', 1)[-1]}; an agent is re-applying it")
        await self._turn(resolver, "code", CodeRequest(
            issue_title=self._request.issue_title, plan_summary=plan.summary,
            subtask=Subtask(id="merge", title="Re-apply a conflicting change",
                            description=f"Apply this change on top of the current code, keeping both:\n```diff\n{diff}\n```"),
        ), CodeReply, lane="pr")

    async def _review(self, repo: RunRepo, plan: Plan, pr: PullRequest) -> None:
        reviews = [("review", "quality", "Code review agent")]
        if SECURITY_REVIEW:
            reviews.append(("security", "security", "Security review agent"))
        for step, focus, reviewer_name in reviews:
            async with self._step(step):
                reviewer = await self._start_agent(f"reviewer-{focus}", self._request.models.reviewer,
                                                   Workspace(repo=repo.repo, branch=f"{self._branches}/integration"))
                diff = await workflow.execute_activity_method(GitHubActivities.get_diff, DiffInput(
                    repo=repo.repo, base=repo.base_branch, head=f"{self._branches}/integration"), **GITHUB)
                result = await self._turn(reviewer, "review", ReviewRequest(
                    issue_title=self._request.issue_title, plan=plan, diff=diff, focus=focus), ReviewReply)
                self._state.review = result.review
                await workflow.execute_activity_method(GitHubActivities.post_review, ReviewInput(
                    repo=repo.repo, pr_number=pr.number, reviewer=reviewer_name, review=result.review), **GITHUB)
                if result.review.verdict == "request_changes":
                    # One round of fixes, made on the PR branch by a coder who sees the comments.
                    self._detail(step, f"Changes requested: {result.review.summary}")
                    fixer = await self._integration_coder(repo, f"{self._branches}/integration")
                    comments = "\n".join(f"- {c.file}: {c.comment}" for c in result.review.comments)
                    await self._turn(fixer, "code", CodeRequest(
                        issue_title=self._request.issue_title, plan_summary=plan.summary,
                        subtask=Subtask(id="review-fixes", title="Address review comments", description=comments),
                    ), CodeReply, lane=step)
                    self._detail(step, "Review comments addressed on the PR branch")
                else:
                    self._detail(step, result.review.summary)

    async def _merge(self, repo: RunRepo, pr: PullRequest) -> str:
        if self._request.autonomy == "supervised":
            await self._gate("merge", "Merge this pull request?", f"{pr.url}\n\n{self._state.review.summary if self._state.review else ''}")
        else:
            self._skip("gate:merge", "Autonomy is set to auto: agents merge after an approving review")
        async with self._step("merge"):
            sha = await workflow.execute_activity_method(GitHubActivities.merge_pull_request, MergePrInput(
                repo=repo.repo, pr_number=pr.number, title=f"{self._request.issue_title} (#{pr.number})"), **GITHUB)
            self._detail("merge", f"Merged {sha[:7]}")
            return sha

    async def _deploy_to_staging(self, sha: str) -> None:
        async with self._step("staging"):
            await self._deploy(DeployRequest(action="staging", sha=sha, run_id=self._request.run_id))
            self._state.staging_sha = sha
            check = await self._check("staging")
            if not check.healthy:
                raise ApplicationError(f"Staging checks failed: {check.detail}", non_retryable=True)
            self._detail("staging", check.detail)

    async def _canary(self, repo: RunRepo, sha: str) -> bool:
        """Send a slice of production traffic to the new commit, watch it, then promote it or
        roll it back. The bake is a durable timer: it survives a worker restart."""
        async with self._step("canary"):
            await self._deploy(DeployRequest(action="canary", sha=sha, weight=CANARY_WEIGHT, run_id=self._request.run_id))
            self._state.canary_sha = sha
            checks = max(1, self._request.canary_bake_seconds // int(CANARY_CHECK_EVERY.total_seconds()))
            for n in range(1, checks + 1):
                await workflow.sleep(CANARY_CHECK_EVERY)
                check = await self._check("canary")
                self._detail("canary", f"Check {n}/{checks}: {check.detail}")
                if not check.healthy:
                    await self._roll_back(repo, sha, check)
                    return False
            await self._deploy(DeployRequest(action="promote", sha=sha, run_id=self._request.run_id))
            self._detail("canary", f"Healthy after {checks} checks; promoted to 100%")
            return True

    async def _roll_back(self, repo: RunRepo, sha: str, check: CheckResult) -> None:
        """Compensate, in order: stop the canary's traffic, revert the merge in git, and reopen
        the issue with what went wrong, so the next run starts from the findings."""
        self._mark("canary", "failed", note=f"Canary unhealthy: {check.detail}")
        async with self._step("rollback"):
            await self._deploy(DeployRequest(action="rollback", sha=sha, run_id=self._request.run_id))
            self._detail("rollback", "Canary traffic back to the stable version")
            revert = await workflow.execute_activity_method(GitHubActivities.revert_merge, RevertInput(
                repo=repo.repo, base_branch=repo.base_branch, merge_sha=sha,
                revert_branch=f"{self._branches}/revert", title=f"Revert: {self._request.issue_title}",
                body=f"The canary failed its checks ({check.detail}), so this change is reverted.",
            ), **GITHUB)
            await workflow.execute_activity_method(GitHubActivities.comment_on_issue, CommentInput(
                repo=repo.repo, issue_number=repo.issue_number, labels=["rolled-back"], reopen=True,
                body=f"⏪ Rolled back. The canary failed: {check.detail}. Revert: {revert.url}",
            ), **GITHUB)
            self._detail("rollback", f"Reverted in {revert.url}")
            self._state.canary_sha = None
            self._prove("D4", f"Bad canary caught ({check.detail}); deploy and merge reverted automatically")

    async def _finish(self, repo: RunRepo, shipped: bool) -> None:
        async with self._step("done"):
            u = self._state.usage
            summary = (
                f"{'🚀 Shipped to production' if shipped else '⏪ Rolled back'} by the agentic SDLC pipeline "
                f"({self._state.version}).\n\n"
                f"- Agent turns: {u.requests} model requests, {u.input_tokens:,} input / {u.output_tokens:,} output tokens\n"
                f"- Estimated model cost: ${u.cost_usd:.2f}\n"
                f"- Every model call, tool call, approval and retry is in Temporal Workflow `{self._id}`."
            )
            await workflow.execute_activity_method(GitHubActivities.comment_on_issue, CommentInput(
                repo=repo.repo, issue_number=repo.issue_number, body=summary, close=shipped), **GITHUB)
            self._state.status = "completed" if shipped else "rolled_back"
            self._prove("D6", "Every agent action of this run is recorded in its Event History")

    # -- Human gates -------------------------------------------------------------------------

    async def _gate(self, key: str, question: str, context: str) -> GateDecision:
        """Wait for a person. Waiting costs nothing and survives any restart, whether it lasts
        seconds or days."""
        since = workflow.now().timestamp()
        self._decision = None
        self._state.gate = Gate(key=key, question=question, context=context[:2000], waiting_since=since)
        self._state.status = "waiting"
        self._mark(f"gate:{key}", "waiting")
        await workflow.wait_condition(lambda: self._decision is not None)
        decision, self._decision = self._decision, None
        waited = workflow.now().timestamp() - since
        self._state.gate = None
        self._state.status = "running"
        self._mark(f"gate:{key}", "completed" if decision.approve else "skipped",
                   note=f"{'Approved' if decision.approve else 'Sent back'} after {_duration(waited)}")
        if key != "budget":
            self._prove("D2", f"{key.capitalize()} gate: waited {_duration(waited)} for a person, using no compute")
        return decision

    @workflow.update
    def decide(self, response: GateResponse) -> RunState:
        self._decision = response.decision
        if response.gate == "budget" and response.decision.raise_budget_usd:
            self._state.budget_usd += response.decision.raise_budget_usd
        return self._state

    @decide.validator
    def _validate_decide(self, response: GateResponse) -> None:
        if self._state.gate is None or self._state.gate.key != response.gate:
            raise ValueError(f"The run isn't waiting at the {response.gate} gate.")

    # -- Agents ------------------------------------------------------------------------------

    async def _start_agent(self, role: str, model: ModelChoice, workspace: Workspace | None, *,
                           lane: str | None = None) -> str:
        agent_id = f"{self._id}.{role}"
        if any(a.workflow_id == agent_id for a in self._state.agents):
            return agent_id
        workflow_type = {"planner": "PlannerAgent", "reviewer": "ReviewerAgent"}.get(role.split("-")[0], "CoderAgent")
        await workflow.start_child_workflow(
            workflow_type,
            args=[AgentConfig(), AgentInit(run_id=self._request.run_id, model=model, workspace=workspace,
                                           autonomy=self._request.autonomy)],
            id=agent_id,
        )
        self._state.agents.append(AgentRef(role=role, workflow_id=agent_id, model=model.key, lane=lane))
        self._publish()
        return agent_id

    async def _integration_coder(self, repo: RunRepo, branch: str) -> str:
        return await self._start_agent("coder-integration", self._request.models.coder,
                                       Workspace(repo=repo.repo, branch=branch))

    async def _turn(self, agent_id: str, handler: str, request: BaseModel, reply_type: type, *,
                    lane: str | None = None):
        await self._ensure_budget()
        self._turns[agent_id] = self._turns.get(agent_id, 0) + 1
        result: AgentTurnResult = await workflow.execute_activity_method(
            AgentTurnActivities.run_agent_turn,
            AgentTurn(workflow_id=agent_id, handler=handler, payload=request.model_dump(mode="json"),
                      # Deterministic, so a retried Activity re-attaches to the same turn.
                      update_id=f"{agent_id}:{handler}:{self._turns[agent_id]}"),
            **AGENT_TURN,
        )
        reply = reply_type.model_validate(result.output)
        self._state.usage = self._state.usage + reply.usage
        self._record_retries(agent_id, result, lane)
        self._publish()
        return reply

    async def _ensure_budget(self) -> None:
        """Stop before the next agent turn once the run has spent its budget, and let a person
        decide whether to spend more."""
        while self._state.usage.cost_usd >= self._state.budget_usd:
            decision = await self._gate(
                "budget", f"This run has spent ${self._state.usage.cost_usd:.2f} of its ${self._state.budget_usd:.2f} budget. Keep going?",
                "Agents are paused before their next model call. Raise the budget to continue.",
            )
            if not decision.approve:
                raise ApplicationError("Stopped at the budget limit.", non_retryable=True)
            self._prove("D3", f"Run paused at its budget and continued when a person raised it to ${self._state.budget_usd:.2f}")

    def _record_retries(self, agent_id: str, result: AgentTurnResult, lane: str | None) -> None:
        role = agent_id.rsplit(".", 1)[-1]
        if result.attempt > 1 or any("heartbeat" in r.last_failure.lower() for r in result.stats.retried):
            self._prove("D1", f"{role} lost its worker mid-turn and resumed on another; "
                              f"{result.stats.model_calls} model calls and {result.stats.tool_calls} tool calls ran once")
        rate_limited = [r for r in result.stats.retried if "429" in r.last_failure or "rate" in r.last_failure.lower()]
        if rate_limited:
            worst = max(r.attempt for r in rate_limited)
            self._prove("D3", f"{role}: rate-limited calls retried (up to attempt {worst}) while other agents kept working")
        if lane and result.stats.retried:
            self._detail(lane, f"{len(result.stats.retried)} call(s) needed a retry; the turn carried on")

    async def _close_agents(self) -> None:
        for ref in self._state.agents:
            try:
                await workflow.get_external_workflow_handle(ref.workflow_id).signal("close")
            except Exception:  # noqa: BLE001 - an agent that already closed needs nothing
                pass

    # -- Deploys and checks ------------------------------------------------------------------

    async def _deploy(self, request: DeployRequest) -> None:
        await workflow.execute_activity_method(
            DeployActivities.apply_deployment, DeployCall(environments_id=self._environments_id, request=request), **QUICK)

    async def _check(self, env: str) -> CheckResult:
        return await workflow.execute_activity_method(DeployActivities.check_environment, CheckInput(
            owner=self._request.owner, environments_id=self._environments_id, env=env), **QUICK)

    # -- Progress for the UI -----------------------------------------------------------------

    @asynccontextmanager
    async def _step(self, key: str):
        self._mark(key, "active")
        try:
            yield
        except BaseException:
            self._mark(key, "failed")
            raise
        if self._find(key).status == "active":
            self._mark(key, "completed")

    def _find(self, key: str) -> Step:
        return next(s for s in self._state.steps if s.key == key)

    def _mark(self, key: str, status: str, *, note: str | None = None) -> None:
        step = self._find(key)
        now = workflow.now().timestamp()
        step.status = status  # type: ignore[assignment]
        if status in ("active", "waiting"):
            step.started_at, step.ended_at = step.started_at or now, None
        else:
            step.ended_at = now
        if note is not None:
            step.note = note
        self._publish()

    def _skip(self, key: str, note: str) -> None:
        self._mark(key, "skipped", note=note)

    def _detail(self, key: str, detail: str) -> None:
        self._find(key).detail = detail[:400]
        self._publish()

    def _note_failure(self, message: str) -> None:
        active = [s for s in self._state.steps if s.status in ("active", "waiting")]
        for step in active:
            step.status, step.note = "failed", message[:300]

    def _prove(self, criterion: str, detail: str) -> None:
        if not any(p.criterion == criterion for p in self._state.proofs):
            self._state.proofs.append(Proof(criterion=criterion, detail=detail, at=workflow.now().timestamp()))
            self._publish()

    def _publish(self) -> None:
        # The memo is what the web reads with Describe: free, and available while workers are down.
        workflow.upsert_memo({"run": self._state.model_dump(mode="json")})

    @workflow.query
    def state(self) -> RunState:
        return self._state


def _initial_steps() -> list[Step]:
    steps = [
        Step(key="issue", label="Open the issue"),
        Step(key="plan", label="Planner agent drafts a plan"),
        Step(key="gate:plan", label="Human approves the plan"),
        Step(key="build", label="Coding agents build in parallel"),
        Step(key="pr", label="Integrate branches and open a PR"),
        Step(key="review", label="Review agent checks the PR"),
    ]
    if SECURITY_REVIEW:
        steps.append(Step(key="security", label="Security agent checks the PR"))
    steps += [
        Step(key="gate:merge", label="Human approves the merge"),
        Step(key="merge", label="Merge"),
        Step(key="staging", label="Deploy to staging and check it"),
        Step(key="gate:production", label="Human approves production"),
        Step(key="canary", label=f"Canary at {CANARY_WEIGHT}%, then promote"),
        Step(key="rollback", label="Roll back if the canary fails"),
        Step(key="gate:budget", label="Budget guard"),
        Step(key="done", label="Report back on the issue"),
    ]
    return steps


def _plan_comment(plan: Plan) -> str:
    lines = ["### 🗺️ Plan from the planner agent", "", plan.summary, "", "**Acceptance criteria**"]
    lines += [f"- {c}" for c in plan.acceptance_criteria]
    lines += ["", "**Subtasks** (one coding agent each, in parallel)"]
    lines += [f"- **{s.title}** (`{s.id}`): {s.description}" for s in plan.subtasks]
    return "\n".join(lines)


def _pr_body(plan: Plan, repo: RunRepo) -> str:
    return f"Closes #{repo.issue_number}\n\n{plan.summary}\n\n" + "\n".join(f"- {s.title}" for s in plan.subtasks)


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"
