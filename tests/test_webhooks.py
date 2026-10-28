"""GitHub webhooks: signatures, which copy of the demo acts on what, and what it does.

``route`` is pure, so most of this needs no server. The last tests carry actions out against a
local Temporal dev server: redelivery idempotence, Signals reaching the right run, the run cap.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
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
from agentic_sdlc.slots import SlotInput, SlotState
from agentic_sdlc.web import sessions, webhooks
from agentic_sdlc.web.webhooks import RunSettings, SignalRuns, StartRun

REPO = "temporal-sa/agentic-sdlc-storefront"
SECRET = "test-secret"
SLOT = "agentic-sdlc--s01"
LABEL = f"agentic-sdlc:{SLOT}"
OPENED = "2026-10-28T12:00:00Z"
OPENED_AT = 1793188800.0  # the same moment, in epoch seconds


@pytest.fixture(autouse=True)
def hosted(monkeypatch):
    """The hosted copy by default; tests that need a laptop or a workspace change it."""
    monkeypatch.setattr(config, "GITHUB_SANDBOX_REPO", REPO)
    monkeypatch.setattr(config, "GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "TASK_QUEUE", "agentic-sdlc")
    monkeypatch.setattr(config, "BASE_TASK_QUEUE", "agentic-sdlc")
    monkeypatch.setattr(config, "OWNER_LABEL", "agentic-sdlc:agentic-sdlc")
    monkeypatch.setattr(config, "IS_LOCAL_COPY", False)
    monkeypatch.setattr(config, "SESSION_SLOTS", 8)


def laptop(monkeypatch, name="local-maya"):
    monkeypatch.setattr(config, "TASK_QUEUE", name)
    monkeypatch.setattr(config, "OWNER_LABEL", f"agentic-sdlc:{name}")
    monkeypatch.setattr(config, "IS_LOCAL_COPY", True)


def labels(*names):
    return [{"name": n} for n in names]


def issue_event(action="opened", issue_labels=("agent", LABEL), body="Please add it", label=None, number=12,
                created_at=OPENED):
    payload = {"action": action, "repository": {"full_name": REPO},
               "issue": {"number": number, "title": "Free shipping", "body": body, "labels": labels(*issue_labels),
                         "created_at": created_at}}
    if label:
        payload["label"] = {"name": label}
    return payload


def pr_event(action="opened", pr_labels=(LABEL,), title="Add a bulk discount", head="feature/bulk", body="10% off",
             draft=False, label=None, number=13, created_at=OPENED):
    payload = {"action": action, "repository": {"full_name": REPO},
               "pull_request": {"number": number, "title": title, "body": body, "draft": draft,
                                "labels": labels(*pr_labels), "head": {"ref": head}, "created_at": created_at}}
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
    assert webhooks.route("issues", issue_event()) == [
        StartRun("issue", SLOT, 12, "Free shipping", "Please add it", created_at=OPENED)]
    assert webhooks.route("issues", issue_event(issue_labels=(LABEL,))) == []  # no `agent` label


def test_labeling_an_existing_issue_agent_starts_it_but_other_labels_dont():
    assert webhooks.route("issues", issue_event("labeled", label="agent"))[0].kind == "issue"
    assert webhooks.route("issues", issue_event("labeled", issue_labels=("agent", "bug", LABEL), label="bug")) == []


def test_a_bump_pr_starts_the_dependency_repair():
    by_label = webhooks.route("pull_request", pr_event(pr_labels=("dependencies", LABEL)))
    by_title = webhooks.route("pull_request", pr_event(title="Bump orbit-money from 1.0.0 to 2.0.0"))
    assert by_label[0].kind == by_title[0].kind == "dependency"


def test_any_other_human_pr_gets_a_review():
    assert webhooks.route("pull_request", pr_event()) == [
        StartRun("review", SLOT, 13, "Add a bulk discount", "10% off", created_at=OPENED)]


def test_drafts_and_the_pipelines_own_prs_are_left_alone():
    assert webhooks.route("pull_request", pr_event(draft=True)) == []
    assert webhooks.route("pull_request", pr_event(body=f"Closes #1\n{BOT_MARKER}")) == []
    assert webhooks.route("issues", issue_event(body=f"Rolled back\n{BOT_MARKER}")) == []


def test_scenario_prs_start_once_their_owner_label_is_known():
    """The sample PR is labeled just after it opens; a bump PR opened from GitHub's page has its labels at once."""
    head = "dependency-bump/orbit-money-2.0.0-99"
    assert webhooks.route("pull_request", pr_event(head=head, pr_labels=())) == []
    labeled = pr_event("labeled", pr_labels=("dependencies", LABEL), head=head,
                       title="Bump orbit-money from 1.0.0 to 2.0.0", label=LABEL)
    assert webhooks.route("pull_request", labeled)[0].kind == "dependency"
    other_label = pr_event("labeled", pr_labels=("dependencies", LABEL), head=head, label="dependencies")
    assert webhooks.route("pull_request", other_label) == []
    # Opened from GitHub's prefilled page, it carries its labels from the start.
    opened_labeled = pr_event(pr_labels=("dependencies", LABEL), head=head,
                              title="Bump orbit-money from 1.0.0 to 2.0.0")
    assert webhooks.route("pull_request", opened_labeled)[0].kind == "dependency"
    # Labeling a person's PR doesn't start a second review.
    assert webhooks.route("pull_request", pr_event("labeled", label=LABEL)) == []


