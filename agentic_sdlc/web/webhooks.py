"""GitHub webhooks: which deliveries this copy of the demo acts on, and what it does with them.

GitHub sends every event on the storefront repo to every webhook registered on it: the hosted
demo's, and a laptop's `gh webhook forward` when someone develops locally. Issues and PRs carry
a label naming the session slot that owns them (``agentic-sdlc:<copy>--s03``), so each copy acts
on its own slots' items and drops the rest:

- the hosted copy owns its slots and crashable workspaces' slots (it starts their runs on the
  slot's task queue, since GitHub only reaches the hosted URL);
- a laptop owns only its own slots;
- an item with no slot label, which a person opened on the repo directly, is nobody's.

A slot's label only counts while the slot is leased, and only for items opened after the lease
began, so a reused slot never inherits the last session's issues.

``route`` turns a delivery into Actions: start a run, Signal the runs watching an issue or PR, or
nothing. ``handle`` carries them out against Temporal.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import re
import time
from datetime import datetime
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field
from temporalio.client import Client
from temporalio.common import (
    SearchAttributeKey,
    SearchAttributePair,
    TypedSearchAttributes,
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import (
    ActivityAlreadyStartedError,
    WorkflowAlreadyStartedError,
)
from temporalio.service import RPCError

from agentic_sdlc import config
from agentic_sdlc.activities.github import BOT_MARKER, CommentInput, HumanComment
from agentic_sdlc.models import Autonomy, RoleModels, RunKind, RunRequest, RunState
from agentic_sdlc.pipeline import dependency, review, workflow
from agentic_sdlc.slots import Slot
from agentic_sdlc.web import sessions

log = logging.getLogger(__name__)

# Same key as pipeline/base.py; not imported from there to keep Workflow code out of the web server.
GITHUB_REFS = SearchAttributeKey.for_keyword_list("AgenticSdlcGitHubRefs")

WORKFLOW_FOR_KIND = {"issue": "IssueToProduction", "dependency": "DependencyRepair", "review": "PullRequestReview"}
RUN_ID_FOR_KIND = {"issue": "issue", "dependency": "pr", "review": "review"}
# The console shows a run's steps before a worker has published them.
STEPS_FOR_KIND = {"issue": workflow._initial_steps, "dependency": dependency._steps, "review": review._steps}
BUMP_TITLE = re.compile(r"^[Bb]ump \S+ from \S+ to \S+")
# PRs from the demo's scenario branches (the bump, the sample PR) belong to whichever copy's label
# they carry. A PR opened from GitHub's prefilled page has it when it opens; one opened through the
# API gets it in a second call, after "opened" has fired. Either way, start once the owner is known,
# and never treat one as an unlabeled person's PR.
SCENARIO_BRANCHES = ("dependency-bump/", "review-sample/")
QUEUE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# GitHub stamps items to the second, and the lease to the millisecond; an item the scenario opens
# right after the claim must not look older than it.
LEASE_SKEW_SECONDS = 5


@dataclass(frozen=True)
class StartRun:
    kind: RunKind
    task_queue: str
    number: int  # the issue or PR number
    title: str
    body: str
    url: str = ""  # the issue or PR on GitHub
    created_at: str = ""  # when it was opened, as GitHub says


@dataclass(frozen=True)
class SignalRuns:
    ref: str  # "issue:12" or "pr:13"
    signal: Literal["github_comment", "pull_request_closed"]
    arg: object


Action = StartRun | SignalRuns


class RunSettings(BaseModel):
    """How a copy's runs are set up, chosen in its console: each agent's model, and whether
    merging waits for /merge."""

    models: RoleModels = Field(default_factory=RoleModels)
    autonomy: Autonomy = "auto"


SettingsFor = Callable[[str], Awaitable[RunSettings]]  # by task queue


def verify(secret: str, body: bytes, signature: str | None) -> bool:
    """GitHub signs each delivery with HMAC-SHA256 of the raw body; compare in constant time."""
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def owner_queue(labels: list[str]) -> str | None:
    """The slot queue whose runs should handle an item with these labels, or None if the item
    belongs to another copy of the demo, or to nobody."""
    for label in labels:
        if not label.startswith(config.OWNER_LABEL_PREFIX):
            continue
        queue = label[len(config.OWNER_LABEL_PREFIX):]
        parsed = config.parse_slot_queue(queue)
        if parsed is None:
            continue
        copy, n = parsed
        if copy == config.TASK_QUEUE and n <= config.SESSION_SLOTS:
            return queue
        workspace_prefix = f"{config.BASE_TASK_QUEUE}-"
        if (not config.IS_LOCAL_COPY and copy.startswith(workspace_prefix) and QUEUE_NAME.fullmatch(copy)
                and n <= config.MAX_SESSION_SLOTS):
            return queue  # a crashable workspace's slot; its runs start on the workspace's queue
    return None  # a laptop's, another deployment's, or an item nobody owns


def route(event: str, payload: dict) -> list[Action]:
    """What to do about one webhook delivery. Pure: no I/O."""
    if payload.get("repository", {}).get("full_name") != config.GITHUB_SANDBOX_REPO:
        return []
    action = payload.get("action")

    if event == "issues" and action in ("opened", "labeled"):
        issue = payload["issue"]
        labels = [label["name"] for label in issue.get("labels", [])]
        starting = action == "opened" or payload.get("label", {}).get("name") == "agent"
        if "agent" not in labels or not starting or _from_bot(issue.get("body")):
            return []
        queue = owner_queue(labels)
        return [StartRun("issue", queue, issue["number"], issue["title"], issue.get("body") or "",
                         issue.get("html_url", ""), issue.get("created_at", ""))] if queue else []

    if event == "pull_request" and action in ("opened", "ready_for_review", "reopened", "labeled"):
        pr = payload["pull_request"]
        if pr.get("draft") or _from_bot(pr.get("body")):
            return []  # the pipeline's own PRs carry the bot marker
        labels = [label["name"] for label in pr.get("labels", [])]
        from_scenario = pr["head"]["ref"].startswith(SCENARIO_BRANCHES)
        owned = any(label.startswith(config.OWNER_LABEL_PREFIX) for label in labels)
        if action == "labeled":
            added = payload.get("label", {}).get("name", "")
            if not (from_scenario and added.startswith(config.OWNER_LABEL_PREFIX)):
                return []
        elif from_scenario and not owned:
            return []
        queue = owner_queue(labels)
        if queue is None:
            return []
        kind: RunKind = "dependency" if "dependencies" in labels or BUMP_TITLE.match(pr["title"]) else "review"
        return [StartRun(kind, queue, pr["number"], pr["title"], pr.get("body") or "", pr.get("html_url", ""),
                         pr.get("created_at", ""))]

    if event == "pull_request" and action == "closed":
        number = payload["pull_request"]["number"]
        return [SignalRuns(f"pr:{number}", "pull_request_closed", number)]

    comment = _comment(event, payload)
    if comment is None:
        return []
    ref = f"pr:{comment.number}" if _on_pull_request(event, payload) else f"issue:{comment.number}"
    return [SignalRuns(ref, "github_comment", comment)]


def _comment(event: str, payload: dict) -> HumanComment | None:
    if event == "issue_comment" and payload.get("action") == "created":
        raw, number, kind, file = payload["comment"], payload["issue"]["number"], "issue", None
    elif event == "pull_request_review" and payload.get("action") == "submitted":
        raw, number, kind, file = payload["review"], payload["pull_request"]["number"], "review", None
        if not raw.get("body"):
            return None  # an approval or a review whose content is all inline comments
        raw = {**raw, "created_at": raw.get("submitted_at")}
    elif event == "pull_request_review_comment" and payload.get("action") == "created":
        raw, number, kind, file = payload["comment"], payload["pull_request"]["number"], "review-comment", payload["comment"].get("path")
    else:
        return None
    body = raw.get("body") or ""
    if _from_bot(body):
        return None  # checked before the body is shortened: the marker is at the end of it
    if file:
        body = f"(on {file}) {body}"
    return HumanComment(id=f"{kind}:{raw['id']}", number=number, author=(raw.get("user") or {}).get("login", "someone"),
                        body=body[:4000], url=raw.get("html_url", ""), created_at=raw.get("created_at") or "")


def _on_pull_request(event: str, payload: dict) -> bool:
    return event != "issue_comment" or "pull_request" in payload.get("issue", {})


def _from_bot(body: str | None) -> bool:
    return BOT_MARKER in (body or "")


async def handle(temporal: Client, actions: list[Action], settings_for: SettingsFor) -> list[str]:
    """Carry out the actions; returns what was done, for the delivery's response body."""
    done: list[str] = []
    for action in actions:
        if isinstance(action, StartRun):
            done.append(await _start(temporal, action, settings_for))
        else:
            query = (f"AgenticSdlcGitHubRefs = '{action.ref}' AND ExecutionStatus = 'Running'")
            async for execution in temporal.list_workflows(query):
                await _record_activity(temporal, execution.id.partition(".gh.")[0])
                await temporal.get_workflow_handle(execution.id).signal(action.signal, action.arg)
                done.append(f"signalled {execution.id}")
    return done


