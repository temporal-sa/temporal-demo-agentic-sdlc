"""GitHub webhooks: signatures, which copy of the demo acts on what, and what it does.

``route`` is pure, so most of this needs no server. The last tests carry actions out against a
local Temporal dev server: redelivery idempotence, Signals reaching the right run, the run cap.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentic_sdlc import config
from agentic_sdlc.activities.github import BOT_MARKER, CommentInput, HumanComment
from agentic_sdlc.models import RunRequest
from agentic_sdlc.pipeline.base import GITHUB_REFS
from agentic_sdlc.web import webhooks
from agentic_sdlc.web.webhooks import RunSettings, SignalRuns, StartRun

REPO = "temporal-sa/agentic-sdlc-storefront"
SECRET = "test-secret"


@pytest.fixture(autouse=True)
def hosted(monkeypatch):
    """The hosted copy by default; tests that need a laptop or a workspace change it."""
    monkeypatch.setattr(config, "GITHUB_SANDBOX_REPO", REPO)
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "TASK_QUEUE", "agentic-sdlc")
    monkeypatch.setattr(config, "BASE_TASK_QUEUE", "agentic-sdlc")
    monkeypatch.setattr(config, "OWNER_LABEL", "agentic-sdlc:agentic-sdlc")
    monkeypatch.setattr(config, "IS_LOCAL_COPY", False)


def laptop(monkeypatch, name="local-maya"):
    monkeypatch.setattr(config, "TASK_QUEUE", "agentic-sdlc")
    monkeypatch.setattr(config, "OWNER_LABEL", f"agentic-sdlc:{name}")
    monkeypatch.setattr(config, "IS_LOCAL_COPY", True)


def labels(*names):
    return [{"name": n} for n in names]


def issue_event(action="opened", issue_labels=("agent",), body="Please add it", label=None, number=12):
    payload = {"action": action, "repository": {"full_name": REPO},
               "issue": {"number": number, "title": "Free shipping", "body": body, "labels": labels(*issue_labels)}}
    if label:
        payload["label"] = {"name": label}
    return payload


def pr_event(action="opened", pr_labels=(), title="Add a bulk discount", head="feature/bulk", body="10% off",
             draft=False, label=None, number=13):
    payload = {"action": action, "repository": {"full_name": REPO},
               "pull_request": {"number": number, "title": title, "body": body, "draft": draft,
                                "labels": labels(*pr_labels), "head": {"ref": head}}}
    if label:
        payload["label"] = {"name": label}
    return payload


def comment_event(body="Looks good", on_pr=False, number=12):
    issue = {"number": number}
    if on_pr:
        issue["pull_request"] = {"url": "https://api.github.com/..."}
    return {"action": "created", "repository": {"full_name": REPO}, "issue": issue,
            "comment": {"id": 7, "body": body, "user": {"login": "maya"}, "html_url": "https://x/c/7",
                        "created_at": "2026-10-26T10:00:00Z"}}


# -- Signatures ---------------------------------------------------------------------------


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_must_match_the_body_and_the_secret():
    body = b'{"zen": "Keep it logically awesome."}'
    assert webhooks.verify(SECRET, body, sign(body))
    assert not webhooks.verify(SECRET, body + b" ", sign(body))
    assert not webhooks.verify(SECRET, body, sign(body, "other"))
    assert not webhooks.verify(SECRET, body, None)
    assert not webhooks.verify(SECRET, body, sign(body).replace("sha256=", "sha1="))
    assert not webhooks.verify("", body, sign(body, ""))  # no secret configured: trust nothing


def test_endpoint_rejects_unsigned_deliveries_and_answers_pings():
    from agentic_sdlc.web.app import app

    client = TestClient(app)  # no lifespan: these requests never reach Temporal
    body = json.dumps({"zen": "hi"}).encode()
    assert client.post("/webhooks/github", content=body, headers={"X-GitHub-Event": "ping"}).status_code == 401
    assert client.post("/webhooks/github", content=body, headers={
        "X-GitHub-Event": "ping", "X-Hub-Signature-256": sign(body, "wrong")}).status_code == 401
    ok = client.post("/webhooks/github", content=body, headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sign(body)})
    assert ok.status_code == 200


# -- Starting runs ------------------------------------------------------------------------


def test_an_issue_labeled_agent_starts_the_issue_flow():
    assert webhooks.route("issues", issue_event()) == [StartRun("issue", "agentic-sdlc", 12, "Free shipping", "Please add it")]
    assert webhooks.route("issues", issue_event(issue_labels=())) == []


def test_labeling_an_existing_issue_agent_starts_it_but_other_labels_dont():
    assert webhooks.route("issues", issue_event("labeled", label="agent"))[0].kind == "issue"
    assert webhooks.route("issues", issue_event("labeled", issue_labels=("agent", "bug"), label="bug")) == []


def test_a_bump_pr_starts_the_dependency_repair():
    by_label = webhooks.route("pull_request", pr_event(pr_labels=("dependencies",)))
    by_title = webhooks.route("pull_request", pr_event(title="Bump orbit-money from 1.0.0 to 2.0.0"))
    assert by_label[0].kind == by_title[0].kind == "dependency"


def test_any_other_human_pr_gets_a_review():
    assert webhooks.route("pull_request", pr_event()) == [StartRun("review", "agentic-sdlc", 13, "Add a bulk discount", "10% off")]


def test_drafts_and_the_pipelines_own_prs_are_left_alone():
    assert webhooks.route("pull_request", pr_event(draft=True)) == []
    assert webhooks.route("pull_request", pr_event(body=f"Closes #1\n{BOT_MARKER}")) == []
    assert webhooks.route("issues", issue_event(body=f"Rolled back\n{BOT_MARKER}")) == []


def test_scenario_prs_start_when_their_owner_label_lands_not_when_they_open():
    """The bump Action and the sample PR label the PR just after opening it."""
    head = "dependency-bump/orbit-money-2.0.0-99"
    assert webhooks.route("pull_request", pr_event(head=head)) == []
    labeled = pr_event("labeled", pr_labels=("dependencies", "agentic-sdlc:agentic-sdlc"), head=head,
                       title="Bump orbit-money from 1.0.0 to 2.0.0", label="agentic-sdlc:agentic-sdlc")
    assert webhooks.route("pull_request", labeled)[0].kind == "dependency"
    other_label = pr_event("labeled", pr_labels=("dependencies",), head=head, label="dependencies")
    assert webhooks.route("pull_request", other_label) == []
    # Labeling a person's PR doesn't start a second review.
    assert webhooks.route("pull_request", pr_event("labeled", label="agentic-sdlc:agentic-sdlc")) == []


def test_events_from_other_repos_are_ignored():
    event = issue_event()
    event["repository"]["full_name"] = "someone/else"
    assert webhooks.route("issues", event) == []


# -- Which copy of the demo owns what -----------------------------------------------------


def test_hosted_copy_owns_its_label_workspace_labels_and_unlabeled_items():
    assert webhooks.owner_queue([]) == "agentic-sdlc"
    assert webhooks.owner_queue(["agentic-sdlc:agentic-sdlc"]) == "agentic-sdlc"
    # A crashable workspace's run starts on the workspace's own queue.
    assert webhooks.owner_queue(["agentic-sdlc:agentic-sdlc-ws-7f3a"]) == "agentic-sdlc-ws-7f3a"
    assert webhooks.owner_queue(["agentic-sdlc:local-maya"]) is None
    assert webhooks.owner_queue(["agentic-sdlc:agentic-sdlc-BAD QUEUE"]) is None


def test_a_laptop_owns_only_its_own_label(monkeypatch):
    laptop(monkeypatch)
    assert webhooks.owner_queue(["agentic-sdlc:local-maya"]) == "agentic-sdlc"
    assert webhooks.owner_queue([]) is None
    assert webhooks.owner_queue(["agentic-sdlc:agentic-sdlc"]) is None
    assert webhooks.owner_queue(["agentic-sdlc:local-sam"]) is None


def test_an_item_owned_by_another_copy_starts_nothing_here():
    assert webhooks.route("issues", issue_event(issue_labels=("agent", "agentic-sdlc:local-sam"))) == []


# -- Comments and closed PRs reach the runs -----------------------------------------------


def test_a_comment_on_an_issue_signals_the_runs_on_that_issue():
    [action] = webhooks.route("issue_comment", comment_event())
    assert (action.ref, action.signal) == ("issue:12", "github_comment")
    assert action.arg == HumanComment(id="issue:7", number=12, author="maya", body="Looks good",
                                      url="https://x/c/7", created_at="2026-10-26T10:00:00Z")


def test_a_conversation_comment_on_a_pr_signals_the_runs_on_that_pr():
    assert webhooks.route("issue_comment", comment_event(on_pr=True, number=13))[0].ref == "pr:13"


def test_inline_review_comments_say_which_file():
    event = {"action": "created", "repository": {"full_name": REPO}, "pull_request": {"number": 13},
             "comment": {"id": 8, "body": "Round here", "path": "src/cart.js", "user": {"login": "sam"},
                         "html_url": "u", "created_at": "t"}}
    [action] = webhooks.route("pull_request_review_comment", event)
    assert (action.ref, action.arg.id, action.arg.body) == ("pr:13", "review-comment:8", "(on src/cart.js) Round here")


def test_a_review_with_a_body_is_a_comment_and_a_bare_approval_is_not():
    review = {"action": "submitted", "repository": {"full_name": REPO}, "pull_request": {"number": 13},
              "review": {"id": 9, "body": "Needs tests", "user": {"login": "sam"}, "html_url": "u",
                         "submitted_at": "2026-10-26T10:00:00Z"}}
    assert webhooks.route("pull_request_review", review)[0].arg.created_at == "2026-10-26T10:00:00Z"
    review["review"]["body"] = None
    assert webhooks.route("pull_request_review", review) == []


def test_the_bots_own_comments_are_ignored():
    assert webhooks.route("issue_comment", comment_event(body=f"Plan\n{BOT_MARKER}")) == []


def test_a_closed_pr_signals_its_runs():
    assert webhooks.route("pull_request", pr_event("closed")) == [SignalRuns("pr:13", "pull_request_closed", 13)]


# -- Carrying it out against Temporal -----------------------------------------------------


async def defaults(_task_queue: str) -> RunSettings:
    return RunSettings()



@workflow.defn(name="PullRequestReview", sandboxed=False)
class FakeReview:
    def __init__(self) -> None:
        self.comments: list[str] = []
        self.closed = False

    @workflow.run
    async def run(self, request: RunRequest) -> list[str]:
        workflow.upsert_search_attributes([GITHUB_REFS.value_set([f"pr:{request.pr_number}"])])
        await workflow.wait_condition(lambda: self.closed)
        return self.comments

    @workflow.signal
    def github_comment(self, comment: HumanComment) -> None:
        self.comments.append(comment.body)

    @workflow.signal
    def pull_request_closed(self, _number: int) -> None:
        self.closed = True


@pytest.fixture
async def temporal():
    async with await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter,
                                                      search_attributes=[GITHUB_REFS]) as env:
        yield env.client


async def test_a_redelivered_pr_event_starts_one_run_and_comments_reach_it(temporal, monkeypatch):
    queue = f"test-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(config, "TASK_QUEUE", queue)
    async with Worker(temporal, task_queue=queue, workflows=[FakeReview]):
        start = webhooks.route("pull_request", pr_event())
        start = [StartRun(a.kind, queue, a.number, a.title, a.body) for a in start]
        assert await webhooks.handle(temporal, start, defaults) == [f"started {queue}.gh.review-13"]
        assert await webhooks.handle(temporal, start, defaults) == [f"started {queue}.gh.review-13"]  # same run, not a second
        handle = temporal.get_workflow_handle(f"{queue}.gh.review-13")
        await _until_visible(temporal, "pr:13")
        await webhooks.handle(temporal, webhooks.route("issue_comment", comment_event("Fix the rounding", on_pr=True, number=13)),
                             defaults)
        await webhooks.handle(temporal, webhooks.route("pull_request", pr_event("closed")), defaults)
        assert await handle.result() == ["Fix the rounding"]
        # Redelivered after the run ended: still nothing new.
        assert await webhooks.handle(temporal, start, defaults) == ["already done review-13"]


async def test_a_run_started_with_no_worker_is_listed_and_reachable(temporal, monkeypatch):
    """The presenter may have the worker killed when GitHub's webhook arrives."""
    queue = f"test-{uuid.uuid4().hex[:6]}"
    await webhooks.handle(temporal, [StartRun("issue", queue, 12, "Free shipping", "")], defaults)
    await _until_visible(temporal, "issue:12", queue)
    handle = temporal.get_workflow_handle(f"{queue}.gh.issue-12")
    run = await (await handle.describe()).memo_value("run")
    assert (run["run_id"], run["kind"], run["title"]) == ("issue-12", "issue", "Free shipping")
    await handle.terminate()


