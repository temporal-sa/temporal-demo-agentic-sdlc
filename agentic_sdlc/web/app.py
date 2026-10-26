"""The web server: GitHub's webhooks, the console's API and live stream, and the storefront environments.

Runs start, and people talk to the agents, on GitHub: webhooks.py turns each delivery into a
Workflow start or a Signal. The console only watches. It reads progress with Describe (free,
and available while workers are down), relays agent events from Redis, and serves staging,
production and canary storefronts from the commits the Environments Workflow records. Its only
writes are demo machinery: starting a scenario on GitHub, stopping a run, faults, rollouts, reset.

It holds no LLM keys and no GitHub token; anything that writes to GitHub runs on the worker.
Everything is scoped to this copy of the demo: Workflow IDs start ``<task queue>.gh.``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from redis.asyncio import Redis
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from agentic_sdlc import config, faults, versions
from agentic_sdlc.activities.github import CleanupInput, ScenarioInput, ScenarioStarted
from agentic_sdlc.agents.registry import allowlist
from agentic_sdlc.models import RunState
from agentic_sdlc.pipeline.environments import EnvState, TrafficBatch
from agentic_sdlc.temporal_client import EVENT_STREAM, connect
from agentic_sdlc.web import webhooks

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
UI_DIST = ROOT / "web" / "dist"
PIPELINE_DIR = Path(__file__).resolve().parent.parent / "pipeline"
SOURCE_FILES = {"issue": "workflow.py", "dependency": "dependency.py", "review": "review.py", "base": "base.py"}
ISSUES = yaml.safe_load((Path(__file__).resolve().parent.parent / "issues.yaml").read_text())

RUNS_PREFIX = f"{config.TASK_QUEUE}.gh."
ENVIRONMENTS_ID = f"{RUNS_PREFIX}environments"
RUN_TYPES = "('IssueToProduction', 'DependencyRepair', 'PullRequestReview')"
RUN_ID = re.compile(r"(issue|pr|review)-[0-9]{1,7}")
KEEPALIVE_SECONDS = 15
VIEW_CACHE_SECONDS = 0.9
ENV_CACHE_SECONDS = 2.0
TRAFFIC_FLUSH_SECONDS = 5
WORKER_HEARTBEAT_PREFIX = f"workers:{config.TASK_QUEUE}:"
CANARY_COOKIE = f"canary-{config.TASK_QUEUE}"
SETTINGS_KEY = "run-settings:{}"  # per task queue, so each crashable workspace keeps its own


class State:
    temporal: Client
    redis: Redis
    http: httpx.AsyncClient
    view_cache: dict[str, tuple[float, dict]] = {}
    env_cache: dict[str, tuple[float, EnvState]] = {}
    file_cache: dict[tuple[str, str], tuple[int, bytes, str]] = {}
    traffic: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # canary sha -> [page loads, errors]


state = State()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not config.GITHUB_WEBHOOK_SECRET:
        raise RuntimeError("GITHUB_WEBHOOK_SECRET isn't set. It's the secret on the storefront repo's webhook "
                           "(scripts/setup-github.sh creates it); without it no GitHub event can be trusted.")
    state.temporal = await connect()
    state.redis = Redis.from_url(config.REDIS_URL)
    state.http = httpx.AsyncClient(timeout=15, follow_redirects=True)
    flusher = asyncio.create_task(_flush_traffic_forever())
    keeper = asyncio.create_task(_keep_a_current_version())
    yield
    flusher.cancel()
    keeper.cancel()
    await state.http.aclose()


app = FastAPI(title="Agentic SDLC demo", lifespan=lifespan)


@app.middleware("http")
async def server_timing(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
    return response


# -- Scope ---------------------------------------------------------------------------------


def run_workflow_id(run_id: str) -> str:
    if not RUN_ID.fullmatch(run_id):
        raise HTTPException(400, "Bad run id")
    return f"{RUNS_PREFIX}{run_id}"


def require_ours(workflow_id: str) -> str:
    # 404 rather than 403, so other copies' IDs aren't confirmed to exist.
    if not workflow_id.startswith(RUNS_PREFIX):
        raise HTTPException(404, "Not found")
    return workflow_id


# -- Config and views ----------------------------------------------------------------------


@app.get("/healthz")
async def healthz():
    # Reaching this proves the server started, which it only does after connecting to Temporal.
    return {"ok": True, "task_queue": config.TASK_QUEUE}


@app.get("/api/config")
async def get_config():
    namespace = state.temporal.namespace
    return {
        "owner_label": config.OWNER_LABEL,
        "models": allowlist(require_keys=False),
        "settings": (await _settings_for(config.TASK_QUEUE)).model_dump(),
        "issues": ISSUES,
        "budget_usd": config.DEFAULT_BUDGET_USD,
        "temporal_ui": f"{config.temporal_ui_url(namespace)}",
        "repo": config.GITHUB_SANDBOX_REPO,
        "local_controls": bool(config.SUPERVISOR_URL),
        "task_queue": config.TASK_QUEUE,
    }


@app.get("/api/source/{name}")
async def pipeline_source(name: Literal["issue", "dependency", "review", "base"]):
    """A run Workflow's source for the console's code panel; ``base`` holds what all three share."""
    return Response((PIPELINE_DIR / SOURCE_FILES[name]).read_text(), media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "max-age=60"})


@app.get("/api/overview")
async def overview():
    """The console's landing view: this copy's runs, environments, workers."""
    runs, env, workers = await asyncio.gather(_list_runs(), _environments(), _workers())
    return {"runs": runs, "environments": env.model_dump(), "workers": workers, "apps": "/apps/gh"}


async def _list_runs() -> list[dict]:
    query = f"WorkflowType IN {RUN_TYPES} AND WorkflowId STARTS_WITH '{RUNS_PREFIX}'"
    runs = []
    async for execution in state.temporal.list_workflows(query, limit=20):
        memo = await execution.memo_value("run", default=None)
        if memo:
            runs.append({**_run_summary(memo), "workflow_id": execution.id, "temporal_run_id": execution.run_id,
                         "closed": execution.status != WorkflowExecutionStatus.RUNNING})
    return sorted(runs, key=lambda r: r["started_at"], reverse=True)


def _run_summary(memo: dict) -> dict:
    return {k: memo.get(k) for k in ("run_id", "kind", "title", "status", "version", "started_at", "ended_at", "usage",
                                     "proofs", "issue_url", "pr_url")}


@app.get("/api/runs/{run_id}")
async def run_view(run_id: str):
    return await _run_view(run_workflow_id(run_id)) or _not_found()


async def _run_view(workflow_id: str) -> dict | None:
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
        _environments(), _workers(), faults.armed([workflow_id, *agent_ids]),
    )
    retries = [*_retries(workflow_id, description)]
    for agent_id, agent_description in zip(agent_ids, agent_descriptions):
        if agent_description is not None:
            retries.extend(_retries(agent_id, agent_description))
    view = {
        "run": run.model_dump(mode="json"),
        "workflow_id": workflow_id,
        # The Temporal UI's execution pages need the run ID as well as the Workflow ID.
        "temporal_run_id": description.run_id,
        "agent_run_ids": {a: d.run_id for a, d in zip(agent_ids, agent_descriptions) if d is not None},
        "closed": description.status != WorkflowExecutionStatus.RUNNING,
        "workflow_status": description.status.name if description.status else "UNKNOWN",
        "retries": retries,
        "faults": armed,
        "environments": env.model_dump(),
        "workers": workers,
        "apps": "/apps/gh",
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


async def _environments() -> EnvState:
    wf_id = ENVIRONMENTS_ID
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


def _not_found():
    raise HTTPException(404, "Not found")


# -- Live stream ---------------------------------------------------------------------------


@app.get("/api/runs/{run_id}/stream")
async def run_stream(run_id: str, request: Request):
    """One SSE stream per run view: run snapshots when they change, plus every agent's
    events (model calls, tool calls, approvals) relayed from Redis."""
    workflow_id = run_workflow_id(run_id)
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2000)

    async def snapshots():
        last = None
        attached: set[str] = set()
        while True:
            view = await _run_view(workflow_id)
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


# -- GitHub ----------------------------------------------------------------------------------


@app.post("/webhooks/github", include_in_schema=False)
async def github_webhook(request: Request):
    """Every event on the storefront repo. Public (no SSO), so nothing is trusted until GitHub's
    signature checks out; then webhooks.route decides whether this copy acts on it."""
    body = await request.body()
    if not webhooks.verify(config.GITHUB_WEBHOOK_SECRET, body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "Bad signature")
    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"ok": True}
    done = await webhooks.handle(state.temporal, webhooks.route(event, json.loads(body)), _settings_for)
    if done:
        log.info("%s %s: %s", event, request.headers.get("X-GitHub-Delivery", ""), "; ".join(done))
        state.view_cache.clear()
    return {"done": done}


async def _settings_for(task_queue: str) -> webhooks.RunSettings:
    raw = await state.redis.get(SETTINGS_KEY.format(task_queue))
    return webhooks.RunSettings.model_validate_json(raw) if raw else webhooks.RunSettings()


# -- Demo machinery ------------------------------------------------------------------------------


class Scenario(BaseModel):
    kind: Literal["issue", "dependency", "review"]
    issue_id: str | None = None  # one of issues.yaml, for kind "issue"
    title: str | None = None  # or a custom issue
    body: str | None = None
    settings: webhooks.RunSettings | None = None  # kept for this copy's runs from now on


SCENARIO_ACTIVITY = {"issue": "open_issue_for_copy", "dependency": "dispatch_bump", "review": "open_sample_pr"}


@app.post("/api/scenarios")
async def start_scenario(body: Scenario):
    """Do on GitHub what a person would (open an issue, run the bump Action, open a PR), labeled
    for this copy. The run itself starts when GitHub's webhook for it arrives."""
    if body.settings:
        for role, choice in body.settings.models:
            if choice.model not in allowlist(require_keys=False).get(choice.provider, ()):
                raise HTTPException(400, f"{choice.key} isn't available for the {role}")
        await state.redis.set(SETTINGS_KEY.format(config.TASK_QUEUE), body.settings.model_dump_json())
    title, text = "", ""
    if body.kind == "issue":
        prepared = next((i for i in ISSUES if i["id"] == body.issue_id), None)
        title = (body.title or (prepared or {}).get("title") or "").strip()[:200]
        text = (body.body or (prepared or {}).get("body") or "").strip()[:4000]
        if not title:
            raise HTTPException(400, "An issue needs a title")
    # The web server holds no GitHub token, so this runs on the worker as a Standalone Activity.
    started = await state.temporal.execute_activity(
        SCENARIO_ACTIVITY[body.kind],
        ScenarioInput(repo=config.GITHUB_SANDBOX_REPO, owner_label=config.OWNER_LABEL, title=title, body=text),
        id=f"{RUNS_PREFIX}scenario-{body.kind}-{secrets.token_hex(4)}", task_queue=config.TASK_QUEUE,
        result_type=ScenarioStarted, start_to_close_timeout=timedelta(seconds=30),
        schedule_to_close_timeout=timedelta(minutes=1),
    )
    return started.model_dump()


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str):
    """Cancel a run. It stops its agents and leaves its issue or PR on GitHub as it is."""
    workflow_id = run_workflow_id(run_id)
    try:
        await state.temporal.get_workflow_handle(workflow_id).cancel()
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            _not_found()
        raise
    state.view_cache.pop(workflow_id, None)
    return {"ok": True}


