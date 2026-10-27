"""DependencyRepair: a dependency-bump PR either passes, or an agent makes it pass and explains why.

The bump PR comes from the storefront's Dependency bump Action, in the style of a Dependabot
version update, and its webhook starts this run. The agent's required check on the PR stays
pending while it works, so nobody merges an upgrade the agents haven't finished with.

Read ``run`` top to bottom: test the bump; if it breaks, diagnose why from the failures and the
dependency's changelog, fix our code on the bump branch, rewrite the PR so it explains the bump,
what broke and what changed, have a second model review it, then listen to the PR for comments.
"""

from __future__ import annotations

import re

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from agentic_sdlc.activities.checks import CheckActivities, TestRun
    from agentic_sdlc.activities.github import CODER, GitHubActivities, PullRequestInfo, PullRequestRef, UpdatePrInput
    from agentic_sdlc.models import (
        CodeReply,
        DiagnoseReply,
        DiagnoseRequest,
        Diagnosis,
        Plan,
        RunRequest,
        RunState,
        Step,
        Subtask,
        Workspace,
    )
    from agentic_sdlc.pipeline.base import GITHUB, QUICK, AgentRun

BUMP_TITLE = re.compile(r"[Bb]ump (?P<package>\S+) from (?P<from_version>\S+) to (?P<to_version>\S+)")


@workflow.defn
class DependencyRepair(AgentRun):
    @workflow.init
    def __init__(self, request: RunRequest) -> None:
        self._begin(request, _steps())

    @workflow.run
    async def run(self, request: RunRequest) -> RunState:
        self._publish()
        try:
            pr = await self._take_pull_request()
            bump = BUMP_TITLE.search(pr.title)
            if bump is None:
                raise ApplicationError(f"Can't tell which dependency “{pr.title}” bumps.", non_retryable=True)
            package, before, after = bump["package"], bump["from_version"], bump["to_version"]
            workspace = Workspace(repo=self._request.repo, branch=pr.head_branch)

            tests = await self._test_the_bump(workspace, package, after)
            if tests.passed:
                await self._compatible(pr, package, after)
            else:
                plan = _plan(package, after)
                diagnosis = await self._diagnose(pr, workspace, package, before, after, tests)
                fix = await self._fix(workspace, plan, diagnosis)
                await self._explain(pr, package, before, after, diagnosis, tests, fix)
                await self._review(pr, plan)
                await self._converse_on_pr(pr, plan)
            self._state.status = "completed"
            self._prove("D6", "Every step the agents took on this upgrade is recorded in its Event History")
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
            self._detail("pr", f"PR #{pr.number} by @{pr.author}: {pr.title}")
            await self._set_check(pr.head_branch, "pending", "Agent is checking whether this upgrade breaks anything")
            return pr

    async def _test_the_bump(self, workspace: Workspace, package: str, version: str) -> TestRun:
        async with self._step("tests"):
            tests = await workflow.execute_activity_method(CheckActivities.run_branch_tests, workspace, **QUICK)
            self._detail("tests", f"Tests {'pass' if tests.passed else 'fail'} with {package} {version}")
            return tests

    async def _compatible(self, pr: PullRequestInfo, package: str, version: str) -> None:
        for key in ("diagnose", "fix", "explain", "review", "gate:conversation"):
            self._skip(key, "Not needed: the upgrade is compatible")
        await self._post(pr.number, f"✅ The test suite passes with {package} {version}; no code changes are needed.")
        await self._set_check(pr.head_branch, "success", f"Compatible with {package} {version}")

    async def _diagnose(self, pr: PullRequestInfo, workspace: Workspace, package: str, before: str, after: str,
                        tests: TestRun) -> Diagnosis:
        """Before changing anything, work out why it breaks: the failures, the dependency's
        changelog, and the code that uses it."""
        async with self._step("diagnose"):
            coder = await self._start_agent("coder", self._request.models.coder, workspace)
            diff = await self._diff(pr.base_branch, pr.head_branch)
            reply = await self._turn(coder, "diagnose", DiagnoseRequest(
                package=package, from_version=before, to_version=after, test_output=tests.output, diff=diff), DiagnoseReply)
            diagnosis = reply.diagnosis
            self._state.diagnosis = diagnosis
            self._detail("diagnose", diagnosis.root_cause)
            await self._post(pr.number, _diagnosis_comment(package, after, diagnosis), agent=CODER)
            return diagnosis

    async def _fix(self, workspace: Workspace, plan: Plan, diagnosis: Diagnosis) -> CodeReply:
        """Adapt our code to the new version, on the bump branch, until the tests pass."""
        async with self._step("fix"):
            coder = await self._start_agent("coder", self._request.models.coder, workspace)
            reply = await self._code_until_green(coder, workspace, plan.summary, Subtask(
                id="upgrade-fix", title=plan.summary, description=diagnosis.fix_plan))
            self._detail("fix", reply.summary)
            return reply

    async def _explain(self, pr: PullRequestInfo, package: str, before: str, after: str, diagnosis: Diagnosis,
                       tests: TestRun, fix: CodeReply) -> None:
        async with self._step("explain"):
            body = _explanation(pr, package, before, after, diagnosis, tests, fix)
            await workflow.execute_activity_method(GitHubActivities.update_pull_request, UpdatePrInput(
                repo=self._request.repo, number=pr.number, body=body, agent=CODER), **GITHUB)
            self._detail("explain", "PR description rewritten: the bump, what broke, what changed and why")

    async def _review(self, pr: PullRequestInfo, plan: Plan) -> None:
        async with self._step("review"):
            result = await self._review_pr(pr, plan)
            approved = result.review.verdict == "approve"
            self._detail("review", result.review.summary)
        await self._set_check(pr.head_branch, "success" if approved else "failure",
                              "Upgrade fixed, tested and reviewed: ready to merge" if approved else result.review.summary)