async def test_over_the_run_cap_nothing_starts_and_the_agent_says_so(temporal, monkeypatch):
    queue = f"test-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(config, "TASK_QUEUE", queue)
    monkeypatch.setattr(config, "MAX_ACTIVE_GITHUB_RUNS", 1)
    said: list[CommentInput] = []

    @activity.defn(name="comment_on_issue")
    async def comment_on_issue(input: CommentInput) -> None:
        said.append(input)

    async with Worker(temporal, task_queue=queue, workflows=[FakeReview], activities=[comment_on_issue]):
        await webhooks.handle(temporal, [StartRun("review", queue, 13, "First", "")], defaults)
        await _until_visible(temporal, "pr:13", queue)
        result = await webhooks.handle(temporal, [StartRun("review", queue, 14, "Second", "")], defaults)
        assert result == ["skipped review-14: 1 runs already active"]
        busy = temporal.get_activity_handle(f"{queue}.gh.busy-review-14")
        await busy.result()
        assert said[0].issue_number == 14 and "limit" in said[0].body
        await temporal.get_workflow_handle(f"{queue}.gh.review-13").terminate()


async def _until_visible(temporal, ref: str, queue: str | None = None) -> None:
    """Visibility is eventually consistent; wait until the webhook's query can see the run."""
    import asyncio

    query = f"AgenticSdlcGitHubRefs = '{ref}'" + (f" AND WorkflowId STARTS_WITH '{queue}.gh.'" if queue else "")
    for _ in range(50):
        if [w async for w in temporal.list_workflows(query)]:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{ref} never became visible")
