"""IssueToProduction: the agentic software delivery pipeline, as one Temporal Workflow.

Agents decide what to do inside a step. This Workflow decides the process: which agent works
when, what a human must approve, how people's comments reach the agents, what to retry, when to
stop spending, and how to undo a bad release. Read ``run`` top to bottom; it is the whole
lifecycle.

People steer the agents from the console or from GitHub. While the run waits at a gate it reads
new comments on the issue and the pull request; a comment on the PR is either feedback the agents
address on the spot, or a scope change that sends the run back to planning.

Every agent is a child Workflow (agents/workflows.py) whose model calls and tool calls are
Activities, so a crashed worker, a rate-limited provider or a two-hour wait for a person
loses no work. The UI reads this Workflow's progress from its memo with Describe.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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
        ClosePrInput,
        CommentInput,
        CommentsInput,
        DiffInput,
        GitHubActivities,
        HumanComment,
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
        ClarifyReply,
        CodeReply,
        CodeRequest,
        Feedback,
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
        TriageReply,
        TriageRequest,
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
MAX_PLAN_REVISIONS = 3  # scope changes that send the run back to planning
MAX_REVIEW_ROUNDS = 5  # rounds of PR feedback before the run gives up
MAX_STEERS_PER_LANE = 3
CANARY_WEIGHT = 20
# Gates where people can talk to the agents rather than only approve.
CONVERSATION_GATES = {"clarify", "plan", "merge"}


class GateResponse(BaseModel):
    """A person's answer to the gate the run is waiting at."""

    gate: str
    decision: GateDecision


class CommentRequest(BaseModel):
    """A comment typed in the console, treated like one left on GitHub."""

    text: str
    author: str = "presenter"


class SteerRequest(BaseModel):
    """A message for one coding agent while it works."""

    lane: str
    text: str
    author: str = "presenter"