class Fault(BaseModel):
    kind: Literal["model_rate_limit", "github_rate_limit"]
    workflow_id: str
    attempts: int = 2


@app.post("/api/faults")
async def inject_fault(body: Fault):
    require_ours(body.workflow_id)
    await faults.arm(body.workflow_id, body.kind, attempts=max(1, min(body.attempts, 4)))
    return {"armed": body.model_dump()}


class BreakCanary(BaseModel):
    seconds: int = 180


@app.post("/api/environments/break-canary")
async def break_canary(body: BreakCanary):
    env = await state.temporal.get_workflow_handle(ENVIRONMENTS_ID).execute_update(
        "break_canary", min(body.seconds, 600), result_type=EnvState)
    state.env_cache.pop(ENVIRONMENTS_ID, None)
    return {"environments": env.model_dump()}


@app.post("/api/reset")
async def reset():
    """Back to a clean slate for this copy only: stop its runs, empty its environments, clear its
    faults, and close its labeled issues and PRs and delete their branches on GitHub."""
    runs = await _list_runs()
    for run in runs:
        if not run["closed"]:
            try:
                await state.temporal.get_workflow_handle(run["workflow_id"]).terminate("Reset by presenter")
            except RPCError:
                pass
    try:
        await state.temporal.get_workflow_handle(ENVIRONMENTS_ID).execute_update("reset", result_type=EnvState)
    except RPCError:
        pass
    await faults.clear([r["workflow_id"] for r in runs])
    await state.temporal.start_activity(
        "cleanup_runs",
        CleanupInput(repo=config.GITHUB_SANDBOX_REPO, owner_label=config.OWNER_LABEL,
                     branch_prefix=f"runs/{config.TASK_QUEUE}/"),
        id=f"{RUNS_PREFIX}cleanup-{int(time.time())}",
        task_queue=config.TASK_QUEUE,
        start_to_close_timeout=timedelta(minutes=5),
        heartbeat_timeout=timedelta(seconds=60),
    )
    state.view_cache.clear()
    state.env_cache.clear()
    return {"ok": True, "stopped": sum(1 for r in runs if not r["closed"])}


