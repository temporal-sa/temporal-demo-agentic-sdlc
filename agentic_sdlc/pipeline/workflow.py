"""IssueToProduction: an issue labeled for the agents becomes a reviewed, deployed change.

Agents decide what to do inside a step. This Workflow decides the process: which agent works
when, what a person must approve, how people's comments reach the agents, what to retry, when to
stop spending, and how to undo a bad release. Read ``run`` top to bottom; it is the whole
lifecycle.

People drive it entirely from GitHub. The planner asks its questions on the issue and people
reply there; ``/approve`` approves the plan; a comment while coders work steers them; comments on
the PR are feedback (fixed on the spot) or a scope change (the run goes back to planning); a
comment once the change is on staging gets it adjusted there; and ``/ship`` or ``/stop`` decides
the release. The console only shows what's happening.

Every agent is a child Workflow (agents/workflows.py) whose model calls and tool calls are
Activities, so a crashed worker, a rate-limited provider or a two-hour wait for a person loses
no work.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from agentic_sdlc import config
    from agentic_sdlc.activities.checks import CheckActivities
    from agentic_sdlc.activities.deploy import CheckInput, CheckResult, DeployActivities, DeployCall
    from agentic_sdlc.activities.github import (
        CODER,
        PLANNER,
        BranchesInput,
        BranchRef,
        ClosePrInput,
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
    from agentic_sdlc.models import (
        ClarifyReply,
        CodeReply,
        CodeRequest,
        Command,
        Feedback,
        Plan,
        PlanReply,
        PlanRequest,
        ReviewReply,
        ReviewRequest,
        RunRequest,
        RunState,
        Step,
        Subtask,
        Workspace,
    )
    from agentic_sdlc.pipeline.base import GITHUB, MAX_TEST_FIX_ROUNDS, QUICK, AgentRun, _count, _duration
    from agentic_sdlc.pipeline.environments import DeployRequest

# Each Worker Deployment Version starts with one PIPELINE_VERSION and never changes it, so a
# Pinned run always replays against the code path it started on. v2 adds a security-review
# agent. In a real project v2 would simply be the next commit; see README "Notes".
SECURITY_REVIEW = config.PIPELINE_VERSION == "v2"

MAX_PLAN_ROUNDS = 3
MAX_PLAN_REVISIONS = 3  # scope changes that send the run back to planning
MAX_REVIEW_ROUNDS = 5  # rounds of PR feedback before the run gives up
MAX_STAGING_ROUNDS = 5  # adjustments made on staging before the agents ask for a new issue
CANARY_WEIGHT = 20


@workflow.defn
class IssueToProduction(AgentRun):
    @workflow.init
    def __init__(self, request: RunRequest) -> None:
        self._begin(request, _initial_steps())
        self._staging_rounds = 0

    @workflow.run
    async def run(self, request: RunRequest) -> RunState:
        self._publish()
        try:
            repo = await self._take_issue()
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
            release = await self._ask_to_ship(repo, plan)
            if release.name == "/stop":
                await self._stop(repo, release.argument)
                return self._state
            shipped = await self._canary(repo, self._state.staging_sha or merge_sha)
            await self._finish(repo, shipped)
        except ApplicationError as err:
            self._state.status = "failed"
            self._note_failure(str(err))
            raise
        finally:
            self._finish_steps()
            await self._close_agents()
        return self._state

    # -- Steps -------------------------------------------------------------------------------

    async def _take_issue(self) -> RunRepo:
        async with self._step("issue"):
            repo = await workflow.execute_activity_method(GitHubActivities.prepare_run, PrepareRunInput(
                repo=self._request.repo, issue_number=self._request.issue_number, branch_prefix=self._branches,
            ), **GITHUB)
            self._home = repo.issue_number
            self._track(issue=repo.issue_number)
            self._state.issue_url = repo.issue_url
            self._detail("issue", f"Issue #{repo.issue_number}")
            return repo

    async def _clarify(self, repo: RunRepo) -> str | None:
        """The planner asks about anything the issue leaves open; people reply on the issue,
        and the answers go into the plan."""
        planner = await self._planner(Workspace(repo=repo.repo, branch=repo.base_branch))
        async with self._step("clarify"):
            reply = await self._turn(planner, "clarify", self._plan_request(), ClarifyReply)
            questions = reply.clarification.questions[:3]
            if not questions:
                self._detail("clarify", "Nothing to ask: the issue is clear enough to plan")
                self._skip("gate:clarify", "The planner had no questions")
                return None
            self._detail("clarify", f"{_count(len(questions), 'question')} for the developer")
            await self._post(repo.issue_number, _questions_comment(questions), agent=PLANNER)
        outcome = await self._wait_for_people(
            "clarify", "The planner asked its questions on the issue", questions=questions,
            commands=["/skip"], issue=repo.issue_number, github_url=repo.issue_url)
        for item in outcome.comments:
            item.kind = "answer"
            self._resolve(item, "addressed", "Used in the plan")
        return "\n".join(c.text for c in outcome.comments) or None

    async def _plan(self, repo: RunRepo, answers: str | None, new_requirements: list[str]) -> Plan:
        planner = await self._planner(Workspace(repo=repo.repo, branch=repo.base_branch))
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
                await self._post(repo.issue_number, _plan_comment(plan, self._state.plan_revision), agent=PLANNER,
                                 labels=["agent-plan"])
            outcome = await self._wait_for_people(
                "plan", "The plan is on the issue, waiting for /approve", plan.summary,
                commands=["/approve"], issue=repo.issue_number, github_url=repo.issue_url)
            if outcome.command:
                return plan
            for item in outcome.comments:
                item.kind = "plan_feedback"
                self._resolve(item, "addressed", "The planner revised the plan")
                self._prove("D7", f"@{item.author} commented on the plan and the planner revised it before any code was written")
            feedback = "\n".join(c.text for c in outcome.comments)
        raise ApplicationError("The plan was sent back too many times.", non_retryable=True)

    def _plan_request(self, answers: str | None = None, feedback: str | None = None) -> PlanRequest:
        return PlanRequest(issue_title=self._request.title, issue_body=self._request.body,
                           clarifications=answers, feedback=feedback)

    async def _build(self, repo: RunRepo, plan: Plan) -> list[str]:
        """One coding agent per subtask, each on its own branch, all at once."""
        async with self._step("build"):
            branches = [f"{self._revision_branches}/{s.id}" for s in plan.subtasks]
            await workflow.execute_activity_method(GitHubActivities.create_branches, BranchesInput(
                repo=repo.repo, from_sha=repo.base_sha, branches=branches,
            ), **GITHUB)
            self._state.steps.extend(Step(key=f"code:{s.id}", label=s.title, lane=s.id) for s in plan.subtasks)
            await self._post(repo.issue_number, f"🔨 {_count(len(plan.subtasks), 'coding agent')} building now. "
                                                "Comment here to steer them: your comment becomes their next instruction "
                                                "when their current turn ends.")
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
                issue_title=self._request.title, plan_summary=plan.summary, subtask=subtask, feedback=feedback,
            ), CodeReply, lane=lane)

        async with self._step(lane):
            reply = await code()
            test_fixes = 0
            while True:
                # A comment on the issue waits for the agent's current turn to end, then becomes
                # the agent's next turn. Its earlier work stays on the branch.
                if steering := self._steers.pop(lane, []):
                    for item in steering:
                        item.status = "working"
                    self._publish()
                    reply = await code(feedback="A developer watching your work says:\n"
                                       + "\n".join(f"- {s.text}" for s in steering))
                    for item in steering:
                        self._resolve(item, "addressed", reply.summary)
                    self._prove("D7", f"@{steering[0].author} steered the coding agents from the issue; they adjusted without starting over")
                    continue
                # Don't take the agent's word for it: run the tests on the pushed branch.
                tests = await workflow.execute_activity_method(CheckActivities.run_branch_tests, workspace, **QUICK)
                if tests.passed:
                    if self._steers.get(lane):
                        continue  # a comment arrived while the tests ran
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
                title=self._request.title, body=_pr_body(plan, repo),
            ), **GITHUB)
            self._track(pr=pr.number)
            self._state.pr_url = pr.url
            await self._set_check(integration, "pending", "Agents are reviewing this change")
            self._detail("pr", f"PR #{pr.number}")
            return pr

    async def _resolve_conflict(self, repo: RunRepo, plan: Plan, integration: str, branch: str) -> None:
        """Two agents touched the same lines. A fresh coder re-applies one branch's change on
        top of the other's, on the integration branch."""
        diff = await self._diff(repo.base_sha, branch)
        resolver = await self._integration_coder(repo)
        self._detail("pr", f"Merge conflict on {branch.rsplit('/', 1)[-1]}; an agent is re-applying it")
        await self._turn(resolver, "code", CodeRequest(
            issue_title=self._request.title, plan_summary=plan.summary,
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
                diff = await self._diff(repo.base_branch, self._integration_branch)
                result = await self._turn(reviewer, "review", ReviewRequest(
                    issue_title=self._request.title, plan=plan, diff=diff, focus=focus), ReviewReply)
                self._state.review = result.review
                await workflow.execute_activity_method(GitHubActivities.post_review, ReviewInput(
                    repo=repo.repo, pr_number=pr.number, reviewer=reviewer_name, review=result.review), **GITHUB)
                if result.review.verdict == "request_changes":
                    # One round of fixes, made on the PR branch by a coder who sees the comments.
                    self._detail(step, f"Changes requested: {result.review.summary}")
                    fixer = await self._integration_coder(repo)
                    comments = "\n".join(f"- {c.file}: {c.comment}" for c in result.review.comments)
                    await self._turn(fixer, "code", CodeRequest(
                        issue_title=self._request.title, plan_summary=plan.summary,
                        subtask=Subtask(id="review-fixes", title="Address review comments", description=comments),
                    ), CodeReply, lane=step)
                    self._detail(step, "Review comments addressed on the PR branch")
                else:
                    self._detail(step, result.review.summary)

    async def _review_window(self, repo: RunRepo, plan: Plan, pr: PullRequest) -> Feedback | None:
        """People review the PR on GitHub. Feedback goes to a coding agent and the run comes
        back here; a scope change is returned so the run can re-plan. On auto merge the window
        closes by itself after a quiet minute; supervised waits for /merge."""
        auto = self._request.autonomy == "auto"
        window = timedelta(seconds=self._request.review_window_seconds) if auto else None
        await self._set_check(self._integration_branch, "success", "Agent review passed")
        await self._post(pr.number, "👀 Reviewed. Comment here to request changes; "
                                    + (f"the agents merge this in {_duration(window.total_seconds())} unless someone does."
                                       if window else "reply `/merge` when it's ready to merge."))
        planner = await self._planner(Workspace(repo=repo.repo, branch=repo.base_branch))
        for _round in range(MAX_REVIEW_ROUNDS):
            outcome = await self._wait_for_people(
                "merge", "Agents merge unless someone comments" if auto else "Waiting for /merge on the PR",
                self._state.review.summary if self._state.review else "", commands=[] if auto else ["/merge"],
                issue=repo.issue_number, pr=pr.number, closes_after=window, github_url=pr.url)
            if not outcome.comments:
                return None  # /merge, or nobody commented before the window closed
            for item in outcome.comments:
                await self._triage(planner, plan, item)
            scope_changes = [c for c in outcome.comments if c.kind == "scope_change"]
            if scope_changes:
                change = scope_changes[0]
                for item in outcome.comments:
                    if item is not change:
                        self._resolve(item, "addressed", "Included in the re-plan")
                change.text = "\n".join(c.text for c in outcome.comments)  # re-plan with everything that was said
                return change
            await self._address_feedback(repo, plan, pr, outcome.comments)
        raise ApplicationError("The pull request went through too many review rounds.", non_retryable=True)

    async def _address_feedback(self, repo: RunRepo, plan: Plan, pr: PullRequest, comments: list[Feedback]) -> None:
        """A coding agent changes the PR branch to address the comments, the tests run, the
        reviewer looks again, and the pipeline answers on the PR."""
        async with self._step("feedback"):
            for item in comments:
                item.status = "working"
            who = ", ".join(sorted({f"@{c.author}" for c in comments}))
            self._detail("feedback", f"Addressing {_count(len(comments), 'comment')} from {who}")
            await self._set_check(self._integration_branch, "pending", "Agent is addressing review comments")
            fixer = await self._integration_coder(repo)
            workspace = Workspace(repo=repo.repo, branch=self._integration_branch)
            reply = await self._code_until_green(fixer, workspace, plan.summary, Subtask(
                id="pr-feedback", title="Address comments on the pull request",
                description="\n".join(f"- @{c.author}: {c.text}" for c in comments)), lane="feedback")
            quoted = "\n".join(f"> **@{c.author}**: {c.text[:300]}" for c in comments)
            await self._post(pr.number, f"{quoted}\n\n🛠️ Addressed: {reply.summary}\n\n"
                                        "The reviewer agent is checking the change again.", agent=CODER)
            for item in comments:
                self._resolve(item, "addressed", reply.summary)
            self._detail("feedback", reply.summary)
            self._prove("D7", f"{who}'s comment on the PR was addressed by a coding agent and re-reviewed, without starting over")
        await self._review(repo, plan, pr)
        await self._set_check(self._integration_branch, "success", "Agent review passed")

    async def _start_over(self, repo: RunRepo, pr: PullRequest, change: Feedback) -> None:
        """A scope change voids the plan approval: close this PR and plan again, keeping the
        issue, the agents' history and everything the run has learned."""
        if self._state.plan_revision >= MAX_PLAN_REVISIONS:
            raise ApplicationError("The scope changed too many times; open a new issue.", non_retryable=True)
        await workflow.execute_activity_method(GitHubActivities.close_pull_request, ClosePrInput(
            repo=repo.repo, pr_number=pr.number,
            comment=f"🔁 This changes the scope: {change.reason}\n\nClosing this PR. The planner will revise the plan "
                    "on the issue, and it needs `/approve` again before agents write more code.",
            agent=PLANNER,
        ), **GITHUB)
        self._state.plan_revision += 1
        self._resolve(change, "addressed", f"The plan went back for revision {self._state.plan_revision}")
        self._prove("D7", f"@{change.author}'s comment changed the scope; the run went back to planning without starting over")
        self._state.pr_url = None
        self._state.review = None
        self._state.check = None
        # Run the build-to-review steps again for the new revision; the old agent lanes go.
        self._state.steps = [s for s in self._state.steps if not s.lane]
        for step in self._state.steps:
            if step.key in REVISION_STEPS:
                step.status, step.detail, step.note, step.started_at, step.ended_at = "pending", "", "", None, None
        self._detail("plan", f"Revision {self._state.plan_revision}: re-planning with the new requirement")

    async def _merge(self, repo: RunRepo, pr: PullRequest) -> str:
        async with self._step("merge"):
            sha = await workflow.execute_activity_method(GitHubActivities.merge_pull_request, MergePrInput(
                repo=repo.repo, pr_number=pr.number, title=f"{self._request.title} (#{pr.number})"), **GITHUB)
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

    async def _ask_to_ship(self, repo: RunRepo, plan: Plan) -> Command:
        """People try staging and decide on the issue. A comment that adjusts what they saw goes
        to a coding agent, staging redeploys, and they're asked again; a new requirement gets a
        reply suggesting its own issue."""
        slot = workflow.info().task_queue
        staging = f" Try it: {self._request.console_url}/apps/{slot}/staging/" if self._request.console_url else ""
        announce = True
        while True:
            if announce:
                await self._post(repo.issue_number, f"🚦 Staging passed its checks.{staging}\n\nReply `/ship` to send it "
                                                    f"to production through a {CANARY_WEIGHT}% canary, `/stop <reason>` to "
                                                    "stop here, or comment with what to change and an agent will adjust it "
                                                    "on staging.")
            outcome = await self._wait_for_people(
                "production", "Waiting for /ship, /stop or a comment on the issue",
                f"Staging passed its checks. The canary starts at {CANARY_WEIGHT}% of traffic.",
                commands=["/ship", "/stop"], issue=repo.issue_number, github_url=repo.issue_url)
            if outcome.command:
                for item in outcome.comments:
                    self._resolve(item, "addressed", f"Noted; {outcome.command.name} came with it")
                return outcome.command
            planner = await self._planner(Workspace(repo=repo.repo, branch=repo.base_branch))
            for item in outcome.comments:
                await self._triage(planner, plan, item)
            for item in outcome.comments:
                if item.kind == "scope_change":
                    await self._post(repo.issue_number, f"> **@{item.author}**: {item.text[:300]}\n\n"
                                                        f"That's a new requirement ({item.reason}), not an adjustment to "
                                                        "this change. Open an issue for it and label it `agent` if you'd "
                                                        "like the agents to take it on.", agent=PLANNER)
                    self._resolve(item, "addressed", "A new requirement; suggested its own issue")
            adjustments = [c for c in outcome.comments if c.kind == "feedback"]
            announce = bool(adjustments) and await self._adjust_on_staging(repo, plan, adjustments)

    async def _adjust_on_staging(self, repo: RunRepo, plan: Plan, comments: list[Feedback]) -> bool:
        """A coding agent changes what's on staging (the run's base branch), the tests run, and
        staging redeploys. Returns whether it did, so the run asks for /ship again."""
        quoted = "\n".join(f"> **@{c.author}**: {c.text[:300]}" for c in comments)
        if self._staging_rounds >= MAX_STAGING_ROUNDS:
            await self._post(repo.issue_number, f"{quoted}\n\nThat's {MAX_STAGING_ROUNDS} adjustments on staging already. "
                                                "Reply `/ship` or `/stop`, or open a new issue for more changes.")
            for item in comments:
                self._resolve(item, "addressed", "Too many adjustments; asked for /ship, /stop or a new issue")
            return False
        self._staging_rounds += 1
        async with self._step("adjust"):
            for item in comments:
                item.status = "working"
            who = ", ".join(sorted({f"@{c.author}" for c in comments}))
            self._detail("adjust", f"Adjusting staging for {_count(len(comments), 'comment')} from {who}")
            workspace = Workspace(repo=repo.repo, branch=repo.base_branch)
            coder = await self._start_agent(self._role("coder-staging"), self._request.models.coder, workspace)
            reply = await self._code_until_green(coder, workspace, plan.summary, Subtask(
                id="staging-feedback", title="Adjust what people saw on staging",
                description="\n".join(f"- @{c.author}: {c.text}" for c in comments)), lane="adjust")
            sha = await workflow.execute_activity_method(GitHubActivities.branch_head, BranchRef(
                repo=repo.repo, branch=repo.base_branch), **GITHUB)
            await self._post(repo.issue_number, f"{quoted}\n\n🛠️ Adjusted: {reply.summary}\n\nRedeploying staging.",
                             agent=CODER)
            for item in comments:
                self._resolve(item, "addressed", reply.summary)
            self._detail("adjust", reply.summary)
            self._prove("D7", f"{who}'s comment on staging was made by a coding agent and redeployed, without starting over")
        await self._deploy_to_staging(sha)
        return True

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
                repo=repo.repo, base_branch=repo.base_branch, merge_sha=sha, restore_sha=repo.base_sha,
                revert_branch=f"{self._branches}/revert", title=f"Revert: {self._request.title}",
                body=f"The canary failed its checks ({check.detail}), so this change is reverted.",
            ), **GITHUB)
            await self._post(repo.issue_number, f"⏪ Rolled back. The canary failed: {check.detail}. Revert: {revert.url}",
                             labels=["rolled-back"], reopen=True)
            self._state.revert_pr_url = revert.url
            self._detail("rollback", f"Reverted in PR #{revert.number}")
            self._state.canary_sha = None
            failed = f"{check.failed_probes} of {check.probes} probes failed" if check.failed_probes else (
                f"{check.browser_errors} browser errors in {check.browser_requests} page loads")
            self._prove("D4", f"Canary caught a bad release ({failed}); traffic, merge and issue rolled back")

    async def _stop(self, repo: RunRepo, reason: str) -> None:
        """Someone declined the release. Production is untouched; say so on the issue."""
        async with self._step("done"):
            await self._post(repo.issue_number, f"⏹️ Stopped at the production approval. {reason or 'No reason given.'} "
                                                "The change is on staging; production is unchanged.")
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
            await self._post(repo.issue_number, summary, close=shipped)
            self._state.status = "completed" if shipped else "rolled_back"
            self._prove("D6", "Every agent action of this run is recorded in its Event History")

    # -- Agents tied to this revision's branches ---------------------------------------------

    async def _integration_coder(self, repo: RunRepo) -> str:
        return await self._start_agent(self._role("coder-integration"), self._request.models.coder,
                                       Workspace(repo=repo.repo, branch=self._integration_branch))

    def _role(self, role: str) -> str:
        """Agents tied to a branch are new for each plan revision, since the branches are."""
        return role if self._state.plan_revision == 1 else f"{role}-r{self._state.plan_revision}"

    @property
    def _revision_branches(self) -> str:
        return self._branches if self._state.plan_revision == 1 else f"{self._branches}/r{self._state.plan_revision}"

    @property
    def _integration_branch(self) -> str:
        return f"{self._revision_branches}/integration"

    # -- Deploys and checks ------------------------------------------------------------------

    async def _deploy(self, request: DeployRequest) -> None:
        await workflow.execute_activity_method(
            DeployActivities.apply_deployment, DeployCall(environments_id=self._environments_id, request=request), **QUICK)

    async def _check(self, env: str) -> CheckResult:
        return await workflow.execute_activity_method(DeployActivities.check_environment, CheckInput(
            slot=workflow.info().task_queue, environments_id=self._environments_id, env=env), **QUICK)


