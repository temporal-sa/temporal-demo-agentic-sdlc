"""The web server: the presenter console's API, its live stream, and the storefront environments.

It holds no LLM keys and no GitHub token. It starts runs, answers gates and approvals, reads
progress with Describe (free, and available while workers are down), relays agent events
from Redis, and serves each session's staging, production and canary storefront from the
commits the Environments Workflow records.

Every browser session is isolated: Workflow IDs, branches, environments and faults are all
scoped to ``<task queue>.<owner>``, where owner is a hash of the signed-in subject (catalog)
or of a signed cookie (local).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel
from redis.asyncio import Redis
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.harness.agent_client import AgentClient

from agentic_sdlc import config, faults
from agentic_sdlc.agents.registry import allowlist
from agentic_sdlc.models import GateDecision, RoleModels, RunRequest, RunState
from agentic_sdlc.pipeline.environments import EnvState, TrafficBatch
from agentic_sdlc.temporal_client import EVENT_STREAM, connect

ROOT = Path(__file__).resolve().parent.parent.parent
UI_DIST = ROOT / "web" / "dist"
PIPELINE_SOURCE = Path(__file__).resolve().parent.parent / "pipeline" / "workflow.py"
ISSUES = yaml.safe_load((Path(__file__).resolve().parent.parent / "issues.yaml").read_text())

SESSION_COOKIE = "sdlc_session"
RUN_ID = re.compile(r"[a-z0-9-]{4,40}")
KEEPALIVE_SECONDS = 15
VIEW_CACHE_SECONDS = 0.9
ENV_CACHE_SECONDS = 2.0
TRAFFIC_FLUSH_SECONDS = 5
WORKER_HEARTBEAT_PREFIX = f"workers:{config.TASK_QUEUE}:"

_signer = URLSafeSerializer(config.SESSION_SECRET, salt="session")


class State:
    temporal: Client
    redis: Redis
    http: httpx.AsyncClient
    view_cache: dict[str, tuple[float, dict]] = {}
    env_cache: dict[str, tuple[float, EnvState]] = {}
    file_cache: dict[tuple[str, str], tuple[int, bytes, str]] = {}
    traffic: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])


state = State()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.temporal = await connect()
    state.redis = Redis.from_url(config.REDIS_URL)
    state.http = httpx.AsyncClient(timeout=15, follow_redirects=True)
    flusher = asyncio.create_task(_flush_traffic_forever())
    yield
    flusher.cancel()
    await state.http.aclose()


app = FastAPI(title="Agentic SDLC demo", lifespan=lifespan)


@app.middleware("http")
async def server_timing(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
    return response


# -- Session identity ----------------------------------------------------------------------


def owner(request: Request, response: Response) -> str:
    """A stable, opaque owner key for the caller. In the catalog the platform's SSO gate
    supplies the subject; locally each browser gets a signed random cookie."""
    if config.TRUST_AUTH_HEADERS:
        subject = request.headers.get("X-Temporal-Auth-Subject")
        if not subject:
            raise HTTPException(401, "Sign-in required")
    else:
        subject = None
        if cookie := request.cookies.get(SESSION_COOKIE):
            try:
                subject = _signer.loads(cookie)
            except BadSignature:
                subject = None
        if subject is None:
            subject = f"{config.DEV_USER}-{secrets.token_hex(8)}"
            response.set_cookie(SESSION_COOKIE, _signer.dumps(subject), httponly=True, samesite="lax",
                                max_age=60 * 60 * 24 * 30)
    return hashlib.sha256(subject.encode()).hexdigest()[:16]


Owner = Annotated[str, Depends(owner)]


def prefix(owner_key: str) -> str:
    return f"{config.TASK_QUEUE}.{owner_key}."


def run_workflow_id(owner_key: str, run_id: str) -> str:
    if not RUN_ID.fullmatch(run_id):
        raise HTTPException(400, "Bad run id")
    return f"{prefix(owner_key)}run-{run_id}"


def environments_id(owner_key: str) -> str:
    return f"{prefix(owner_key)}environments"


def require_owned(owner_key: str, workflow_id: str) -> str:
    # 404 rather than 403, so other people's IDs aren't confirmed to exist.
    if not workflow_id.startswith(prefix(owner_key)):
        raise HTTPException(404, "Not found")
    return workflow_id


# -- Config and views ----------------------------------------------------------------------


@app.get("/healthz")
async def healthz():
    # Reaching this proves the server started, which it only does after connecting to Temporal.
    return {"ok": True, "task_queue": config.TASK_QUEUE}


@app.get("/api/config")
async def get_config(owner_key: Owner):
    namespace = state.temporal.namespace
    return {
        "owner": owner_key,
        "models": allowlist(require_keys=False),
        "defaults": RoleModels().model_dump(),
        "issues": ISSUES,
        "budget_usd": config.DEFAULT_BUDGET_USD,
        "temporal_ui": f"{config.temporal_ui_url(namespace)}",
        "repo": config.GITHUB_SANDBOX_REPO,
        "local_controls": bool(config.SUPERVISOR_URL),
        "task_queue": config.TASK_QUEUE,
    }


@app.get("/api/source/pipeline")
async def pipeline_source():
    return Response(PIPELINE_SOURCE.read_text(), media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "max-age=60"})


@app.get("/api/session")
async def session_view(owner_key: Owner):
    """The console's landing view: this session's runs, environments, workers."""
    runs, env, workers = await asyncio.gather(_list_runs(owner_key), _environments(owner_key), _workers())
    return {"runs": runs, "environments": env.model_dump(), "workers": workers,
            "apps": f"/apps/{owner_key}"}


async def _list_runs(owner_key: str) -> list[dict]:
    query = f"WorkflowType = 'IssueToProduction' AND WorkflowId STARTS_WITH '{prefix(owner_key)}run-'"
    runs = []
    async for execution in state.temporal.list_workflows(query, limit=20):
        memo = await execution.memo_value("run", default=None)
        if memo:
            runs.append({**_run_summary(memo), "workflow_id": execution.id,
                         "closed": execution.status != WorkflowExecutionStatus.RUNNING})
    return sorted(runs, key=lambda r: r["started_at"], reverse=True)


def _run_summary(memo: dict) -> dict:
    return {k: memo.get(k) for k in ("run_id", "title", "status", "version", "started_at", "ended_at", "usage", "proofs",
                                     "issue_url", "pr_url")}


@app.get("/api/runs/{run_id}")
async def run_view(run_id: str, owner_key: Owner):
    return await _run_view(owner_key, run_wf := run_workflow_id(owner_key, run_id)) or _not_found(run_wf)


async def _run_view(owner_key: str, workflow_id: str) -> dict | None:
    """Everything the run screen shows, from one cached group of free reads. Several viewers
    of the same run share one Describe a second."""
    cached = state.view_cache.get(workflow_id)
    if cached and time.monotonic() - cached[0] < VIEW_CACHE_SECONDS:
        return cached[1]
    try:
        description = await state.temporal.get_workflow_handle(workflow_id).describe()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            return None
        raise
    memo = await description.memo_value("run", default=None)
    if memo is None:
        return None
    run = RunState.model_validate(memo)
    agent_ids = [a.workflow_id for a in run.agents]
    agent_descriptions, env, workers, armed = await asyncio.gather(
        asyncio.gather(*(_describe_quietly(a) for a in agent_ids)),
        _environments(owner_key), _workers(), faults.armed([workflow_id, *agent_ids]),
    )
    retries = [*_retries(workflow_id, description)]
    for agent_id, agent_description in zip(agent_ids, agent_descriptions):
        if agent_description is not None:
            retries.extend(_retries(agent_id, agent_description))
    view = {
        "run": run.model_dump(mode="json"),
        "workflow_id": workflow_id,
        "closed": description.status != WorkflowExecutionStatus.RUNNING,
        "workflow_status": description.status.name if description.status else "UNKNOWN",
        "retries": retries,
        "faults": armed,
        "environments": env.model_dump(),
        "workers": workers,
        "apps": f"/apps/{owner_key}",
        "read_at": time.time(),
    }
    state.view_cache[workflow_id] = (time.monotonic(), view)
    return view


async def _describe_quietly(workflow_id: str):
    try:
        return await state.temporal.get_workflow_handle(workflow_id).describe()
    except RPCError:
        return None


def _retries(workflow_id: str, description) -> list[dict]:
    """Activities that are retrying right now, from Describe's pending-activity list."""
    found = []
    for pending in description.raw_description.pending_activities:
        if pending.attempt > 1 or pending.HasField("last_failure"):
            found.append({
                "workflow_id": workflow_id,
                "activity": pending.activity_type.name,
                "attempt": pending.attempt,
                "last_failure": pending.last_failure.message[:240],
                "next_attempt_in": max(0.0, pending.next_attempt_schedule_time.seconds - time.time())
                if pending.HasField("next_attempt_schedule_time") else None,
            })
    return found