def test_events_from_other_repos_are_ignored():
    event = issue_event()
    event["repository"]["full_name"] = "someone/else"
    assert webhooks.route("issues", event) == []


# -- Which copy of the demo owns what -----------------------------------------------------


def test_hosted_copy_owns_its_slots_and_crashable_workspaces_slots():
    assert webhooks.owner_queue([LABEL]) == SLOT
    # A crashable workspace's run starts on the workspace's own slot queue.
    assert webhooks.owner_queue(["agentic-sdlc:agentic-sdlc-ws-7f3a--s02"]) == "agentic-sdlc-ws-7f3a--s02"
    assert webhooks.owner_queue(["agentic-sdlc:local-maya--s01"]) is None
    assert webhooks.owner_queue(["agentic-sdlc:agentic-sdlc-BAD QUEUE--s01"]) is None


def test_items_nobody_owns_start_nothing():
    assert webhooks.owner_queue([]) is None  # a person on GitHub opened it directly
    assert webhooks.owner_queue(["agentic-sdlc:agentic-sdlc"]) is None  # the copy itself isn't a slot
    assert webhooks.owner_queue(["agentic-sdlc:agentic-sdlc--s09"]) is None  # past the pool of 8
    assert webhooks.owner_queue(["agentic-sdlc:agentic-sdlc--s00"]) is None
    assert webhooks.route("issues", issue_event(issue_labels=("agent",))) == []
    assert webhooks.route("pull_request", pr_event(pr_labels=())) == []


def test_a_laptop_owns_only_its_own_slots(monkeypatch):
    laptop(monkeypatch)
    assert webhooks.owner_queue(["agentic-sdlc:local-maya--s02"]) == "local-maya--s02"
    assert webhooks.owner_queue([]) is None
    assert webhooks.owner_queue([LABEL]) is None  # the hosted copy's slot
    assert webhooks.owner_queue(["agentic-sdlc:agentic-sdlc-ws-7f3a--s01"]) is None
    assert webhooks.owner_queue(["agentic-sdlc:local-sam--s01"]) is None


def test_an_item_owned_by_another_copy_starts_nothing_here():
    assert webhooks.route("issues", issue_event(issue_labels=("agent", "agentic-sdlc:local-sam--s01"))) == []


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


def test_a_long_bot_comment_is_ignored_too():
    """The marker is at the end; the body is shortened only after the check."""
    long_plan = "### Plan\n" + "- a long acceptance criterion\n" * 400 + f"\n{BOT_MARKER}"
    assert len(long_plan) > 4000
    assert webhooks.route("issue_comment", comment_event(body=long_plan)) == []


def test_the_marker_leads_every_bot_comment():
    from agentic_sdlc.activities.github import _bot

    long_plan = _bot("### Plan\n" + "- criterion\n" * 500, "Planner agent")
    assert long_plan[:4000].startswith(BOT_MARKER)
    assert _bot(long_plan, "Planner agent") == long_plan  # marking twice changes nothing
    assert webhooks.route("issue_comment", comment_event(body=long_plan)) == []


def test_comments_say_which_agent_or_the_pipeline_wrote_them():
    """The demo posts with its PAT, so on GitHub the author is always the token's owner."""
    from agentic_sdlc.activities.github import _bot

    planner = _bot("Here's the plan.", "Planner agent").splitlines()
    assert planner[1] == "🤖 **Planner agent**"
    assert "posted by the agentic sdlc demo" in planner[-1].lower()
    assert _bot("Staging passed.", "Pipeline").splitlines()[1] == "⚙️ **Pipeline**"