# -- Pipeline versions ----------------------------------------------------------------------


@app.get("/api/versions")
async def get_versions():
    return await versions.describe(state.temporal)


class SetVersion(BaseModel):
    build_id: Literal["v1", "v2"]


@app.post("/api/versions/current")
async def set_version(body: SetVersion):
    """Roll out a pipeline version: new runs start on it, in-flight runs stay pinned to theirs.
    This is a presenter control for the whole deployment (each crashable workspace has its own)."""
    try:
        return await versions.set_current(state.temporal, body.build_id)
    except RPCError as err:
        raise HTTPException(409, f"Couldn't make {body.build_id} current: {err.message}") from err


async def _keep_a_current_version() -> None:
    """A fresh deployment has no current version, and versioned workers take no new runs until
    one is set. Make v1 current as soon as its worker registers."""
    while True:
        try:
            if await versions.ensure_current(state.temporal):
                await asyncio.sleep(60)
                continue
        except RPCError:
            pass
        await asyncio.sleep(5)


# -- Local presenter controls (scripts/dev's supervisor) ------------------------------------


@app.post("/api/worker/{action}")
async def worker_control(action: Literal["kill", "start"]):
    if not config.SUPERVISOR_URL:
        raise HTTPException(409, "No local supervisor. In the catalog, use the workspace's crash controls.")
    response = await state.http.post(f"{config.SUPERVISOR_URL}/{action}")
    return JSONResponse(response.json(), status_code=response.status_code)