async def _record_activity(temporal: Client, slot_queue: str) -> None:
    """Comments on a slot's issues and PRs are the session using the demo, so they keep its lease alive."""
    parsed = config.parse_slot_queue(slot_queue)
    if parsed:
        with contextlib.suppress(RPCError):
            await temporal.get_workflow_handle(config.slot_workflow_id(parsed[1], parsed[0])).signal(
                Slot.github_activity, time.time())


async def _leased_before(temporal: Client, start: StartRun) -> str | None:
    """Why this item mustn't start a run, or None if its slot is leased and it was opened since."""
    parsed = config.parse_slot_queue(start.task_queue)
    state = await sessions.slot_state(temporal, parsed[1], parsed[0]) if parsed else None
    if state is None or state.phase != "owned":
        return "its slot isn't in use"
    try:
        opened = datetime.fromisoformat(start.created_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return "it has no creation time"
    if opened < state.claimed_at - LEASE_SKEW_SECONDS:
        return "it was opened before the slot's current session began"
    return None


async def _start(temporal: Client, start: StartRun, settings_for: SettingsFor) -> str:
    run_id = f"{RUN_ID_FOR_KIND[start.kind]}-{start.number}"
    if skipped := await _leased_before(temporal, start):
        return f"skipped {run_id}: {skipped}"
    await _record_activity(temporal, start.task_queue)
    settings = await settings_for(start.task_queue)
    copy, _ = config.parse_slot_queue(start.task_queue)
    prefix = f"{start.task_queue}.gh."
    running_types = "WorkflowType IN ('IssueToProduction', 'DependencyRepair', 'PullRequestReview')"
    running = [w async for w in temporal.list_workflows(
        f"WorkflowId STARTS_WITH '{prefix}' AND ExecutionStatus = 'Running' AND {running_types}")]
    # Every run spends real model money, so the copy as a whole is capped too, not just each slot.
    everywhere = [w async for w in temporal.list_workflows(
        f"WorkflowId STARTS_WITH '{copy}--s' AND ExecutionStatus = 'Running' AND {running_types}")]
    already = any(w.id == prefix + run_id for w in running)
    if not already and (len(running) >= config.MAX_ACTIVE_GITHUB_RUNS or len(everywhere) >= config.MAX_ACTIVE_RUNS_ALL_SLOTS):
        log.warning("not starting %s: %d runs already active here, %d in all", run_id, len(running), len(everywhere))
        # The web server holds no GitHub token: the owning copy's worker posts the comment, from the
        # copy's own queue so it works while the slot's Workers are crashed.
        with contextlib.suppress(ActivityAlreadyStartedError):  # a redelivery: already said so
            await temporal.start_activity(
                "comment_on_issue", CommentInput(repo=config.GITHUB_SANDBOX_REPO, issue_number=start.number, body=(
                    "This demo has too many agent runs in progress right now, so the agents won't pick "
                    "this up. Close one of the others and reopen this to try again.")),
                id=f"{prefix}busy-{run_id}", task_queue=copy,
                start_to_close_timeout=timedelta(minutes=1),
            )
        return f"skipped {run_id}: {len(running)} runs active in the slot, {len(everywhere)} in all"
    console = config.PUBLIC_URL if copy == config.TASK_QUEUE else None
    request = RunRequest(
        run_id=run_id, kind=start.kind, repo=config.GITHUB_SANDBOX_REPO, title=start.title[:200], body=start.body[:4000],
        issue_number=start.number if start.kind == "issue" else None,
        pr_number=start.number if start.kind != "issue" else None,
        models=settings.models, autonomy=settings.autonomy, budget_usd=config.DEFAULT_BUDGET_USD, console_url=console,
    )
    try:
        await temporal.start_workflow(
            WORKFLOW_FOR_KIND[start.kind], request, id=prefix + run_id, task_queue=start.task_queue,
            # A run waits for people, so it can last days; this bounds an abandoned one, which
            # would otherwise hold one of the copy's run slots forever.
            execution_timeout=timedelta(days=3),
            # Set from the start, so the console lists the run and comments reach it even before a
            # worker has picked it up (say, while the presenter has the worker killed).
            memo={"run": RunState(
                run_id=run_id, kind=start.kind, version="", title=request.title, budget_usd=request.budget_usd,
                started_at=time.time(), steps=STEPS_FOR_KIND[start.kind](),
                issue_url=start.url if start.kind == "issue" else None,
                pr_url=start.url if start.kind != "issue" else None,
            ).model_dump(mode="json")},
            search_attributes=TypedSearchAttributes([SearchAttributePair(
                GITHUB_REFS, [f"{'issue' if start.kind == 'issue' else 'pr'}:{start.number}"])]),
            # A redelivered webhook, or "labeled" after "opened", finds the run already going.
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            # ...and one redelivered after the run ended doesn't start it again.
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        return f"already done {run_id}"
    return f"started {prefix + run_id}"