@dataclass
class Outcome:
    """How a wait for people ended."""

    decision: GateDecision | None = None
    comments: list[Feedback] = field(default_factory=list)
    timed_out: bool = False


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
        self._inbox: list[Feedback] = []  # comments not yet handed to an agent
        self._steers: dict[str, list[Feedback]] = {}  # per coder lane, waiting for its turn to end
        self._seen_comments: set[str] = set()
        # Parallel coders can reach the budget at the same moment; one gate, one answer.
        self._budget_lock = asyncio.Lock()
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
            answers = await self._clarify(repo)
            new_requirements: list[str] = []
            while True:
                plan = await self._plan(repo, answers, new_requirements)
                branches = await self._build(repo, plan)
                pr = await self._open_pull_request(repo, plan, branches)
                await self._review(repo, plan, pr)
                scope_change = await self._review_window(repo, plan, pr)
                if scope_change is None:
                    break
                new_requirements.append(scope_change.text)
                await self._start_over(repo, pr, scope_change)
            merge_sha = await self._merge(repo, pr)
            await self._deploy_to_staging(merge_sha)
            release = await self._gate("production", "Ship this change to production?",
                                       f"Staging passed its checks. The canary starts at {CANARY_WEIGHT}% of traffic.")
            if not release.approve:
                await self._stop(repo, release.feedback)
                return self._state
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

    async def _clarify(self, repo: RunRepo) -> str | None:
        """The planner asks about anything the issue leaves open. People answer in the console
        or by replying on the issue; the answers go into the plan."""
        planner = await self._planner(repo)
        async with self._step("clarify"):
            reply = await self._turn(planner, "clarify", self._plan_request(), ClarifyReply)
            questions = reply.clarification.questions[:3]
            if not questions:
                self._detail("clarify", "Nothing to ask: the issue is clear enough to plan")
                self._skip("gate:clarify", "The planner had no questions")
                return None
            self._detail("clarify", f"{_count(len(questions), 'question')} for the developer")
            await workflow.execute_activity_method(GitHubActivities.comment_on_issue, CommentInput(
                repo=repo.repo, issue_number=repo.issue_number, body=_questions_comment(questions)), **GITHUB)
        outcome = await self._wait_for_people(
            "clarify", "The planner has questions before it plans", "", questions=questions,
            watch_issue=repo.issue_number, github_url=repo.issue_url)
        answers = [c.text for c in outcome.comments]
        if outcome.decision and outcome.decision.feedback:
            answers.insert(0, outcome.decision.feedback)
        for item in outcome.comments:
            self._resolve(item, "answer", "addressed", "Used in the plan")
        return "\n".join(answers) or None

    async def _plan(self, repo: RunRepo, answers: str | None, new_requirements: list[str]) -> Plan:
        planner = await self._planner(repo)
        feedback: str | None = None
        if new_requirements:
            feedback = "Requirements added during review:\n" + "\n".join(f"- {r}" for r in new_requirements)
        for _round in range(MAX_PLAN_ROUNDS):
            async with self._step("plan"):
                reply = await self._turn(planner, "plan", self._plan_request(answers, feedback), PlanReply)
                plan = reply.plan
                plan.subtasks = plan.subtasks[:3]
                self._state.plan = plan
                self._detail("plan", f"{len(plan.subtasks)} subtask(s): " + ", ".join(s.title for s in plan.subtasks))
                await workflow.execute_activity_method(GitHubActivities.comment_on_issue, CommentInput(
                    repo=repo.repo, issue_number=repo.issue_number, body=_plan_comment(plan, self._state.plan_revision),
                    labels=["agent-plan"],
                ), **GITHUB)
            outcome = await self._wait_for_people(
                "plan", "Approve this plan before any code is written?", plan.summary,
                watch_issue=repo.issue_number, github_url=repo.issue_url)
            if outcome.decision and outcome.decision.approve and not outcome.comments:
                return plan
            notes = [c.text for c in outcome.comments]
            if outcome.decision and outcome.decision.feedback:
                notes.insert(0, outcome.decision.feedback)
            for item in outcome.comments:
                self._resolve(item, "plan_feedback", "addressed", "The planner revised the plan")
                self._prove("D7", f"{_who(item)} commented on the plan and the planner revised it before any code was written")
            feedback = "\n".join(notes) or "Make the plan smaller and more specific."
        raise ApplicationError("The plan was rejected too many times.", non_retryable=True)

    def _plan_request(self, answers: str | None = None, feedback: str | None = None) -> PlanRequest:
        return PlanRequest(issue_title=self._request.issue_title, issue_body=self._request.issue_body,
                           clarifications=answers, feedback=feedback)

    async def _build(self, repo: RunRepo, plan: Plan) -> list[str]:
        """One coding agent per subtask, each on its own branch, all at once."""
        async with self._step("build"):
            branches = [f"{self._revision_branches}/{s.id}" for s in plan.subtasks]
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
        coder = await self._start_agent(self._role(f"coder-{subtask.id}"), self._request.models.coder, workspace,
                                        lane=subtask.id)

        async def code(feedback: str | None = None) -> CodeReply:
            return await self._turn(coder, "code", CodeRequest(
                issue_title=self._request.issue_title, plan_summary=plan.summary, subtask=subtask, feedback=feedback,
            ), CodeReply, lane=lane)

        async with self._step(lane):
            reply = await code()
            test_fixes = 0
            while True:
                # A developer's message waits for the agent's current turn to end, then becomes
                # the agent's next turn. Its earlier work stays on the branch.
                if steering := self._steers.pop(lane, []):
                    for item in steering:
                        item.status = "working"
                    self._publish()
                    reply = await code(feedback="A developer watching your work says:\n"
                                       + "\n".join(f"- {s.text}" for s in steering))
                    for item in steering:
                        self._resolve(item, "steer", "addressed", reply.summary)
                    self._prove("D7", f"{_who(steering[0])} steered a coding agent mid-task; it adjusted without starting over")
                    continue
                # Don't take the agent's word for it: run the tests on the pushed branch.
                tests = await workflow.execute_activity_method(CheckActivities.run_branch_tests, workspace, **QUICK)
                if tests.passed:
                    if self._steers.get(lane):
                        continue  # a message arrived while the tests ran
                    break
                test_fixes += 1
                if test_fixes > MAX_TEST_FIX_ROUNDS:
                    raise ApplicationError(f"Subtask {subtask.id} still fails its tests.", non_retryable=True)
                self._detail(lane, "Tests failed on the branch; sending the failures back to the agent")
                reply = await code(feedback=f"The test suite fails on your branch:\n{tests.output[-4000:]}")
            self._detail(lane, reply.summary)

    async def _open_pull_request(self, repo: RunRepo, plan: Plan, branches: list[str]) -> PullRequest:
        async with self._step("pr"):
            integration = self._integration_branch
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
        resolver = await self._integration_coder(repo)
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
                reviewer = await self._start_agent(self._role(f"reviewer-{focus}"), self._request.models.reviewer,
                                                   Workspace(repo=repo.repo, branch=self._integration_branch))
                diff = await workflow.execute_activity_method(GitHubActivities.get_diff, DiffInput(
                    repo=repo.repo, base=repo.base_branch, head=self._integration_branch), **GITHUB)
                result = await self._turn(reviewer, "review", ReviewRequest(
                    issue_title=self._request.issue_title, plan=plan, diff=diff, focus=focus), ReviewReply)
                self._state.review = result.review
                await workflow.execute_activity_method(GitHubActivities.post_review, ReviewInput(
                    repo=repo.repo, pr_number=pr.number, reviewer=reviewer_name, review=result.review), **GITHUB)
                if result.review.verdict == "request_changes":
                    # One round of fixes, made on the PR branch by a coder who sees the comments.
                    self._detail(step, f"Changes requested: {result.review.summary}")
                    fixer = await self._integration_coder(repo)
                    comments = "\n".join(f"- {c.file}: {c.comment}" for c in result.review.comments)
                    await self._turn(fixer, "code", CodeRequest(
                        issue_title=self._request.issue_title, plan_summary=plan.summary,
                        subtask=Subtask(id="review-fixes", title="Address review comments", description=comments),
                    ), CodeReply, lane=step)
                    self._detail(step, "Review comments addressed on the PR branch")
                else:
                    self._detail(step, result.review.summary)

    async def _review_window(self, repo: RunRepo, plan: Plan, pr: PullRequest) -> Feedback | None:
        """People review the PR, in the console or on GitHub. Feedback goes to a coding agent and
        the run comes back here; a scope change is returned so the run can re-plan. On auto merge
        the window closes by itself after a quiet minute; supervised waits for an approval."""
        window = self._request.review_window_seconds if self._request.autonomy == "auto" else None
        for _round in range(MAX_REVIEW_ROUNDS):
            outcome = await self._wait_for_people(
                "merge", "Merge this pull request?" if window is None else "Agents merge this PR unless someone comments",
                self._state.review.summary if self._state.review else "",
                watch_issue=repo.issue_number, watch_pr=pr.number, closes_after=window, github_url=pr.url)
            comments = list(outcome.comments)
            if outcome.decision and not outcome.decision.approve:
                comments.insert(0, self._console_feedback("merge", outcome.decision.feedback or ""))
            if not comments:
                return None  # approved, or nobody commented before the window closed
            scope_changes = []
            for item in comments:
                triage = (await self._turn(await self._planner(repo), "triage",
                                           TriageRequest(plan=plan, comment=item.text), TriageReply)).triage
                item.kind, item.reason = triage.kind, triage.reason
                if triage.kind == "scope_change":
                    scope_changes.append(item)
            self._publish()
            if scope_changes:
                change = scope_changes[0]
                for item in comments:
                    if item is not change:
                        self._resolve(item, item.kind or "feedback", "addressed", "Included in the re-plan")
                change.text = "\n".join(c.text for c in comments)  # re-plan with everything that was said
                return change
            await self._address_feedback(repo, plan, pr, comments)
        raise ApplicationError("The pull request went through too many review rounds.", non_retryable=True)

    async def _address_feedback(self, repo: RunRepo, plan: Plan, pr: PullRequest, comments: list[Feedback]) -> None:
        """A coding agent changes the PR branch to address the comments, the tests run, the
        reviewer looks again, and the pipeline answers on the PR."""
        async with self._step("feedback"):
            for item in comments:
                item.status = "working"
            self._detail("feedback", f"Addressing {_count(len(comments), 'comment')} from {', '.join(sorted({_who(c) for c in comments}))}")
            fixer = await self._integration_coder(repo)
            workspace = Workspace(repo=repo.repo, branch=self._integration_branch)
            reply = await self._turn(fixer, "code", CodeRequest(
                issue_title=self._request.issue_title, plan_summary=plan.summary,
                subtask=Subtask(id="pr-feedback", title="Address comments on the pull request",
                                description="\n".join(f"- {_who(c)}: {c.text}" for c in comments)),
            ), CodeReply, lane="feedback")
            tests = await workflow.execute_activity_method(CheckActivities.run_branch_tests, workspace, **QUICK)
            if not tests.passed:
                reply = await self._turn(fixer, "code", CodeRequest(
                    issue_title=self._request.issue_title, plan_summary=plan.summary,
                    subtask=Subtask(id="pr-feedback", title="Address comments on the pull request", description=""),
                    feedback=f"The test suite fails after your change:\n{tests.output[-4000:]}",
                ), CodeReply, lane="feedback")
            await workflow.execute_activity_method(GitHubActivities.comment_on_issue, CommentInput(
                repo=repo.repo, issue_number=pr.number, body=_addressed_comment(comments, reply.summary)), **GITHUB)
            for item in comments:
                self._resolve(item, "feedback", "addressed", reply.summary)
            self._detail("feedback", reply.summary)
            self._prove("D7", f"{_who(comments[0])}'s comment on the PR was addressed by a coding agent and "
                              "re-reviewed, without restarting the run")
        await self._review(repo, plan, pr)

    async def _start_over(self, repo: RunRepo, pr: PullRequest, change: Feedback) -> None:
        """A scope change voids the plan approval: close this PR and plan again, keeping the
        issue, the agents' history and everything the run has learned."""
        if self._state.plan_revision >= MAX_PLAN_REVISIONS:
            raise ApplicationError("The scope changed too many times; start a new run.", non_retryable=True)
        await workflow.execute_activity_method(GitHubActivities.close_pull_request, ClosePrInput(
            repo=repo.repo, pr_number=pr.number,
            comment=f"🔁 This changes the scope: {change.reason}\n\nClosing this PR. The planner will revise the plan, "
                    "and it needs approval again before agents write more code.",
        ), **GITHUB)
        self._state.plan_revision += 1
        self._resolve(change, "scope_change", "addressed", f"The plan went back for revision {self._state.plan_revision}")
        self._prove("D7", f"{_who(change)}'s comment changed the scope; the run went back to planning without starting over")
        self._state.pr_url = None
        self._state.review = None
        # Run the build-to-review steps again for the new revision; the old agent lanes go.
        self._state.steps = [s for s in self._state.steps if not s.lane]
        for step in self._state.steps:
            if step.key in REVISION_STEPS:
                step.status, step.detail, step.note, step.started_at, step.ended_at = "pending", "", "", None, None
        self._detail("plan", f"Revision {self._state.plan_revision}: re-planning with the new requirement")
        self._publish()

    async def _merge(self, repo: RunRepo, pr: PullRequest) -> str:
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
            every = self._request.canary_check_seconds
            checks = max(1, self._request.canary_bake_seconds // every)
            for n in range(1, checks + 1):
                await workflow.sleep(timedelta(seconds=every))
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
            self._state.revert_pr_url = revert.url
            self._detail("rollback", f"Reverted in PR #{revert.number}")
            self._state.canary_sha = None
            failed = f"{check.failed_probes} of {check.probes} probes failed" if check.failed_probes else (
                f"{check.browser_errors} browser errors in {check.browser_requests} page loads")
            self._prove("D4", f"Canary caught a bad release ({failed}); traffic, merge and issue rolled back")

    async def _stop(self, repo: RunRepo, reason: str | None) -> None:
        """A person declined the release. Production is untouched; say so on the issue."""
        async with self._step("done"):
            await workflow.execute_activity_method(GitHubActivities.comment_on_issue, CommentInput(
                repo=repo.repo, issue_number=repo.issue_number,
                body=f"⏹️ Stopped at the production approval. {reason or 'No reason given.'} "
                     "The change is on staging; production is unchanged."), **GITHUB)
            self._detail("done", f"Stopped before production: {reason or 'no reason given'}")
            self._state.status = "stopped"

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
        """Wait for a person's decision, and nothing else."""
        outcome = await self._wait_for_people(key, question, context)
        assert outcome.decision is not None
        return outcome.decision

    async def _wait_for_people(self, key: str, question: str, context: str, *, questions: list[str] | None = None,
                               watch_issue: int | None = None, watch_pr: int | None = None,
                               closes_after: int | None = None, github_url: str | None = None) -> Outcome:
        """Wait for a person: a decision in the console, or a comment from the console or GitHub.
        Waiting costs nothing and survives any restart, whether it lasts seconds or days. While it
        waits it reads new comments on the watched issue and PR every few seconds; with
        ``closes_after`` it gives up after that long and reports a timeout."""
        start = workflow.now()
        self._decision = None
        closes_at = start + timedelta(seconds=closes_after) if closes_after else None
        self._state.gate = Gate(key=key, question=question, context=context[:2000], waiting_since=start.timestamp(),
                                questions=questions or [], github_url=github_url,
                                closes_at=closes_at.timestamp() if closes_at else None)
        self._state.status = "waiting"
        self._mark(f"gate:{key}", "waiting")
        watching = bool(watch_issue or watch_pr)
        outcome = Outcome()
        while True:
            # Poll briskly while someone is likely engaged, then ease off to spare GitHub's rate limit.
            idle = workflow.now() - start
            poll = timedelta(seconds=self._request.github_poll_seconds if idle < timedelta(minutes=10) else 60)
            timeout = poll if watching else None
            if closes_at:
                timeout = min(timeout or closes_at - workflow.now(), closes_at - workflow.now())
            if timeout is None or timeout > timedelta(0):
                try:
                    await workflow.wait_condition(lambda: self._decision is not None or bool(self._inbox), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
            if self._decision is None and not self._inbox and watching:
                await self._read_github(watch_issue, watch_pr, since=start)
            if self._decision is not None or self._inbox:
                break
            if closes_at and workflow.now() >= closes_at:
                outcome.timed_out = True
                break
        outcome.decision, self._decision = self._decision, None
        outcome.comments, self._inbox = self._inbox, []
        waited = (workflow.now() - start).total_seconds()
        self._state.gate = None
        self._state.status = "running"
        if outcome.comments or (outcome.decision and not outcome.decision.approve):
            self._mark(f"gate:{key}", "active", note=f"Feedback after {_duration(waited)}; agents are on it")
        elif outcome.timed_out:
            self._mark(f"gate:{key}", "completed", note=f"Nobody commented in {_duration(waited)}; continuing")
        else:
            self._mark(f"gate:{key}", "completed", note=f"Approved after {_duration(waited)}")
        if key not in ("budget",) and outcome.decision:
            self._prove("D2", f"{key.capitalize()} gate: waited {_duration(waited)} for a person, using no compute")
        return outcome

    async def _read_github(self, issue: int | None, pull: int | None, *, since: datetime) -> None:
        found: list[HumanComment] = await workflow.execute_activity_method(
            GitHubActivities.fetch_comments,
            CommentsInput(repo=config.GITHUB_SANDBOX_REPO, issue=issue, pull=pull, since=_iso(since)), **GITHUB)
        for c in found:
            if c.id in self._seen_comments:
                continue
            self._seen_comments.add(c.id)
            item = Feedback(id=c.id, source="github", author=c.author, text=c.body, url=c.url,
                            at=workflow.now().timestamp(), stage=self._state.gate.key if self._state.gate else "")
            self._state.conversation.append(item)
            self._inbox.append(item)
        if found:
            self._publish()

    @workflow.update
    def decide(self, response: GateResponse) -> RunState:
        self._decision = response.decision
        if response.gate == "budget" and response.decision.raise_budget_usd:
            self._state.budget_usd += response.decision.raise_budget_usd
        if response.decision.feedback and response.gate in ("clarify", "plan"):
            item = self._console_feedback(response.gate, response.decision.feedback)
            self._resolve(item, "answer" if response.gate == "clarify" else "plan_feedback", "addressed",
                          "Used in the plan" if response.gate == "clarify" else "The planner revised the plan")
        return self._state

    @decide.validator
    def _validate_decide(self, response: GateResponse) -> None:
        if self._state.gate is None or self._state.gate.key != response.gate:
            raise ValueError(f"The run isn't waiting at the {response.gate} gate.")
        if response.gate == "merge" and not response.decision.approve and not (response.decision.feedback or "").strip():
            raise ValueError("Say what should change, so the agents can act on it.")

    @workflow.update
    def comment(self, request: CommentRequest) -> RunState:
        item = self._console_feedback(self._state.gate.key if self._state.gate else "", request.text, request.author)
        self._inbox.append(item)
        return self._state

    @comment.validator
    def _validate_comment(self, request: CommentRequest) -> None:
        if not request.text.strip():
            raise ValueError("The comment is empty.")
        if self._state.gate is None or self._state.gate.key not in CONVERSATION_GATES:
            raise ValueError("The agents aren't waiting for comments right now.")

    @workflow.update
    def steer(self, request: SteerRequest) -> RunState:
        item = self._console_feedback(request.lane, request.text, request.author)
        item.kind, item.status, item.outcome = "steer", "queued", "Given to the agent when its current turn ends"
        self._steers.setdefault(request.lane, []).append(item)
        self._publish()
        return self._state

    @steer.validator
    def _validate_steer(self, request: SteerRequest) -> None:
        if not request.text.strip():
            raise ValueError("The message is empty.")
        step = next((s for s in self._state.steps if s.key == request.lane and s.lane), None)
        if step is None or step.status not in ("active", "retrying"):
            raise ValueError("That agent isn't working right now.")
        sent = sum(1 for f in self._state.conversation if f.kind == "steer" and f.stage == request.lane)
        if sent >= MAX_STEERS_PER_LANE:
            raise ValueError(f"That agent has had {MAX_STEERS_PER_LANE} messages already.")

    def _console_feedback(self, stage: str, text: str, author: str = "presenter") -> Feedback:
        item = Feedback(id=f"console:{len(self._state.conversation) + 1}", source="console", author=author,
                        text=text.strip()[:4000], at=workflow.now().timestamp(), stage=stage)
        self._state.conversation.append(item)
        self._publish()
        return item

    def _resolve(self, item: Feedback, kind: str, status: str, outcome: str) -> None:
        item.kind = item.kind or kind  # type: ignore[assignment]
        item.status, item.outcome = status, outcome[:400]  # type: ignore[assignment]
        self._publish()

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

    async def _integration_coder(self, repo: RunRepo) -> str:
        return await self._start_agent(self._role("coder-integration"), self._request.models.coder,
                                       Workspace(repo=repo.repo, branch=self._integration_branch))

    async def _planner(self, repo: RunRepo) -> str:
        return await self._start_agent("planner", self._request.models.planner,
                                       Workspace(repo=repo.repo, branch=repo.base_branch))

    def _role(self, role: str) -> str:
        """Agents tied to a branch are new for each plan revision, since the branches are."""
        return role if self._state.plan_revision == 1 else f"{role}-r{self._state.plan_revision}"

    @property
    def _revision_branches(self) -> str:
        return self._branches if self._state.plan_revision == 1 else f"{self._branches}/r{self._state.plan_revision}"

    @property
    def _integration_branch(self) -> str:
        return f"{self._revision_branches}/integration"

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
        async with self._budget_lock:
            await self._hold_at_budget()

    async def _hold_at_budget(self) -> None:
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
                              f"{_count(result.stats.model_calls, 'model call')} and "
                              f"{_count(result.stats.tool_calls, 'tool call')} each ran once")
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
        Step(key="clarify", label="Planner agent asks about anything unclear"),
        Step(key="gate:clarify", label="Developer answers the planner"),
        Step(key="plan", label="Planner agent drafts a plan"),
        Step(key="gate:plan", label="Human approves the plan"),
        Step(key="build", label="Coding agents build in parallel"),
        Step(key="pr", label="Integrate branches and open a PR"),
        Step(key="review", label="Review agent checks the PR"),
    ]
    if SECURITY_REVIEW:
        steps.append(Step(key="security", label="Security agent checks the PR"))
    steps += [
        Step(key="gate:merge", label="People review the PR; comments go to the agents"),
        Step(key="feedback", label="Coding agent addresses PR comments"),
        Step(key="merge", label="Merge"),
        Step(key="staging", label="Deploy to staging and check it"),
        Step(key="gate:production", label="Human approves production"),
        Step(key="canary", label=f"Canary at {CANARY_WEIGHT}%, then promote"),
        Step(key="rollback", label="Roll back if the canary fails"),
        Step(key="gate:budget", label="Budget guard"),
        Step(key="done", label="Report back on the issue"),
    ]
    return steps