# -- Storefront environments ----------------------------------------------------------------


@app.get("/apps/gh/{env}/{path:path}")
async def serve_storefront(env: Literal["staging", "prod", "canary"], path: str, request: Request):
    """Serve this copy's storefront exactly as committed: files are fetched from GitHub at an
    immutable commit and cached. Agent-written code only ever runs in the visitor's browser."""
    environments = await _environments()
    response = Response()
    sha, is_canary = _route(environments, env, request, response)
    if sha is None:
        return HTMLResponse(_empty_page(env), status_code=200)
    path = path or "index.html"
    synthetic = request.headers.get("X-Synthetic-Check") == "1"
    if is_canary and environments.canary_fault_until > time.time() and path != "index.html":
        # Presenter fault, labeled in the console: the canary's assets fail to load.
        if not synthetic:
            state.traffic[sha][1] += 1
        return Response("Injected fault: canary asset unavailable", status_code=503)
    status, content, media_type = await _fetch_file(sha, path)
    if is_canary and not synthetic:
        counts = state.traffic[sha]
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


@app.post("/apps/gh/{env}/__beacon")
async def storefront_beacon(env: str, request: Request):
    """Browser errors reported by the storefront's telemetry.js count against the canary."""
    environments = await _environments()
    if environments.canary_sha and request.cookies.get(CANARY_COOKIE) == environments.canary_sha:
        state.traffic[environments.canary_sha][1] += 1
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
    assigned = request.cookies.get(CANARY_COOKIE)
    if assigned not in (env.canary_sha, "stable"):
        assigned = env.canary_sha if secrets.randbelow(100) < env.canary_weight else "stable"
        response.set_cookie(CANARY_COOKIE, assigned, max_age=3600, samesite="lax")
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
    """Send canary page loads and errors to the Environments Workflow in batches: one Signal
    every few seconds, and only when there was traffic."""
    while True:
        await asyncio.sleep(TRAFFIC_FLUSH_SECONDS)
        batch, state.traffic = state.traffic, defaultdict(lambda: [0, 0])
        for sha, (requests, errors) in batch.items():
            if requests or errors:
                try:
                    await state.temporal.get_workflow_handle(ENVIRONMENTS_ID).signal(
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
