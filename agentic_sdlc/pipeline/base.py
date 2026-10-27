"""What every agent run has in common, whatever started it on GitHub.

A run is a Workflow that starts agents as child Workflows, sends them turns, keeps a budget,
publishes its progress for the console, and talks with people on GitHub. People only ever talk
to it on GitHub: comments reach the run as a ``github_comment`` Signal from the webhook, a
comment that starts with a command (``/approve``, ``/ship``) decides a gate, and any other
comment is conversation the agents act on. Nothing polls GitHub; a run waiting for people costs
nothing until someone says something.

IssueToProduction, DependencyRepair and PullRequestReview subclass this and keep only their own
lifecycle.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.agent_protocol import AgentConfig

    from agentic_sdlc import config
    from agentic_sdlc.activities.agent_turn import AgentTurn, AgentTurnActivities, AgentTurnResult
    from agentic_sdlc.activities.checks import CheckActivities
    from agentic_sdlc.activities.github import (
        CODER,
        PIPELINE,
        PLANNER,
        ClosePrInput,
        CommentInput,
        CommentsInput,
        DiffInput,
        GitHubActivities,
        HumanComment,
        PullRequestInfo,
        ReviewInput,
        StatusInput,
    )
    from agentic_sdlc.models import (
        AgentInit,
        AgentRef,
        CodeReply,
        CodeRequest,
        Command,
        Feedback,
        Gate,
        ModelChoice,
        Plan,
        Proof,
        ReviewReply,
        ReviewRequest,
        RunRequest,
        RunState,
        Step,
        Subtask,
        TriageReply,
        TriageRequest,
        Workspace,
    )

# The issue and PR numbers a run is working on, as "issue:12" and "pr:13". The webhook finds
# the runs to Signal with a Visibility query on this, which isn't a billable Action.
GITHUB_REFS = SearchAttributeKey.for_keyword_list("AgenticSdlcGitHubRefs")

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

MAX_TEST_FIX_ROUNDS = 2
BUDGET_STEP_USD = 3.0


@dataclass
class Outcome:
    """How a wait for people ended."""

    command: Command | None = None
    comments: list[Feedback] = field(default_factory=list)
    timed_out: bool = False
    closed: bool = False  # the PR was closed while waiting


class AgentRun:
    """Shared machinery. Subclasses call ``_begin`` from ``@workflow.init``."""

    def _begin(self, request: RunRequest, steps: list[Step]) -> None:
        self._request = request
        info = workflow.info()
        self._id = info.workflow_id
        self._branches = f"runs/{info.task_queue}/{request.run_id}"
        self._environments_id = f"{info.task_queue}.gh.environments"
        self._inbox: list[Feedback] = []  # comments not yet handed to an agent
        self._steers: dict[str, list[Feedback]] = {}  # per coder lane, waiting for its turn to end
        self._seen: set[str] = set()
        self._closed_prs: set[int] = set()
        self._refs: list[str] = []
        # Parallel coders can reach the budget at the same moment; one gate, one answer.
        self._budget_lock = asyncio.Lock()
        self._turns: dict[str, int] = {}
        self._home: int | None = None  # where the run posts: its issue, or its PR
        self._state = RunState(
            run_id=request.run_id, kind=request.kind, version=config.PIPELINE_VERSION, title=request.title,
            budget_usd=request.budget_usd, started_at=workflow.now().timestamp(), steps=steps,
        )

    # -- GitHub: tracking, statuses, comments ------------------------------------------------

    def _track(self, *, issue: int | None = None, pr: int | None = None) -> None:
        """Record which issue and PR this run is about, so the webhook can find it."""
        for ref in ([f"issue:{issue}"] if issue else []) + ([f"pr:{pr}"] if pr else []):
            if ref not in self._refs:
                self._refs.append(ref)
        workflow.upsert_search_attributes([GITHUB_REFS.value_set(self._refs)])

    async def _post(self, number: int, body: str, *, agent: str = PIPELINE, **extra) -> None:
        """Comment on an issue or PR, labeled with who wrote it: an agent, or the pipeline."""
        await workflow.execute_activity_method(GitHubActivities.comment_on_issue, CommentInput(
            repo=self._request.repo, issue_number=number, body=body, agent=agent, **extra), **GITHUB)

    async def _set_check(self, branch: str, state: str, description: str) -> None:
        """The agent's required check on the PR: pending blocks merging; success or failure says
        whether the agent is done and happy."""
        await workflow.execute_activity_method(GitHubActivities.set_commit_status, StatusInput(
            repo=self._request.repo, branch=branch, state=state, description=description,
            target_url=self._request.console_url), **GITHUB)
        self._state.check, self._state.check_description = state, description  # type: ignore[assignment]
        self._publish()

    @workflow.signal
    def github_comment(self, comment: HumanComment) -> None:
        """A person commented on this run's issue or PR (delivered by the webhook)."""
        self._receive(comment)

    @workflow.signal
    def pull_request_closed(self, number: int) -> None:
        self._closed_prs.add(number)

    def _receive(self, comment: HumanComment) -> None:
        if comment.id in self._seen:
            return  # the webhook and a catch-up read can both deliver the same comment
        self._seen.add(comment.id)
        stage = self._state.gate.key if self._state.gate else "running"
        item = Feedback(id=comment.id, author=comment.author, text=comment.body.strip()[:4000], url=comment.url,
                        at=workflow.now().timestamp(), stage=stage)
        self._state.conversation.append(item)
        lanes = [s.key for s in self._state.steps if s.lane and s.status in ("active", "retrying")]
        if self._state.gate is None and lanes and not _command(item.text):
            # Coders are working and nobody's waiting at a gate: the comment steers every active
            # coder and becomes its next turn.
            item.kind, item.status, item.outcome = "steer", "queued", "Given to the coding agents when their current turns end"
            for lane in lanes:
                self._steers.setdefault(lane, []).append(item)
        else:
            self._inbox.append(item)
        self._publish()

    async def _wait_for_people(self, key: str, question: str, context: str = "", *, commands: list[str] = (),
                               conversational: bool = True, questions: list[str] | None = None,
                               issue: int | None = None, pr: int | None = None, closes_after: timedelta | None = None,
                               github_url: str | None = None) -> Outcome:
        """Wait for a person on GitHub. A comment starting with one of ``commands`` decides the
        gate; with ``conversational``, any other comment ends the wait too and is returned for
        the agents. Waiting costs nothing and survives any restart, whether it lasts seconds or
        days. ``closes_after`` gives up after that long; a closed PR ends the wait as well."""
        start = workflow.now()
        closes_at = start + closes_after if closes_after else None
        self._state.gate = Gate(key=key, question=question, context=context[:2000], waiting_since=start.timestamp(),
                                questions=questions or [], commands=list(commands), github_url=github_url,
                                closes_at=closes_at.timestamp() if closes_at else None)
        self._state.status = "waiting"
        self._mark(f"gate:{key}", "waiting")
        # GitHub doesn't redeliver a webhook that failed (say, while the web server restarted),
        # so read what's there once when a gate opens. Signals cover everything after.
        await self._catch_up(issue, pr)
        outcome = Outcome()
        while True:
            outcome = self._take_from_inbox(outcome, commands, conversational)
            if outcome.command or outcome.comments:
                break
            if pr and pr in self._closed_prs:
                outcome.closed = True
                break
            remaining = closes_at - workflow.now() if closes_at else None
            if remaining is not None and remaining <= timedelta(0):
                outcome.timed_out = True
                break
            try:
                await workflow.wait_condition(lambda: bool(self._inbox) or (pr in self._closed_prs if pr else False),
                                              timeout=remaining)
            except asyncio.TimeoutError:
                pass
        waited = (workflow.now() - start).total_seconds()
        self._state.gate = None
        self._state.status = "running"
        if outcome.command:
            self._mark(f"gate:{key}", "completed", note=f"{outcome.command.name} after {_duration(waited)}")
            self._prove("D2", f"{key.capitalize()} gate: waited {_duration(waited)} for {outcome.command.name} on GitHub, using no compute")
        elif outcome.comments:
            self._mark(f"gate:{key}", "active", note=f"A comment after {_duration(waited)}; agents are on it")
        else:
            self._mark(f"gate:{key}", "completed",
                       note="PR closed" if outcome.closed else f"Nobody commented in {_duration(waited)}; continuing")
        return outcome

    def _take_from_inbox(self, outcome: Outcome, commands: list[str], conversational: bool) -> Outcome:
        while self._inbox:
            item = self._inbox.pop(0)
            command = _command(item.text)
            if command and command.name in commands:
                item.kind, item.status, item.outcome = "command", "addressed", f"{command.name} accepted"
                outcome.command = command
                break
            if command or not conversational:
                awaited = " or ".join(commands) or "nothing"
                item.kind, item.status, item.outcome = "noted", "addressed", f"Noted; the run is waiting for {awaited}"
                continue
            outcome.comments.append(item)
        self._publish()
        return outcome

    async def _catch_up(self, issue: int | None, pr: int | None) -> None:
        if not issue and not pr:
            return
        since = datetime.fromtimestamp(self._state.started_at, tz=timezone.utc)
        found: list[HumanComment] = await workflow.execute_activity_method(
            GitHubActivities.fetch_comments,
            CommentsInput(repo=self._request.repo, issue=issue, pull=pr, since=_iso(since)), **GITHUB)
        for comment in found:
            self._receive(comment)

    # -- Agents ------------------------------------------------------------------------------

    async def _start_agent(self, role: str, model: ModelChoice, workspace: Workspace | None, *,
                           lane: str | None = None) -> str:
        agent_id = f"{self._id}.{role}"
        if any(a.workflow_id == agent_id for a in self._state.agents):
            return agent_id
        workflow_type = {"planner": "PlannerAgent", "reviewer": "ReviewerAgent"}.get(role.split("-")[0], "CoderAgent")
        await workflow.start_child_workflow(
            workflow_type,
            # Agents' tool calls are always auto-approved: nobody can approve one from GitHub, and
            # the console only watches. Supervised runs wait for /merge instead (workflow.py).
            args=[AgentConfig(), AgentInit(run_id=self._request.run_id, model=model, workspace=workspace,
                                           autonomy="auto")],
            id=agent_id,
        )
        self._state.agents.append(AgentRef(role=role, workflow_id=agent_id, model=model.key, lane=lane))
        self._publish()
        return agent_id

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
        """Stop before the next agent turn once the run has spent its budget, and ask on
        GitHub whether to spend more."""
        async with self._budget_lock:
            while self._state.usage.cost_usd >= self._state.budget_usd:
                spent, budget = self._state.usage.cost_usd, self._state.budget_usd
                if self._home:
                    await self._post(self._home, f"💸 This run has spent an estimated ${spent:.2f} of its ${budget:.2f} "
                                                 f"budget. Reply `/continue` to allow ${BUDGET_STEP_USD:.0f} more, or `/stop`.")
                outcome = await self._wait_for_people(
                    "budget", f"Spent ${spent:.2f} of ${budget:.2f}. Waiting for /continue or /stop.",
                    commands=["/continue", "/stop"], conversational=False, issue=self._home)
                if outcome.command is None or outcome.command.name == "/stop":
                    raise ApplicationError("Stopped at the budget limit.", non_retryable=True)
                self._state.budget_usd += BUDGET_STEP_USD
                self._prove("D3", "Run paused at its budget and continued when someone replied /continue")

    async def _planner(self, workspace: Workspace) -> str:
        return await self._start_agent("planner", self._request.models.planner, workspace)

    async def _triage(self, planner: str, plan: Plan, item: Feedback) -> None:
        triage = (await self._turn(planner, "triage", TriageRequest(plan=plan, comment=item.text), TriageReply)).triage
        item.kind, item.reason = triage.kind, triage.reason
        self._publish()

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

    # -- A pull request people can talk to ----------------------------------------------------

    async def _review_pr(self, pr: PullRequestInfo, plan: Plan, *, reviewer_name: str = "Code review agent") -> ReviewReply:
        reviewer = await self._start_agent("reviewer-quality", self._request.models.reviewer,
                                           Workspace(repo=self._request.repo, branch=pr.head_branch))
        diff = await self._diff(pr.base_branch, pr.head_branch)
        result = await self._turn(reviewer, "review", ReviewRequest(
            issue_title=pr.title, plan=plan, diff=diff), ReviewReply)
        self._state.review = result.review
        await workflow.execute_activity_method(GitHubActivities.post_review, ReviewInput(
            repo=self._request.repo, pr_number=pr.number, reviewer=reviewer_name, review=result.review), **GITHUB)
        return result

    async def _diff(self, base: str, head: str) -> str:
        return await workflow.execute_activity_method(
            GitHubActivities.get_diff, DiffInput(repo=self._request.repo, base=base, head=head), **GITHUB)

    async def _converse_on_pr(self, pr: PullRequestInfo, plan: Plan) -> None:
        """Listen to the PR until it's closed or goes quiet. Feedback goes to a coding agent on
        the PR branch, then the tests and the reviewer run again and the agent answers on the
        PR. A request outside the PR's scope gets an explanation instead of a change."""
        planner = await self._planner(Workspace(repo=self._request.repo, branch=pr.head_branch))
        idle = timedelta(hours=self._request.idle_close_hours)
        while True:
            outcome = await self._wait_for_people(
                "conversation", "Listening for comments on the PR", pr=pr.number,
                closes_after=idle, github_url=pr.url)
            if outcome.closed:
                return
            if outcome.timed_out:
                await workflow.execute_activity_method(GitHubActivities.close_pull_request, ClosePrInput(
                    repo=self._request.repo, pr_number=pr.number,
                    comment="Closing after a quiet day; the agents have stopped listening to this PR."), **GITHUB)
                return
            for item in outcome.comments:
                await self._triage(planner, plan, item)
            in_scope = [c for c in outcome.comments if c.kind == "feedback"]
            for item in outcome.comments:
                if item.kind == "scope_change":
                    await self._post(pr.number, f"> **@{item.author}**: {item.text[:300]}\n\n"
                                                f"That's outside this PR's scope ({item.reason}). Open an issue for it "
                                                "and label it `agent` if you'd like the agents to take it on.",
                                     agent=PLANNER)
                    self._resolve(item, "addressed", "Out of scope for this PR; suggested an issue")
            if in_scope:
                await self._address_on_pr(pr, plan, in_scope)

    async def _address_on_pr(self, pr: PullRequestInfo, plan: Plan, comments: list[Feedback]) -> None:
        async with self._step("feedback"):
            for item in comments:
                item.status = "working"
            who = ", ".join(sorted({f"@{c.author}" for c in comments}))
            if pr.from_fork:
                await self._post(pr.number, "This PR comes from a fork, which the agents can't push to. "
                                            "Here's what they'd change:\n\n" + "\n".join(f"- {c.text}" for c in comments),
                                 agent=CODER)
                for item in comments:
                    self._resolve(item, "addressed", "Fork PR: suggested rather than pushed")
                return
            self._detail("feedback", f"Addressing {_count(len(comments), 'comment')} from {who}")
            await self._set_check(pr.head_branch, "pending", "Agent is addressing review comments")
            workspace = Workspace(repo=self._request.repo, branch=pr.head_branch)
            coder = await self._start_agent("coder", self._request.models.coder, workspace)
            reply = await self._code_until_green(coder, workspace, plan.summary, Subtask(
                id="pr-feedback", title="Address comments on the pull request",
                description="\n".join(f"- @{c.author}: {c.text}" for c in comments)))
            quoted = "\n".join(f"> **@{c.author}**: {c.text[:300]}" for c in comments)
            await self._post(pr.number, f"{quoted}\n\n🛠️ Addressed: {reply.summary}\n\n"
                                        "The reviewer agent is checking the change again.", agent=CODER)
            for item in comments:
                self._resolve(item, "addressed", reply.summary)
            self._prove("D7", f"{who}'s comment on the PR was addressed by a coding agent and re-reviewed, without starting over")
        review = await self._review_pr(pr, plan)
        await self._set_check(pr.head_branch, "success" if review.review.verdict == "approve" else "failure",
                              "Agent review passed" if review.review.verdict == "approve" else review.review.summary)

    async def _code_until_green(self, coder: str, workspace: Workspace, summary: str, subtask: Subtask, *,
                                lane: str | None = None, feedback: str | None = None) -> CodeReply:
        """One coding turn, then the tests on the pushed branch; failures go back to the agent."""
        reply = await self._turn(coder, "code", CodeRequest(
            issue_title=self._state.title, plan_summary=summary, subtask=subtask, feedback=feedback), CodeReply, lane=lane)
        for _round in range(MAX_TEST_FIX_ROUNDS):
            tests = await workflow.execute_activity_method(CheckActivities.run_branch_tests, workspace, **QUICK)
            if tests.passed:
                return reply
            reply = await self._turn(coder, "code", CodeRequest(
                issue_title=self._state.title, plan_summary=summary, subtask=subtask,
                feedback=f"The test suite fails on the branch:\n{tests.output[-4000:]}"), CodeReply, lane=lane)
        raise ApplicationError("The change still fails its tests.", non_retryable=True)

    def _resolve(self, item: Feedback, status: str, outcome: str) -> None:
        item.status, item.outcome = status, outcome[:400]  # type: ignore[assignment]
        self._publish()

    # -- Progress for the console ------------------------------------------------------------

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
        for step in self._state.steps:
            if step.status in ("active", "waiting"):
                step.status, step.note = "failed", message[:300]

    def _finish_steps(self) -> None:
        self._state.ended_at = workflow.now().timestamp()
        for step in self._state.steps:
            if step.status == "pending":
                step.status = "skipped"
        self._publish()

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


def _command(text: str) -> Command | None:
    """``/stop not before Friday`` -> Command("/stop", "not before Friday")."""
    if not text.startswith("/"):
        return None
    name, _, argument = text.partition(" ")
    return Command(name=name.lower().strip(), argument=argument.strip())


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
