"""PullRequestReview: an agent reviews a person's PR, then fixes things when asked.

Started by the webhook when someone opens a PR on the storefront. The agent's required check
stays pending until its review is done. Then the run listens to the PR: a reply asking for a
change sends a coding agent to push the fix to the PR branch, and the reviewer looks again. It
ends when the PR is closed, or after a quiet day.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from agentic_sdlc.activities.github import GitHubActivities, PullRequestInfo, PullRequestRef
    from agentic_sdlc.models import Plan, RunRequest, RunState, Step, Subtask
    from agentic_sdlc.pipeline.base import GITHUB, AgentRun


@workflow.defn
class PullRequestReview(AgentRun):
    @workflow.init
    def __init__(self, request: RunRequest) -> None:
        self._begin(request, _steps())

    @workflow.run
    async def run(self, request: RunRequest) -> RunState:
        self._publish()
        try:
            pr = await self._take_pull_request()
            plan = _plan(pr)
            async with self._step("review"):
                result = await self._review_pr(pr, plan)
                approved = result.review.verdict == "approve"
                self._detail("review", result.review.summary)
            await self._set_check(pr.head_branch, "success" if approved else "failure",
                                  "Agent review passed" if approved else result.review.summary)
            if not pr.from_fork:
                await self._post(pr.number, "Reply here if you'd like the coding agent to make a change; "
                                            "it will push to this branch and the reviewer will look again.")
            await self._converse_on_pr(pr, plan)
            self._state.status = "completed"
        except ApplicationError as err:
            self._state.status = "failed"
            self._note_failure(str(err))
            raise
        finally:
            self._finish_steps()
            await self._close_agents()
        return self._state

    async def _take_pull_request(self) -> PullRequestInfo:
        async with self._step("pr"):
            pr = await workflow.execute_activity_method(GitHubActivities.get_pull_request, PullRequestRef(
                repo=self._request.repo, number=self._request.pr_number), **GITHUB)
            self._home = pr.number
            self._track(pr=pr.number)
            self._state.pr_url, self._state.title = pr.url, pr.title
            self._detail("pr", f"PR #{pr.number} by @{pr.author}" + (" (from a fork: review only)" if pr.from_fork else ""))
            await self._set_check(pr.head_branch, "pending", "Agent is reviewing this PR")
            return pr


def _steps() -> list[Step]:
    return [
        Step(key="pr", label="Pull request opened"),
        Step(key="review", label="Review agent reviews the PR"),
        Step(key="gate:conversation", label="Listening for replies on the PR"),
        Step(key="feedback", label="Coding agent pushes the requested change"),
        Step(key="gate:budget", label="Budget guard"),
    ]


def _plan(pr: PullRequestInfo) -> Plan:
    """What the PR says it does, as the yardstick for the review and for triaging replies."""
    return Plan(summary=f"{pr.title}. {pr.body}".strip()[:1500],
                acceptance_criteria=["The change does what the PR description says", "Tests cover the new behavior"],
                subtasks=[Subtask(id="pr", title=pr.title, description=pr.body[:1500])])