def test_the_catch_up_read_skips_long_bot_comments():
    from agentic_sdlc.activities.github import _human

    long_plan = "### Plan\n" + "- criterion\n" * 500 + f"\n{BOT_MARKER}"
    raw = {"id": 1, "body": long_plan, "user": {"login": "maya"}, "html_url": "u", "created_at": "t"}
    assert _human(raw, 12, "issue") is None
    assert _human({**raw, "body": "A person's reply"}, 12, "issue").body == "A person's reply"


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


@workflow.defn(name="Slot", sandboxed=False)
class FakeSlot:
    """A slot someone holds since OPENED_AT - 60. The webhook only reads its memo and signals it."""

    def __init__(self) -> None:
        self.activity = 0

    @workflow.run
    async def run(self, input: SlotInput) -> None:
        held = SlotState(phase="owned", owner="alice", claimed_at=OPENED_AT - 60, last_action_at=OPENED_AT - 60)
        workflow.upsert_memo({"slot": held.model_dump(mode="json")})
        await workflow.wait_condition(lambda: False)

    @workflow.signal
    def github_activity(self, _at: float) -> None:
        self.activity += 1

    @workflow.query
    def seen_activity(self) -> int:
        return self.activity


@pytest.fixture
async def temporal():
    async with await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter,
                                                      search_attributes=[GITHUB_REFS]) as env:
        yield env.client


@pytest.fixture
def copy(monkeypatch) -> str:
    """A copy of the demo with its own queues, so tests sharing a server never see each other."""
    name = f"test-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(config, "TASK_QUEUE", name)
    return name


@contextlib.asynccontextmanager
async def leased_slots(temporal, copy: str, *numbers: int, copy_activities=()):
    """Slots someone holds, each with a Worker for its runs, plus the copy's own queue."""
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(Worker(temporal, task_queue=config.host_queue(copy), workflows=[FakeSlot]))
        if copy_activities:
            await stack.enter_async_context(Worker(temporal, task_queue=copy, activities=list(copy_activities)))
        for n in numbers:
            await stack.enter_async_context(Worker(temporal, task_queue=config.slot_queue(n, copy), workflows=[FakeReview]))
            await temporal.start_workflow(FakeSlot.run, SlotInput(queue=config.slot_queue(n, copy), repo=REPO),
                                          id=config.slot_workflow_id(n, copy), task_queue=config.host_queue(copy))
            while (held := await sessions.slot_state(temporal, n, copy)) is None or held.phase != "owned":
                await asyncio.sleep(0.05)  # its first task has to publish the lease
        yield [config.slot_queue(n, copy) for n in numbers]
        for n in numbers:
            await temporal.get_workflow_handle(config.slot_workflow_id(n, copy)).terminate()


def starting(slot_queue: str, number: int = 13, created_at: str = OPENED) -> list[StartRun]:
    return [StartRun("review", slot_queue, number, "Add a bulk discount", "10% off", created_at=created_at)]


async def test_a_redelivered_pr_event_starts_one_run_and_comments_reach_it(temporal, copy):
    async with leased_slots(temporal, copy, 1) as [slot]:
        start = starting(slot)
        assert await webhooks.handle(temporal, start, defaults) == [f"started {slot}.gh.review-13"]
        assert await webhooks.handle(temporal, start, defaults) == [f"started {slot}.gh.review-13"]  # same run, not a second
        handle = temporal.get_workflow_handle(f"{slot}.gh.review-13")
        await _until_visible(temporal, "pr:13")
        await webhooks.handle(temporal, webhooks.route("issue_comment", comment_event("Fix the rounding", on_pr=True, number=13)),
                             defaults)
        await webhooks.handle(temporal, webhooks.route("pull_request", pr_event("closed")), defaults)
        assert await handle.result() == ["Fix the rounding"]
        # Redelivered after the run ended: still nothing new.
        assert await webhooks.handle(temporal, start, defaults) == ["already done review-13"]


async def test_a_run_started_with_no_worker_is_listed_and_reachable(temporal, copy):
    """The presenter may have the worker crashed when GitHub's webhook arrives."""
    async with leased_slots(temporal, copy, 1) as [slot]:
        await webhooks.handle(temporal, [StartRun("issue", slot, 12, "Free shipping", "", created_at=OPENED)], defaults)
        await _until_visible(temporal, "issue:12", slot)
        handle = temporal.get_workflow_handle(f"{slot}.gh.issue-12")
        run = await (await handle.describe()).memo_value("run")
        assert (run["run_id"], run["kind"], run["title"]) == ("issue-12", "issue", "Free shipping")
        await handle.terminate()


