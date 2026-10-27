"""GitHub Activities: issues, branches, merges, pull requests, reviews, commit statuses and
reverts on the sandbox repo, plus the scenario triggers the console uses.

Each one is safe to retry. Creates check for what an earlier attempt already made, so a
retried Activity never opens a second issue or pull request. Rate limits come back as an
error with ``next_retry_delay`` set from GitHub's headers, so Temporal waits as long as
GitHub asked before the next attempt.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from typing import Literal

from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentic_sdlc import config, faults
from agentic_sdlc.agents import tools as git_tools
from agentic_sdlc.models import Review, Workspace

API = "https://api.github.com"
# Everything the pipeline writes on GitHub carries this marker, so reading comments back can
# tell people from the bot even though both post with the same token's account.
BOT_MARKER = "<!-- agentic-sdlc-bot -->"


class RunRepo(BaseModel):
    repo: str
    issue_number: int
    issue_url: str
    base_branch: str
    base_sha: str


class PrepareRunInput(BaseModel):
    repo: str
    issue_number: int
    branch_prefix: str


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


class CommentsInput(BaseModel):
    repo: str
    issue: int | None = None
    pull: int | None = None
    since: str  # ISO 8601; only comments created after this


class HumanComment(BaseModel):
    id: str
    number: int
    author: str
    body: str
    url: str
    created_at: str


class ClosePrInput(BaseModel):
    repo: str
    pr_number: int
    comment: str


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
    owner_label: str
    branch_prefix: str


class PullRequestRef(BaseModel):
    repo: str
    number: int


class PullRequestInfo(BaseModel):
    number: int
    url: str
    title: str
    body: str
    author: str
    head_branch: str
    head_sha: str
    base_branch: str
    from_fork: bool


class StatusInput(BaseModel):
    repo: str
    branch: str  # the status goes on the branch's current head commit
    state: Literal["pending", "success", "failure", "error"]
    description: str
    target_url: str | None = None


class UpdatePrInput(BaseModel):
    repo: str
    number: int
    body: str


class ScenarioInput(BaseModel):
    """Start a scenario the way a person would on GitHub, labeled for one copy of the demo."""

    repo: str
    owner_label: str
    title: str = ""
    body: str = ""


class ScenarioStarted(BaseModel):
    url: str  # the issue or the PR, or for the bump, GitHub's prefilled "Open a pull request" page
    kind: str
    open_pr: bool = False  # the presenter opens the PR from ``url``, as a developer would


# The required check the agent sets on pull requests; a ruleset on main requires it.
STATUS_CONTEXT = "agentic-sdlc/agent-review"
BUMP_WORKFLOW = "dependency-bump.yml"
BUMP_PACKAGE, BUMP_TO = "orbit-money", "2.0.0"
BUMP_BRANCH_WAIT_SECONDS = 120  # the Action usually pushes its branch within 30 seconds
SAMPLE_PR_BRANCH = "samples/discount-rounding"


class GitHubActivities:
    def __init__(self, _client=None) -> None:
        self._http: httpx.AsyncClient | None = None

    def activities(self) -> list:
        return [
            self.prepare_run, self.create_branches, self.comment_on_issue, self.merge_branches,
            self.open_pull_request, self.get_diff, self.post_review, self.merge_pull_request,
            self.revert_merge, self.cleanup_runs, self.fetch_comments, self.close_pull_request,
            self.get_pull_request, self.set_commit_status, self.update_pull_request,
            self.open_issue_for_copy, self.dispatch_bump, self.open_sample_pr,
        ]

    @activity.defn
    async def prepare_run(self, input: PrepareRunInput) -> RunRepo:
        """Take on the issue someone opened, and create the run's base branch from the
        baseline tag. The run's PRs merge there, so main stays untouched for everyone."""
        await _maybe_inject_rate_limit()
        baseline = await self._get(f"/repos/{input.repo}/git/ref/tags/{config.BASELINE_REF}")
        base_sha = baseline["object"]["sha"]
        base_branch = f"{input.branch_prefix}/main"
        await self._ensure_branch(input.repo, base_branch, base_sha)
        issue = await self._get(f"/repos/{input.repo}/issues/{input.issue_number}")
        return RunRepo(repo=input.repo, issue_number=issue["number"], issue_url=issue["html_url"],
                       base_branch=base_branch, base_sha=base_sha)

    @activity.defn
    async def create_branches(self, input: BranchesInput) -> None:
        """Create one branch per coding agent, all from the run's base."""
        await asyncio.gather(*(self._ensure_branch(input.repo, b, input.from_sha) for b in input.branches))

    @activity.defn
    async def comment_on_issue(self, input: CommentInput) -> None:
        await _maybe_inject_rate_limit()
        path = f"/repos/{input.repo}/issues/{input.issue_number}"
        body = _bot(input.body)
        # A retry after a successful post would comment twice; skip if this exact body is already there.
        comments = await self._get(f"{path}/comments?per_page=100&sort=created&direction=desc")
        if not any(c["body"] == body for c in comments):
            await self._post(f"{path}/comments", {"body": body})
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
            {"title": input.title, "head": input.head, "base": input.base, "body": _bot(input.body)},
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
        """Post the agent's review on the PR, with comments on the lines they're about. The
        demo's token can belong to the PR's author, so it comments rather than approving; the
        required commit status is the gate. If GitHub rejects a line (it isn't in the diff),
        the comments go in the review body instead."""
        verdict = "✅ Looks good" if input.review.verdict == "approve" else "🔁 Changes requested"
        summary = [f"**{input.reviewer}**: {verdict}", "", input.review.summary]
        inline = [c for c in input.review.comments if c.line]
        listed = [c for c in input.review.comments if not c.line]
        path = f"/repos/{input.repo}/pulls/{input.pr_number}/reviews"
        if any(BOT_MARKER in (r.get("body") or "") and r.get("body", "").startswith(summary[0])
               and input.review.summary in r.get("body", "") for r in await self._get(path)):
            return  # an earlier attempt posted it
        body = _bot("\n".join(summary + [f"- `{c.file}`: {c.comment}" for c in listed]))
        payload = {"body": body, "event": "COMMENT",
                   "comments": [{"path": c.file, "line": c.line, "side": "RIGHT", "body": c.comment} for c in inline]}
        response = await self._request("POST", path, payload, allow={200, 422})
        if response.status_code == 422:
            everything = summary + [f"- `{c.file}`" + (f" line {c.line}" if c.line else "") + f": {c.comment}"
                                    for c in input.review.comments]
            await self._post(path, {"body": _bot("\n".join(everything)), "event": "COMMENT"})

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
    async def fetch_comments(self, input: CommentsInput) -> list[HumanComment]:
        """Comments people left on the issue or the pull request since a time: conversation
        comments, review summaries and inline review comments. The bot's own are skipped."""
        found: list[HumanComment | None] = []
        # A PR's conversation comments live on its issue, so both numbers use the issues API.
        for number in (n for n in (input.issue, input.pull) if n):
            for c in await self._get(f"/repos/{input.repo}/issues/{number}/comments?since={input.since}&per_page=100"):
                found.append(_human(c, number, "issue"))
        if input.pull:
            for c in await self._get(f"/repos/{input.repo}/pulls/{input.pull}/comments?since={input.since}&per_page=100"):
                found.append(_human(c, input.pull, "review-comment", file=c.get("path")))
            for r in await self._get(f"/repos/{input.repo}/pulls/{input.pull}/reviews?per_page=100"):
                if r.get("body") and (r.get("submitted_at") or "") > input.since:
                    found.append(_human({**r, "created_at": r["submitted_at"]}, input.pull, "review"))
        people = [c for c in found if c is not None and c.body.strip() and c.created_at > input.since]
        return sorted(people, key=lambda c: c.created_at)

    @activity.defn
    async def close_pull_request(self, input: ClosePrInput) -> None:
        await self.comment_on_issue(CommentInput(repo=input.repo, issue_number=input.pr_number, body=input.comment))
        await self._request("PATCH", f"/repos/{input.repo}/pulls/{input.pr_number}", {"state": "closed"})

    @activity.defn
    async def get_pull_request(self, input: PullRequestRef) -> PullRequestInfo:
        pr = await self._get(f"/repos/{input.repo}/pulls/{input.number}")
        return PullRequestInfo(
            number=pr["number"], url=pr["html_url"], title=pr["title"], body=pr.get("body") or "",
            author=(pr.get("user") or {}).get("login", ""), head_branch=pr["head"]["ref"], head_sha=pr["head"]["sha"],
            base_branch=pr["base"]["ref"], from_fork=(pr["head"].get("repo") or {}).get("full_name") != input.repo,
        )

    @activity.defn
    async def set_commit_status(self, input: StatusInput) -> None:
        """Set the agent's check on the branch's head commit. A pending or failing check blocks
        merging when the target branch's ruleset requires it."""
        head = await self._get(f"/repos/{input.repo}/git/ref/heads/{input.branch}")
        payload = {"state": input.state, "context": STATUS_CONTEXT, "description": input.description[:140]}
        if input.target_url:
            payload["target_url"] = input.target_url
        await self._post(f"/repos/{input.repo}/statuses/{head['object']['sha']}", payload)

    @activity.defn
    async def update_pull_request(self, input: UpdatePrInput) -> None:
        await self._request("PATCH", f"/repos/{input.repo}/pulls/{input.number}", {"body": _bot(input.body)})

    @activity.defn
    async def open_issue_for_copy(self, input: ScenarioInput) -> ScenarioStarted:
        """Open an issue labeled for the agents and for this copy of the demo; the webhook for
        it starts the run, exactly as if a person had opened it on GitHub."""
        await self._ensure_label(input.repo, input.owner_label)
        issue = await self._post(f"/repos/{input.repo}/issues", {
            "title": input.title, "body": input.body, "labels": ["agent", input.owner_label]})
        return ScenarioStarted(url=issue["html_url"], kind="issue")

    @activity.defn
    async def dispatch_bump(self, input: ScenarioInput) -> ScenarioStarted:
        """Run the repo's Dependency bump Action, which pushes a Dependabot-style bump branch, and
        return GitHub's "Open a pull request" page for it, prefilled and labeled for this copy.
        The presenter opens the PR from there, and that PR's webhook starts the repair.

        The Action doesn't open the PR itself: organizations often forbid Actions from opening
        PRs, and a person opening it is the developer's part of the story anyway."""
        await self._ensure_label(input.repo, input.owner_label)
        spec = (await self._request("GET", f"/repos/{input.repo}/contents/package.json?ref=main",
                                    headers={"Accept": "application/vnd.github.raw+json"})).json()
        dependency = spec["dependencies"][BUMP_PACKAGE]  # "github:temporal-sa/orbit-money#v1.0.0"
        source, before = dependency.removeprefix("github:").split("#v")
        branch = f"dependency-bump/{BUMP_PACKAGE}-{BUMP_TO}-{activity.info().activity_id.rsplit('-', 1)[-1]}"
        if not await self._branch_exists(input.repo, branch):  # a retry after the dispatch
            await self._request("POST", f"/repos/{input.repo}/actions/workflows/{BUMP_WORKFLOW}/dispatches", {
                "ref": "main", "inputs": {"package": BUMP_PACKAGE, "to_version": BUMP_TO, "branch": branch}}, allow={204})
        # A bounded wait for one Action run, heartbeating, so the console can hand over a link that works.
        deadline = time.monotonic() + BUMP_BRANCH_WAIT_SECONDS
        while not await self._branch_exists(input.repo, branch):
            if time.monotonic() > deadline:
                raise ApplicationError(f"The Dependency bump Action didn't push {branch}; check the repo's Actions tab.",
                                       type="GitHubRejected", non_retryable=True)
            activity.heartbeat()
            await asyncio.sleep(2)
        title = f"Bump {BUMP_PACKAGE} from {before} to {BUMP_TO}"
        query = urlencode({"expand": "1", "title": title, "body": _bump_body(source, before, BUMP_TO),
                           "labels": f"dependencies,{input.owner_label}"})
        return ScenarioStarted(url=f"https://github.com/{input.repo}/compare/main...{branch}?{query}",
                               kind="dependency", open_pr=True)

    async def _branch_exists(self, repo: str, branch: str) -> bool:
        response = await self._request("GET", f"/repos/{repo}/git/ref/heads/{branch}", allow={404})
        return response.status_code == 200

    @activity.defn
    async def open_sample_pr(self, input: ScenarioInput) -> ScenarioStarted:
        """Open a pull request from a prepared branch with a subtle bug, as a person would, for
        the agent to review. Each run gets its own branch so several people can do it at once."""
        await self._ensure_label(input.repo, input.owner_label)
        sample = await self._get(f"/repos/{input.repo}/git/ref/heads/{SAMPLE_PR_BRANCH}")
        branch = f"review-sample/{activity.info().activity_id.rsplit('-', 1)[-1]}"
        await self._ensure_branch(input.repo, branch, sample["object"]["sha"])
        pr = await self._post(f"/repos/{input.repo}/pulls", {
            "title": "Add a bulk discount for 3 or more items", "head": branch, "base": "main",
            "body": "Customers buying 3 or more items get 10% off the subtotal. Shown as its own line in the totals."})
        await self._post(f"/repos/{input.repo}/issues/{pr['number']}/labels", {"labels": [input.owner_label]})
        return ScenarioStarted(url=pr["html_url"], kind="review")

    @activity.defn
    async def cleanup_runs(self, input: CleanupInput) -> int:
        """Reset: close this copy's open issues and pull requests, and delete the branches its
        runs created. Other copies' work is left alone."""
        closed = 0
        for item in await self._get(f"/repos/{input.repo}/issues?state=open&labels={input.owner_label}&per_page=100"):
            await self._request("PATCH", f"/repos/{input.repo}/issues/{item['number']}", {"state": "closed"})
            closed += 1
            if "pull_request" in item:
                pr = await self._get(f"/repos/{input.repo}/pulls/{item['number']}")
                head = pr["head"]["ref"]
                if head.startswith(("dependency-bump/", "review-sample/")):
                    await self._request("DELETE", f"/repos/{input.repo}/git/refs/heads/{head}", allow={204, 422})
            activity.heartbeat()
        for ref in await self._get(f"/repos/{input.repo}/git/matching-refs/heads/{input.branch_prefix}"):
            await self._request("DELETE", f"/repos/{input.repo}/git/{ref['ref']}", allow={204, 422})
            activity.heartbeat()
        return closed

    # -- HTTP helpers ------------------------------------------------------------------------

    async def _ensure_branch(self, repo: str, branch: str, sha: str, *, reset: bool = False) -> None:
        response = await self._request(
            "POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}, allow={201, 422}
        )
        if response.status_code == 422 and reset:
            await self._request("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": sha, "force": True})

    async def _ensure_label(self, repo: str, name: str) -> None:
        await self._request("POST", f"/repos/{repo}/labels",
                            {"name": name, "color": "5319e7", "description": "Owned by this copy of the agentic SDLC demo"},
                            allow={201, 422})

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