async def _environments(owner_key: str) -> EnvState:
    wf_id = environments_id(owner_key)
    cached = state.env_cache.get(wf_id)
    if cached and time.monotonic() - cached[0] < ENV_CACHE_SECONDS:
        return cached[1]
    env = EnvState()
    description = await _describe_quietly(wf_id)
    if description is not None and description.status == WorkflowExecutionStatus.RUNNING:
        env = EnvState.model_validate(await description.memo_value("environments", default={}))
    state.env_cache[wf_id] = (time.monotonic(), env)
    return env


async def _workers() -> list[dict]:
    """Live workers on this task queue. Each worker refreshes a Redis key every two seconds
    with a short TTL, so a killed worker disappears from the console within seconds."""
    workers = []
    async for key in state.redis.scan_iter(f"{WORKER_HEARTBEAT_PREFIX}*"):
        if raw := await state.redis.get(key):
            workers.append(json.loads(raw))
    return sorted(workers, key=lambda w: w.get("started_at", 0))


def _not_found(_workflow_id: str):
    raise HTTPException(404, "Not found")


# -- Live stream ---------------------------------------------------------------------------


@app.get("/api/runs/{run_id}/stream")
async def run_stream(run_id: str, owner_key: Owner, request: Request):
    """One SSE stream per run view: run snapshots when they change, plus every agent's
    events (model calls, tool calls, approvals) relayed from Redis."""
    workflow_id = run_workflow_id(owner_key, run_id)
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2000)

    async def snapshots():
        last = None
        attached: set[str] = set()
        while True:
            view = await _run_view(owner_key, workflow_id)
            if view is not None:
                body = json.dumps(view, default=str)
                comparable = json.dumps({**view, "read_at": 0}, default=str)
                if comparable != last:
                    last = comparable
                    await queue.put(_sse("run", body))
                for agent in view["run"]["agents"]:
                    if agent["workflow_id"] not in attached:
                        attached.add(agent["workflow_id"])
                        tasks.append(asyncio.create_task(agent_events(agent["workflow_id"])))
            await asyncio.sleep(1.0)

    async def agent_events(agent_id: str):
        async for stored in EVENT_STREAM.subscribe(state.temporal, agent_id, from_offset=0):
            event = stored.data
            payload = _trim(event.event.model_dump(mode="json"))
            if payload.get("type") in _QUIET_EVENTS:
                continue
            await queue.put(_sse("agent", json.dumps({
                "agent_workflow_id": agent_id, "offset": stored.offset, "timestamp": event.timestamp, **payload,
            })))

    tasks: list[asyncio.Task] = [asyncio.create_task(snapshots())]

    async def body():
        try:
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield b": keepalive\n\n"
        finally:
            for task in tasks:
                task.cancel()

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