async def test_an_item_for_a_slot_nobody_holds_starts_nothing(temporal, copy):
    async with leased_slots(temporal, copy, 1):
        free = config.slot_queue(2, copy)
        assert await webhooks.handle(temporal, starting(free), defaults) == ["skipped review-13: its slot isn't in use"]
        assert [w async for w in temporal.list_workflows(f"WorkflowId STARTS_WITH '{free}.gh.'")] == []


async def test_an_item_opened_before_the_current_session_started_nothing(temporal, copy):
    """A reused slot never inherits the last session's issues, even if someone reopens one."""
    async with leased_slots(temporal, copy, 1) as [slot]:
        result = await webhooks.handle(temporal, starting(slot, created_at="2026-10-28T11:00:00Z"), defaults)
        assert result == ["skipped review-13: it was opened before the slot's current session began"]
        # An item opened just before the claim, within GitHub's one second resolution, still counts.
        just_before = "2026-10-28T11:59:00Z"  # the claim was 60 seconds before OPENED
        assert (await webhooks.handle(temporal, starting(slot, created_at=just_before), defaults))[0].startswith("started")
        await temporal.get_workflow_handle(f"{slot}.gh.review-13").terminate()


async def test_github_activity_on_a_run_keeps_its_slots_lease_alive(temporal, copy):
    async with leased_slots(temporal, copy, 1) as [slot]:
        slot_handle = temporal.get_workflow_handle(config.slot_workflow_id(1, copy))
        await webhooks.handle(temporal, starting(slot), defaults)
        assert await slot_handle.query(FakeSlot.seen_activity) == 1  # starting a run is activity
        await _until_visible(temporal, "pr:13", slot)
        await webhooks.handle(temporal, webhooks.route("issue_comment", comment_event("Hi", on_pr=True, number=13)), defaults)
        assert await slot_handle.query(FakeSlot.seen_activity) == 2
        await temporal.get_workflow_handle(f"{slot}.gh.review-13").terminate()


async def test_over_the_slots_run_cap_nothing_starts_and_the_agent_says_so(temporal, copy, monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_GITHUB_RUNS", 1)
    said: list[CommentInput] = []

    @activity.defn(name="comment_on_issue")
    async def comment_on_issue(input: CommentInput) -> None:
        said.append(input)

    async with leased_slots(temporal, copy, 1, copy_activities=[comment_on_issue]) as [slot]:
        await webhooks.handle(temporal, starting(slot, 13), defaults)
        await _until_visible(temporal, "pr:13", slot)
        result = await webhooks.handle(temporal, starting(slot, 14), defaults)
        assert result == ["skipped review-14: 1 runs active in the slot, 1 in all"]
        # The comment runs on the copy's own queue, so it is said even while the slot's Workers are down.
        await temporal.get_activity_handle(f"{slot}.gh.busy-review-14").result()
        assert said[0].issue_number == 14 and "too many" in said[0].body
        await temporal.get_workflow_handle(f"{slot}.gh.review-13").terminate()


async def test_the_copy_as_a_whole_is_capped_across_its_slots(temporal, copy, monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_RUNS_ALL_SLOTS", 1)

    @activity.defn(name="comment_on_issue")
    async def comment_on_issue(input: CommentInput) -> None:
        pass

    async with leased_slots(temporal, copy, 1, 2, copy_activities=[comment_on_issue]) as [first, second]:
        await webhooks.handle(temporal, starting(first, 13), defaults)
        await _until_visible(temporal, "pr:13", first)
        result = await webhooks.handle(temporal, starting(second, 14), defaults)
        assert result == ["skipped review-14: 0 runs active in the slot, 1 in all"]
        await temporal.get_workflow_handle(f"{first}.gh.review-13").terminate()


async def _until_visible(temporal, ref: str, queue: str | None = None) -> None:
    """Visibility is eventually consistent; wait until the webhook's query can see the run."""
    query = f"AgenticSdlcGitHubRefs = '{ref}'" + (f" AND WorkflowId STARTS_WITH '{queue}.gh.'" if queue else "")
    for _ in range(50):
        if [w async for w in temporal.list_workflows(query)]:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{ref} never became visible")