def _steps() -> list[Step]:
    return [
        Step(key="pr", label="Dependency-bump PR opened"),
        Step(key="tests", label="Run the tests with the new version"),
        Step(key="diagnose", label="Coding agent works out what broke and why"),
        Step(key="fix", label="Coding agent adapts the code on the bump branch"),
        Step(key="explain", label="Rewrite the PR to explain the change"),
        Step(key="review", label="Review agent checks the upgrade"),
        Step(key="gate:conversation", label="Listening for comments on the PR"),
        Step(key="feedback", label="Coding agent addresses PR comments"),
        Step(key="gate:budget", label="Budget guard"),
    ]


def _plan(package: str, version: str) -> Plan:
    return Plan(
        summary=f"Upgrade {package} to {version} and adapt our code to its changes.",
        acceptance_criteria=[f"All tests pass with {package} {version}",
                             "Prices and totals render exactly as before the upgrade",
                             "No pinning to the old version or compatibility shims"],
        subtasks=[Subtask(id="upgrade-fix", title=f"Adapt the code to {package} {version}", description="")],
    )


def _diagnosis_comment(package: str, version: str, d: Diagnosis) -> str:
    return (f"### 🔎 Why {package} {version} breaks the build\n\n"
            f"**What changed in {package}:** {d.what_changed}\n\n**What broke:** {d.what_broke}\n\n"
            f"**Root cause:** {d.root_cause}\n\n**Fix:** {d.fix_plan}\n\n"
            "The coding agent is making these changes on this branch now.")


def _explanation(pr: PullRequestInfo, package: str, before: str, after: str, d: Diagnosis, tests: TestRun,
                 fix: CodeReply) -> str:
    failing = "\n".join(line for line in tests.output.splitlines() if "not ok" in line or "Error" in line)[:1500]
    return f"""Bumps **{package}** from {before} to {after}, and adapts our code to the breaking changes in {after}.

## What changed in {package} {after}

{d.what_changed}

## What broke

{d.what_broke}

```
{failing or tests.output[-1500:]}
```

## What we changed, and why

{d.root_cause}

{fix.summary}

## How to verify

- `npm install && npm test`: the suite passes with {package} {after}.
- Open the storefront and check prices, the cart totals and tax render as before.

---
<details>
<summary>Original description</summary>

{pr.body}
</details>

Diagnosed, fixed and explained by the coding agent, then reviewed by the review agent."""