_QUIET_EVENTS = {"auto_approval_evaluation_started", "auto_approval_evaluation_ended",
                 "auto_approval_evaluation_superseded", "state_snapshot", "state_patch"}


def _sse(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode()


def _trim(value: Any, limit: int = 600) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"… [{len(value) - limit} more]"
    if isinstance(value, dict):
        return {k: _trim(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim(v, limit) for v in value[:50]]
    return value


# -- Writes --------------------------------------------------------------------------------


class StartRun(BaseModel):
    run_id: str
    issue_title: str
    issue_body: str
    models: RoleModels = RoleModels()
    autonomy: Literal["supervised", "auto"] = "auto"
    budget_usd: float | None = None


@app.post("/api/runs")
async def start_run(body: StartRun, owner_key: Owner):
    workflow_id = run_workflow_id(owner_key, body.run_id)
    for role, choice in body.models:
        if choice.model not in allowlist(require_keys=False).get(choice.provider, ()):
            raise HTTPException(400, f"{choice.key} isn't available for the {role}")
    active = [r for r in await _list_runs(owner_key) if not r["closed"]]
    if len(active) >= config.MAX_ACTIVE_RUNS_PER_USER:
        raise HTTPException(429, f"You already have {len(active)} runs in progress. Finish or reset one first.")
    request = RunRequest(
        run_id=body.run_id, owner=owner_key, issue_title=body.issue_title[:200], issue_body=body.issue_body[:4000],
        models=body.models, autonomy=body.autonomy, budget_usd=min(body.budget_usd or config.DEFAULT_BUDGET_USD, 25.0),
    )
    try:
        await state.temporal.start_workflow(
            "IssueToProduction", request, id=workflow_id, task_queue=config.TASK_QUEUE,
            # A run waits for people, so it can last days; this bounds an abandoned one.
            execution_timeout=timedelta(days=3),
        )
    except RPCError as err:
        if err.status != RPCStatusCode.ALREADY_EXISTS:
            raise
    # The write returns what the screen shows next, so the browser needn't make a second call.
    return {"run": RunState(run_id=body.run_id, version="", title=request.issue_title,
                            budget_usd=request.budget_usd, started_at=time.time()).model_dump(mode="json"),
            "workflow_id": workflow_id}


class Decide(BaseModel):
    gate: str
    approve: bool
    feedback: str | None = None
    raise_budget_usd: float | None = None


@app.post("/api/runs/{run_id}/decide")
async def decide(run_id: str, body: Decide, owner_key: Owner):
    workflow_id = run_workflow_id(owner_key, run_id)
    decision = GateDecision(approve=body.approve, feedback=body.feedback, raise_budget_usd=body.raise_budget_usd)
    try:
        run = await state.temporal.get_workflow_handle(workflow_id).execute_update(
            "decide", {"gate": body.gate, "decision": decision.model_dump()}, result_type=RunState,
            rpc_timeout=timedelta(seconds=10),
        )
    except Exception as err:  # noqa: BLE001 - a validator rejection (stale gate) is a 409
        raise HTTPException(409, f"Couldn't record that decision: {err}") from err
    state.view_cache.pop(workflow_id, None)
    return {"run": run.model_dump(mode="json")}


class Approve(BaseModel):
    tool_id: str
    approved: bool


@app.post("/api/agents/{agent_workflow_id}/approve")
async def approve_tool(agent_workflow_id: str, body: Approve, owner_key: Owner):
    require_owned(owner_key, agent_workflow_id)
    agent = AgentClient(state.temporal, agent_workflow_id, event_stream=EVENT_STREAM)
    result = await agent.approve_tool(body.tool_id, approved=body.approved,
                                      reason=None if body.approved else "The presenter denied this command.")
    return {"ok": True, "result": str(result)}


class Fault(BaseModel):
    kind: Literal["model_rate_limit", "github_rate_limit"]
    workflow_id: str
    attempts: int = 2


@app.post("/api/faults")
async def inject_fault(body: Fault, owner_key: Owner):
    require_owned(owner_key, body.workflow_id)
    await faults.arm(body.workflow_id, body.kind, attempts=max(1, min(body.attempts, 4)))
    return {"armed": body.model_dump()}


class BreakCanary(BaseModel):
    seconds: int = 180


@app.post("/api/environments/break-canary")
async def break_canary(body: BreakCanary, owner_key: Owner):
    env = await state.temporal.get_workflow_handle(environments_id(owner_key)).execute_update(
        "break_canary", min(body.seconds, 600), result_type=EnvState)
    state.env_cache.pop(environments_id(owner_key), None)
    return {"environments": env.model_dump()}


@app.post("/api/reset")
async def reset(owner_key: Owner):
    """Back to a clean slate for this session only: stop its runs, empty its environments,
    clear its faults, and clean up its issues, PRs and branches on GitHub."""
    runs = await _list_runs(owner_key)
    for run in runs:
        if not run["closed"]:
            try:
                await state.temporal.get_workflow_handle(run["workflow_id"]).terminate("Reset by presenter")
            except RPCError:
                pass
    try:
        await state.temporal.get_workflow_handle(environments_id(owner_key)).execute_update("reset", result_type=EnvState)
    except RPCError:
        pass
    agent_prefix = [r["workflow_id"] for r in runs]
    await faults.clear(agent_prefix)
    # The web server holds no GitHub token, so the clean-up runs on the worker as a Standalone
    # Activity: no Workflow needed for a one-off job.
    await state.temporal.start_activity(
        "cleanup_runs",
        args=[{"repo": config.GITHUB_SANDBOX_REPO, "branch_prefix": f"runs/{config.TASK_QUEUE}/{owner_key}/",
               "marker": "agentic-sdlc-run:"}],
        id=f"{prefix(owner_key)}cleanup-{int(time.time())}",
        task_queue=config.TASK_QUEUE,
        start_to_close_timeout=timedelta(minutes=5),
        heartbeat_timeout=timedelta(seconds=60),
    )
    state.view_cache.clear()
    state.env_cache.clear()
    return {"ok": True, "stopped": sum(1 for r in runs if not r["closed"])}


# -- Local presenter controls (scripts/dev's supervisor) ------------------------------------


@app.post("/api/worker/{action}")
async def worker_control(action: Literal["kill", "start", "rollout-v2", "rollback-v1"], owner_key: Owner):
    if not config.SUPERVISOR_URL:
        raise HTTPException(409, "No local supervisor. In the catalog, use the workspace's crash controls.")
    response = await state.http.post(f"{config.SUPERVISOR_URL}/{action}")
    return JSONResponse(response.json(), status_code=response.status_code)


# -- Storefront environments ----------------------------------------------------------------


@app.get("/apps/{owner_key}/{env}/{path:path}")
async def serve_storefront(owner_key: str, env: Literal["staging", "prod", "canary"], path: str, request: Request):
    """Serve a session's storefront exactly as committed: files are fetched from GitHub at an
    immutable commit and cached. Agent-written code only ever runs in the visitor's browser."""
    if not re.fullmatch(r"[0-9a-f]{16}", owner_key):
        raise HTTPException(404)
    environments = await _environments(owner_key)
    response = Response()
    sha, is_canary = _route(environments, env, request, response)
    if sha is None:
        return HTMLResponse(_empty_page(env), status_code=200)
    path = path or "index.html"
    synthetic = request.headers.get("X-Synthetic-Check") == "1"
    if is_canary and environments.canary_fault_until > time.time() and path != "index.html":
        # Presenter fault, labeled in the console: the canary's assets fail to load.
        if not synthetic:
            state.traffic[(owner_key, sha)][1] += 1
        return Response("Injected fault: canary asset unavailable", status_code=503)
    status, content, media_type = await _fetch_file(sha, path)
    if is_canary and not synthetic:
        counts = state.traffic[(owner_key, sha)]
        counts[0] += path == "index.html"
        counts[1] += status >= 500
    if path == "index.html" and status == 200:
        content = _badge(content, env, sha, is_canary)
    result = Response(content, status_code=status, media_type=media_type,
                      headers={"X-Commit": sha[:12], "Cache-Control": "no-store"})
    for name, value in response.raw_headers:
        if name == b"set-cookie":  # the canary assignment from _route
            result.raw_headers.append((name, value))
    return result


@app.post("/apps/{owner_key}/{env}/__beacon")
async def storefront_beacon(owner_key: str, env: str, request: Request):
    """Browser errors reported by the storefront's telemetry.js count against the canary."""
    environments = await _environments(owner_key)
    if environments.canary_sha and request.cookies.get(f"canary-{owner_key}") == environments.canary_sha:
        state.traffic[(owner_key, environments.canary_sha)][1] += 1
    return Response(status_code=204)


def _route(env: EnvState, name: str, request: Request, response: Response) -> tuple[str | None, bool]:
    if name == "staging":
        return env.staging_sha, False
    if name == "canary":
        return env.canary_sha, env.canary_sha is not None
    stable = env.stable_sha or config.BASELINE_REF
    if not env.canary_sha:
        return stable, False
    # Production: a sticky share of visitors gets the canary.
    owner_key = request.path_params["owner_key"]
    cookie = f"canary-{owner_key}"
    assigned = request.cookies.get(cookie)
    if assigned not in (env.canary_sha, "stable"):
        assigned = env.canary_sha if secrets.randbelow(100) < env.canary_weight else "stable"
        response.set_cookie(cookie, assigned, max_age=3600, samesite="lax")
    return (env.canary_sha, True) if assigned == env.canary_sha else (stable, False)


async def _fetch_file(ref: str, path: str) -> tuple[int, bytes, str]:
    key = (ref, path)
    if key in state.file_cache:
        return state.file_cache[key]
    url = f"https://raw.githubusercontent.com/{config.GITHUB_SANDBOX_REPO}/{ref}/{path}"
    upstream = await state.http.get(url)
    media_type = _media_type(path)
    result = (upstream.status_code if upstream.status_code in (200, 404) else 502, upstream.content, media_type)
    if upstream.status_code in (200, 404) and len(state.file_cache) < 2000:
        state.file_cache[key] = result
    return result


def _media_type(path: str) -> str:
    return {
        ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml",
    }.get(Path(path).suffix, "application/octet-stream")


def _badge(content: bytes, env: str, sha: str, is_canary: bool) -> bytes:
    """A small corner label so the audience can tell which environment and commit they see."""
    label = f"{'canary' if is_canary else env} · {sha[:7]}"
    color = "#fecb2f" if is_canary else ("#4fd8ff" if env == "staging" else "#1ff1a5")
    badge = (f'<div style="position:fixed;bottom:12px;right:12px;z-index:9999;font:600 12px ui-monospace,monospace;'
             f'background:#141414;color:{color};padding:6px 10px;border-radius:6px;opacity:.9">{label}</div>')
    return content.replace(b"</body>", badge.encode() + b"</body>")


def _empty_page(env: str) -> str:
    return (f"<html><body style='font-family:system-ui;padding:48px;color:#646a82'>"
            f"<h2>Nothing is deployed to {env} yet.</h2><p>A pipeline run deploys here after its PR merges.</p></body></html>")


async def _flush_traffic_forever() -> None:
    """Send canary page loads and errors to each session's Environments Workflow in batches:
    one Signal every few seconds, and only when there was traffic."""
    while True:
        await asyncio.sleep(TRAFFIC_FLUSH_SECONDS)
        batch, state.traffic = state.traffic, defaultdict(lambda: [0, 0])
        for (owner_key, sha), (requests, errors) in batch.items():
            if requests or errors:
                try:
                    await state.temporal.get_workflow_handle(environments_id(owner_key)).signal(
                        "record_traffic", TrafficBatch(sha=sha, requests=requests, errors=errors))
                except RPCError:
                    pass


# -- The console UI ------------------------------------------------------------------------

if UI_DIST.exists():
    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
async def ui(path: str):
    if not UI_DIST.exists():
        return HTMLResponse("<p>The UI isn't built. Run <code>npm --prefix web run build</code>.</p>")
    candidate = UI_DIST / path
    if path and candidate.is_file() and UI_DIST in candidate.resolve().parents:
        return FileResponse(candidate)
    return FileResponse(UI_DIST / "index.html", headers={"Cache-Control": "no-store"})