def _bot(body: str) -> str:
    return body if BOT_MARKER in body else f"{body}\n\n{BOT_MARKER}"


def _human(raw: dict, number: int, kind: str, *, file: str | None = None) -> HumanComment | None:
    """A person's comment, or None for the bot's own. Checked on the whole body: the marker is at
    the end, so a long bot comment cut short would pass for a person's."""
    body = raw.get("body") or ""
    if BOT_MARKER in body:
        return None
    if file:
        body = f"(on {file}) {body}"
    return HumanComment(id=f"{kind}:{raw['id']}", number=number, author=(raw.get("user") or {}).get("login", "someone"),
                        body=body[:4000], url=raw.get("html_url", ""), created_at=raw.get("created_at", ""))


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


def _bump_body(source: str, before: str, after: str) -> str:
    """A Dependabot-style description for the bump PR."""
    return f"""Bumps [{BUMP_PACKAGE}](https://github.com/{source}) from {before} to {after}.

<details>
<summary>Release notes</summary>

See the [{BUMP_PACKAGE} changelog](https://github.com/{source}/blob/v{after}/CHANGELOG.md).
</details>

<details>
<summary>Commits</summary>

[Compare v{before}...v{after}](https://github.com/{source}/compare/v{before}...v{after})
</details>

Updated in `package.json` and the import map in `index.html` by the *Dependency bump* workflow, in the style
of a Dependabot version update."""