REVISION_STEPS = {"plan", "gate:plan", "build", "pr", "review", "security", "gate:merge", "feedback"}


def _initial_steps() -> list[Step]:
    steps = [
        Step(key="issue", label="Take on the issue"),
        Step(key="clarify", label="Planner agent asks about anything unclear"),
        Step(key="gate:clarify", label="Developer answers on the issue"),
        Step(key="plan", label="Planner agent drafts a plan"),
        Step(key="gate:plan", label="Someone replies /approve"),
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
        Step(key="gate:production", label="Someone replies /ship"),
        Step(key="adjust", label="Coding agent adjusts what people saw on staging"),
        Step(key="canary", label=f"Canary at {CANARY_WEIGHT}%, then promote"),
        Step(key="rollback", label="Roll back if the canary fails"),
        Step(key="gate:budget", label="Budget guard"),
        Step(key="done", label="Report back on the issue"),
    ]
    return steps


def _questions_comment(questions: list[str]) -> str:
    lines = ["### ❓ Questions before I plan", ""]
    lines += [f"{i}. {q}" for i, q in enumerate(questions, 1)]
    lines += ["", "Reply here with your answers, or `/skip` to let the planner decide."]
    return "\n".join(lines)


def _plan_comment(plan: Plan, revision: int = 1) -> str:
    heading = "### 🗺️ Plan" + (f" (revision {revision})" if revision > 1 else "")
    lines = [heading, "", plan.summary, "", "**Acceptance criteria**"]
    lines += [f"- {c}" for c in plan.acceptance_criteria]
    lines += ["", "**Subtasks** (one coding agent each, in parallel)"]
    lines += [f"- **{s.title}** (`{s.id}`): {s.description}" for s in plan.subtasks]
    lines += ["", "Reply `/approve` to start building, or reply with what to change."]
    return "\n".join(lines)


def _pr_body(plan: Plan, repo: RunRepo) -> str:
    return f"Closes #{repo.issue_number}\n\n{plan.summary}\n\n" + "\n".join(f"- {s.title}" for s in plan.subtasks)
