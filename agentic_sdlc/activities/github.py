"""GitHub Activities: issues, branches, merges, pull requests and reverts on the sandbox repo.

Each one is safe to retry. Creates check for what an earlier attempt already made, so a
retried Activity never opens a second issue or pull request. Rate limits come back as an
error with ``next_retry_delay`` set from GitHub's headers, so Temporal waits as long as
GitHub asked before the next attempt.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import httpx
from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentic_sdlc import config, faults
from agentic_sdlc.agents import tools as git_tools
from agentic_sdlc.models import Review, Workspace

API = "https://api.github.com"


class RunRepo(BaseModel):
    repo: str
    issue_number: int
    issue_url: str
    base_branch: str
    base_sha: str


class PrepareRunInput(BaseModel):
    run_id: str
    branch_prefix: str
    title: str
    body: str


class BranchesInput(BaseModel):
    repo: str
    from_sha: str
    branches: list[str]


class CommentInput(BaseModel):
    repo: str
    issue_number: int
    body: str
    labels: list[str] = []
    reopen: bool = False
    close: bool = False


class MergeInput(BaseModel):
    repo: str
    base_sha: str
    integration_branch: str
    branches: list[str]


class MergeResult(BaseModel):
    sha: str
    conflicts: list[str]


class PullRequestInput(BaseModel):
    repo: str
    head: str
    base: str
    title: str
    body: str


class PullRequest(BaseModel):
    number: int
    url: str
    head_sha: str


class DiffInput(BaseModel):
    repo: str
    base: str
    head: str
    max_chars: int = 60_000


class ReviewInput(BaseModel):
    repo: str
    pr_number: int
    reviewer: str
    review: Review


class MergePrInput(BaseModel):
    repo: str
    pr_number: int
    title: str


class RevertInput(BaseModel):
    repo: str
    base_branch: str
    merge_sha: str
    revert_branch: str
    title: str
    body: str


class CleanupInput(BaseModel):
    repo: str
    branch_prefix: str
    marker: str


class GitHubActivities:
    def __init__(self, _client=None) -> None:
        self._http: httpx.AsyncClient | None = None

    def activities(self) -> list:
        return [
            self.prepare_run, self.create_branches, self.comment_on_issue, self.merge_branches,
            self.open_pull_request, self.get_diff, self.post_review, self.merge_pull_request,
            self.revert_merge, self.cleanup_runs,
        ]

    @activity.defn
    async def prepare_run(self, input: PrepareRunInput) -> RunRepo:
        """Open the issue and create the run's base branch from the baseline tag."""
        await _maybe_inject_rate_limit()
        repo = config.GITHUB_SANDBOX_REPO
        baseline = await self._get(f"/repos/{repo}/git/ref/tags/{config.BASELINE_REF}")
        base_sha = baseline["object"]["sha"]
        base_branch = f"{input.branch_prefix}/main"
        await self._ensure_branch(repo, base_branch, base_sha)

        marker = _marker(input.run_id)
        issue = await self._find_issue(repo, marker)
        if issue is None:
            issue = await self._post(
                f"/repos/{repo}/issues",
                {"title": input.title, "body": f"{input.body}\n\n{marker}", "labels": ["agentic-sdlc"]},
            )
        return RunRepo(repo=repo, issue_number=issue["number"], issue_url=issue["html_url"],
                       base_branch=base_branch, base_sha=base_sha)

    @activity.defn
    async def create_branches(self, input: BranchesInput) -> None:
        """Create one branch per coding agent, all from the run's base."""
        await asyncio.gather(*(self._ensure_branch(input.repo, b, input.from_sha) for b in input.branches))

    @activity.defn
    async def comment_on_issue(self, input: CommentInput) -> None:
        await _maybe_inject_rate_limit()
        path = f"/repos/{input.repo}/issues/{input.issue_number}"
        # A retry after a successful post would comment twice; skip if this exact body is the
        # issue's latest comment.
        comments = await self._get(f"{path}/comments?per_page=100")
        if not any(c["body"] == input.body for c in comments[-5:]):
            await self._post(f"{path}/comments", {"body": input.body})
        if input.labels:
            await self._post(f"{path}/labels", {"labels": input.labels})
        if input.reopen or input.close:
            await self._request("PATCH", path, {"state": "closed" if input.close else "open"})

    @activity.defn
    async def merge_branches(self, input: MergeInput) -> MergeResult:
        """Merge every agent's branch into a fresh integration branch. A branch that conflicts
        is reported rather than forced, so the pipeline can send it back to an agent."""
        await self._ensure_branch(input.repo, input.integration_branch, input.base_sha, reset=True)
        conflicts: list[str] = []
        sha = input.base_sha
        for branch in input.branches:
            response = await self._request(
                "POST", f"/repos/{input.repo}/merges",
                {"base": input.integration_branch, "head": branch, "commit_message": f"Merge {branch}"},
                allow={201, 204, 409},
            )
            if response.status_code == 409:
                conflicts.append(branch)
            elif response.status_code == 201:
                sha = response.json()["sha"]
        return MergeResult(sha=sha, conflicts=conflicts)

    @activity.defn
    async def open_pull_request(self, input: PullRequestInput) -> PullRequest:
        await _maybe_inject_rate_limit()
        owner = input.repo.split("/")[0]
        existing = await self._get(f"/repos/{input.repo}/pulls?state=open&head={owner}:{input.head}")
        pr = existing[0] if existing else await self._post(
            f"/repos/{input.repo}/pulls",
            {"title": input.title, "head": input.head, "base": input.base, "body": input.body},
        )
        return PullRequest(number=pr["number"], url=pr["html_url"], head_sha=pr["head"]["sha"])

    @activity.defn
    async def get_diff(self, input: DiffInput) -> str:
        response = await self._request(
            "GET", f"/repos/{input.repo}/compare/{input.base}...{input.head}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        diff = response.text
        if len(diff) > input.max_chars:
            diff = diff[: input.max_chars] + f"\n…[diff cut at {input.max_chars} characters]"
        return diff

    @activity.defn
    async def post_review(self, input: ReviewInput) -> None:
        """Post the agent's review as a pull request review comment. The demo's token belongs to
        the PR author, so it comments rather than approving; the merge gate is the approval."""
        verdict = "✅ Approve" if input.review.verdict == "approve" else "🔁 Changes requested"
        lines = [f"**{input.reviewer}**: {verdict}", "", input.review.summary]
        lines += [f"- `{c.file}`: {c.comment}" for c in input.review.comments]
        body = "\n".join(lines)
        reviews = await self._get(f"/repos/{input.repo}/pulls/{input.pr_number}/reviews")
        if not any(r.get("body") == body for r in reviews):
            await self._post(f"/repos/{input.repo}/pulls/{input.pr_number}/reviews", {"body": body, "event": "COMMENT"})

    @activity.defn
    async def merge_pull_request(self, input: MergePrInput) -> str:
        pr = await self._get(f"/repos/{input.repo}/pulls/{input.pr_number}")
        if pr.get("merged"):
            return pr["merge_commit_sha"]
        result = await self._request(
            "PUT", f"/repos/{input.repo}/pulls/{input.pr_number}/merge",
            {"merge_method": "merge", "commit_title": input.title},
        )
        return result.json()["sha"]

    @activity.defn
    async def revert_merge(self, input: RevertInput) -> PullRequest:
        """Undo a merged change the way a team would: a revert commit, through a pull request,
        merged into the base branch. GitHub's REST API has no revert, so this uses git."""
        await self._ensure_branch(input.repo, input.revert_branch, input.merge_sha)
        workspace = Workspace(repo=input.repo, branch=input.revert_branch)
        async with git_tools.workspace_lock(workspace):
            root = await git_tools.checkout(workspace)
            log = await git_tools._git(root, "log", "-1", "--format=%s")
            if not log.startswith("Revert"):
                await git_tools._git(
                    root, "-c", "user.name=Orbit release agent",
                    "-c", "user.email=agentic-sdlc-agent@users.noreply.github.com",
                    "revert", "--no-edit", "-m", "1", input.merge_sha,
                )
                await git_tools._git(root, "push", "--quiet", "origin", f"HEAD:{input.revert_branch}")
        pr = await self.open_pull_request(PullRequestInput(
            repo=input.repo, head=input.revert_branch, base=input.base_branch, title=input.title, body=input.body,
        ))
        await self.merge_pull_request(MergePrInput(repo=input.repo, pr_number=pr.number, title=input.title))
        return pr

    @activity.defn
    async def cleanup_runs(self, input: CleanupInput) -> int:
        """Reset: close this session's issues and pull requests and delete its branches."""
        refs = await self._get(f"/repos/{input.repo}/git/matching-refs/heads/{input.branch_prefix}")
        for pr in await self._get(f"/repos/{input.repo}/pulls?state=open&per_page=100"):
            if pr["head"]["ref"].startswith(input.branch_prefix):
                await self._request("PATCH", f"/repos/{input.repo}/pulls/{pr['number']}", {"state": "closed"})
        for issue in await self._get(f"/repos/{input.repo}/issues?state=open&labels=agentic-sdlc&per_page=100"):
            if input.marker in (issue.get("body") or ""):
                await self._request("PATCH", f"/repos/{input.repo}/issues/{issue['number']}", {"state": "closed"})
        for ref in refs:
            await self._request("DELETE", f"/repos/{input.repo}/git/{ref['ref']}", allow={204, 422})
            activity.heartbeat()
        return len(refs)

    # -- HTTP helpers ------------------------------------------------------------------------

    async def _ensure_branch(self, repo: str, branch: str, sha: str, *, reset: bool = False) -> None:
        response = await self._request(
            "POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}, allow={201, 422}
        )
        if response.status_code == 422 and reset:
            await self._request("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": sha, "force": True})

    async def _find_issue(self, repo: str, marker: str) -> dict | None:
        issues = await self._get(f"/repos/{repo}/issues?state=all&labels=agentic-sdlc&per_page=30&sort=created")
        return next((i for i in issues if marker in (i.get("body") or "")), None)

    async def _get(self, path: str):
        return (await self._request("GET", path)).json()

    async def _post(self, path: str, body: dict):
        return (await self._request("POST", path, body)).json()

    async def _request(self, method: str, path: str, body: dict | None = None, *,
                       allow: set[int] | None = None, headers: dict | None = None) -> httpx.Response:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=API, timeout=30,
                headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}", "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"},
            )
        response = await self._http.request(method, path, json=body, headers=headers)
        if allow and response.status_code in allow:
            return response
        if response.status_code in (403, 429) and _is_rate_limited(response):
            raise ApplicationError(
                f"GitHub rate limit ({response.status_code})", type="GitHubRateLimited",
                next_retry_delay=_retry_after(response),
            )
        if response.status_code >= 500:
            raise ApplicationError(f"GitHub {response.status_code} on {method} {path}", type="GitHubUnavailable")
        if response.status_code >= 400:
            # Bad input or a missing resource won't fix itself on retry.
            raise ApplicationError(
                f"GitHub {response.status_code} on {method} {path}: {response.text[:300]}",
                type="GitHubRejected", non_retryable=True,
            )
        return response


def _marker(run_id: str) -> str:
    return f"<!-- agentic-sdlc-run:{run_id} -->"


async def _maybe_inject_rate_limit() -> None:
    """Demo fault, armed by the presenter for this run and labeled as injected in the UI:
    answer as GitHub does when it rate limits, with a short Retry-After."""
    if await faults.take(activity.info().workflow_id or "", "github_rate_limit"):
        raise ApplicationError(
            "Injected: GitHub returned 429 Too Many Requests (Retry-After: 4s)",
            type="InjectedRateLimit",
            next_retry_delay=timedelta(seconds=4),
        )


def _is_rate_limited(response: httpx.Response) -> bool:
    return response.status_code == 429 or response.headers.get("x-ratelimit-remaining") == "0" or (
        "rate limit" in response.text.lower()
    )


def _retry_after(response: httpx.Response) -> timedelta:
    """GitHub's documented order: Retry-After, then the reset time, else at least a minute."""
    if retry_after := response.headers.get("retry-after"):
        return timedelta(seconds=int(retry_after))
    if response.headers.get("x-ratelimit-remaining") == "0" and (reset := response.headers.get("x-ratelimit-reset")):
        return timedelta(seconds=max(1, int(reset) - int(time.time())))
    return timedelta(seconds=60)
