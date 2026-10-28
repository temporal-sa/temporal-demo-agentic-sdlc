"""Browser sessions and the slot each one leases (see agentic_sdlc/slots.py).

A browser holds a signed cookie with a random session ID and a hint: the slot it last claimed.
The cookie identifies the browser and nothing else. Whether a browser holds a slot is always
answered by the slot's own Workflow, and a browser gets one only by starting a scenario.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from fastapi import Request, Response
from temporalio.client import Client, WithStartWorkflowOperation, WorkflowExecutionStatus, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from agentic_sdlc import config
from agentic_sdlc.slots import Slot, SlotInput, SlotState

COOKIE = "agentic-session"
COOKIE_MAX_AGE = 24 * 3600
LOOKUP_CACHE_SECONDS = 1.0


def _signature(payload: str) -> str:
    # Keyed from the webhook secret, which every web server already has, so the catalog needs no
    # new secret; the context string keeps the key from doing anything else.
    key = hmac.new(config.GITHUB_WEBHOOK_SECRET.encode(), b"agentic-sdlc session cookie", hashlib.sha256).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]


@dataclass
class Session:
    sid: str
    hint: int = 0  # the slot this browser last claimed; 0 for none
    set_cookie: bool = False  # the cookie is new or has changed


def load(request: Request) -> Session:
    """The browser's session. A cookie that is missing or doesn't verify is replaced."""
    raw = request.cookies.get(COOKIE, "")
    sid, _, rest = raw.partition(".")
    hint, _, signature = rest.partition(".")
    if sid and hint.isdigit() and hmac.compare_digest(signature, _signature(f"{sid}.{hint}")):
        return Session(sid, int(hint))
    return Session(secrets.token_urlsafe(16), 0, set_cookie=True)


def issue(request: Request, response: Response, session: Session) -> None:
    payload = f"{session.sid}.{session.hint}"
    response.set_cookie(
        COOKIE, f"{payload}.{_signature(payload)}", max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax",
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
    )


@dataclass(frozen=True)
class Lease:
    n: int
    state: SlotState

    @property
    def queue(self) -> str:
        return config.slot_queue(self.n)

    @property
    def owner_label(self) -> str:
        return f"{config.OWNER_LABEL_PREFIX}{self.queue}"


class NoFreeSlot(Exception):
    pass


class LeaseLost(Exception):
    """The slot expired or was cleared between the lookup and the action."""


async def act(temporal: Client, session: Session, lease: Lease) -> Lease:
    """Record a demo action on the lease, which pushes back its idle expiry. Polling never calls this."""
    try:
        state = await temporal.get_workflow_handle(config.slot_workflow_id(lease.n)).execute_update(
            Slot.touch, session.sid, result_type=SlotState)
    except (WorkflowUpdateFailedError, RPCError) as err:
        forget(session)
        raise LeaseLost from err
    lease = Lease(lease.n, state)
    _lookups[session.sid] = (time.monotonic(), lease)
    return lease


_lookups: dict[str, tuple[float, Lease | None]] = {}


async def slot_state(temporal: Client, n: int, copy: str | None = None) -> SlotState | None:
    """What the slot's Workflow says, or None if the slot is free (no Workflow running)."""
    try:
        description = await temporal.get_workflow_handle(config.slot_workflow_id(n, copy)).describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            return None
        raise
    if description.status != WorkflowExecutionStatus.RUNNING:
        return None
    return SlotState.model_validate(await description.memo_value("slot", default={}))


async def find(temporal: Client, session: Session) -> Lease | None:
    """The slot this browser holds right now, if any. Cached for a second so that a console's
    polls share one Describe."""
    cached = _lookups.get(session.sid)
    if cached and time.monotonic() - cached[0] < LOOKUP_CACHE_SECONDS:
        return cached[1]
    lease = None
    if 1 <= session.hint <= config.SESSION_SLOTS:
        state = await slot_state(temporal, session.hint)
        if state and state.phase == "owned" and state.owner == session.sid:
            lease = Lease(session.hint, state)
    _lookups[session.sid] = (time.monotonic(), lease)
    return lease


def forget(session: Session) -> None:
    _lookups.pop(session.sid, None)


def remember(session: Session, lease: Lease) -> None:
    _lookups[session.sid] = (time.monotonic(), lease)


async def claim(temporal: Client, session: Session) -> Lease:
    """Keep the slot this browser holds, or take a free one. The slot's Workflow decides: its
    validator rejects a claim on a slot someone else holds, so two browsers can't both win."""
    if lease := await find(temporal, session):
        return await act(temporal, session, lease)
    running = {n async for n in _running_slots(temporal)}
    candidates = [session.hint] if 1 <= session.hint <= config.SESSION_SLOTS else []
    candidates += [n for n in range(1, config.SESSION_SLOTS + 1) if n not in running and n != session.hint]
    for n in candidates:
        start = WithStartWorkflowOperation(
            Slot.run, SlotInput(queue=config.slot_queue(n), repo=config.GITHUB_SANDBOX_REPO),
            id=config.slot_workflow_id(n), task_queue=config.host_queue(config.TASK_QUEUE),
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        try:
            state = await temporal.execute_update_with_start_workflow(Slot.claim, session.sid, start_workflow_operation=start)
        except WorkflowUpdateFailedError:
            continue  # held by someone else, or being cleared: try the next
        session.hint, session.set_cookie = n, True
        lease = Lease(n, state)
        _lookups[session.sid] = (time.monotonic(), lease)
        return lease
    raise NoFreeSlot


async def _running_slots(temporal: Client):
    prefix = f"{config.TASK_QUEUE}.slot-"
    async for execution in temporal.list_workflows(f"WorkflowId STARTS_WITH '{prefix}' AND ExecutionStatus = 'Running'"):
        yield int(execution.id.removeprefix(prefix))