REVISION_STEPS = {"plan", "gate:plan", "build", "pr", "review", "security", "gate:merge", "feedback"}


def _questions_comment(questions: list[str]) -> str:
    lines = ["### ❓ The planner agent has questions before it plans", ""]
    lines += [f"{i}. {q}" for i, q in enumerate(questions, 1)]
    lines += ["", "Reply here or in the console."]
    return "\n".join(lines)


def _addressed_comment(comments: list[Feedback], summary: str) -> str:
    quoted = "\n".join(f"> **{_who(c)}**: {c.text[:300]}" for c in comments)
    return f"{quoted}\n\n🛠️ Addressed by the coding agent: {summary}\n\nThe reviewer agent is checking the change again."


def _who(item: Feedback) -> str:
    return f"@{item.author}" if item.source == "github" else item.author


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plan_comment(plan: Plan, revision: int = 1) -> str:
    heading = "### 🗺️ Plan from the planner agent" + (f" (revision {revision})" if revision > 1 else "")
    lines = [heading, "", plan.summary, "", "**Acceptance criteria**"]
    lines += [f"- {c}" for c in plan.acceptance_criteria]
    lines += ["", "**Subtasks** (one coding agent each, in parallel)"]
    lines += [f"- **{s.title}** (`{s.id}`): {s.description}" for s in plan.subtasks]
    return "\n".join(lines)


def _pr_body(plan: Plan, repo: RunRepo) -> str:
    return f"Closes #{repo.issue_number}\n\n{plan.summary}\n\n" + "\n".join(f"- {s.title}" for s in plan.subtasks)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"
