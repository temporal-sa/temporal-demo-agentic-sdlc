# Catalog readiness

The decisions [buildkit guide 12](https://github.com/temporal-sa/temporal-toolkit-demo-buildkit/blob/main/guides/12-demo-catalog-readiness.md)
asks every demo to write down. The draft resource is [`deploy/demoproject.yaml`](../deploy/demoproject.yaml);
it passes `scripts/validate_projects.py` in the registry and is not committed there yet.

1. **Session boundary and identity.** In the catalog, the platform's SSO gate supplies
   `X-Temporal-Auth-Subject` (`TRUST_AUTH_HEADERS=true`). Locally, each browser gets a signed
   random cookie. Either way the owner key is the first 16 hex characters of a SHA-256 of the subject.
2. **What the identity scopes.** Run Workflow IDs are `<task queue>.<owner>.run-<run id>`; agents
   are `<run id>.<role>`; environments are `<task queue>.<owner>.environments`. Every API read,
   write, stream, fault and reset checks that prefix and answers 404 for anyone else's IDs.
   GitHub branches are `runs/<task queue>/<owner>/<run id>/…`; issues carry a per-run marker.
   Faults live under `faults:<task queue>:<workflow id>`.
3. **Task queue.** `TEMPORAL_TASK_QUEUE`, base `agentic-sdlc`. Every Workflow starts on it; child
   agents and Activities inherit it. No queue name appears in Workflow code.
4. **Distinct across crashable workspaces.** The task queue is in every Workflow ID, branch name,
   Redis key and worker liveness key, so clones sharing the Temporal namespace, the GitHub repo or
   a Redis never see each other's work.
5. **Build and components.** One image (`docker/app.Dockerfile`) for `web` (temporalAccess, port 80)
   and `worker` (versioned, deployment `agentic-sdlc`), plus `redis` (`docker/redis.Dockerfile`).
6. **Routes.** One ingress host to `web`, which serves the console, `/api`, and the storefront
   environments at `/apps/<owner>/<env>/`.
7. **Temporal configuration.** Platform-injected `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE` and API
   key. Clients load with the config file disabled.
8. **Secrets and external state.** Shared `anthropic`, `openai`, `gemini`; project `github`
   (`GITHUB_TOKEN`, a fine-grained PAT for the storefront repo only, expires 2027-10-22) and
   `runtime` (`REDIS_PASSWORD`, `SESSION_SECRET`). Only the worker holds LLM keys and the GitHub
   token. External state: the public sandbox repo `temporal-sa/agentic-sdlc-storefront`.
9. **Smoke check.** `GET /healthz`, which only answers once the server has connected to Temporal.
10. **Known gaps.**
    - Agent-written tests run in the worker pod with a clean environment (no keys, no token), a
      throwaway HOME and a timeout, not in the platform's `isolatedExecutor`. Moving `run_tests` and
      `run_command` to an executor is the next hardening step.
    - The v2 pipeline is the same image with `PIPELINE_VERSION=v2`; showing D5 in the catalog means
      rolling out a new Worker Deployment Version with that value.
    - Large-payload offload is off (no shared bucket); tool output is capped to keep payloads small.

## Cost guards

Every run makes real model calls on the shared provider keys.

- `MAX_ACTIVE_RUNS_PER_USER=2`, enforced by the web server.
- A per-run budget (default $3, capped at $25) that the Workflow enforces before every agent turn.
- Per-turn request limits on each agent.
- Runs have a 3-day execution timeout; environments close after 24 hours idle; faults expire in 10 minutes.

## Actions estimate

Measured on a real run (free-shipping issue, two coders, the worker killed once mid-build): about
**170 Actions**. That's 1 Workflow start, 4 child agents (8), about 130 Activity attempts (model calls,
tool calls, GitHub, deploys and checks, including the retries after the crash), 12 Updates, 6 canary
timers, 4 Signals and about 10 heartbeats that reached the server. A run with a rollback adds a handful.
The console reads progress with Describe and List, which aren't billable, and streams agent events from
Redis rather than Workflow history. Count it yourself with the history of any run in the Temporal UI.
