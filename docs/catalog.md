# Catalog readiness

The decisions [buildkit guide 12](https://github.com/temporal-sa/temporal-toolkit-demo-buildkit/blob/main/guides/12-demo-catalog-readiness.md)
asks every demo to write down. The draft resource is [`deploy/demoproject.yaml`](../deploy/demoproject.yaml);
it passes `scripts/validate_projects.py` in the registry and is not committed there yet.

1. **Session boundary and identity.** The boundary is the copy of the demo, not the browser: runs
   belong to GitHub issues and PRs, which anyone can comment on. The hosted copy, each crashable
   workspace and each laptop are separate copies. Issues and PRs carry an owner label
   (`agentic-sdlc:<task queue>`, or `agentic-sdlc:local-<login>` for a laptop) and each copy acts only
   on its own. The hosted copy also takes unlabeled items (people using the repo directly) and starts
   workspace-labeled runs on the workspace's queue, since GitHub reaches only the hosted URL. The
   console sits behind catalog SSO and only watches; its writes are demo machinery (start a scenario,
   stop a run, faults, rollouts, reset), scoped to its own copy.
2. **What the identity scopes.** Run Workflow IDs are `<task queue>.gh.issue-<n>`, `.pr-<n>` or
   `.review-<n>`, so a redelivered webhook can't start a second run; agents are `<run id>.<role>`;
   environments are `<task queue>.gh.environments`. Every API read, stream, fault and stop checks
   the `<task queue>.gh.` prefix and answers 404 otherwise. Run branches are
   `runs/<task queue>/<run id>/…`; scenario branches are `dependency-bump/…` and `review-sample/…`.
   Reset closes the copy's labeled issues and PRs and deletes its branches. Faults live under
   `faults:<task queue>:<workflow id>`; run settings (models, autonomy) under `run-settings:<task queue>`.
3. **Task queue.** `TEMPORAL_TASK_QUEUE`, base `agentic-sdlc`. Every Workflow starts on it; child
   agents and Activities inherit it. No queue name appears in Workflow code.
4. **Distinct across crashable workspaces.** The task queue is in every Workflow ID, branch name,
   Redis key and worker liveness key, so clones sharing the Temporal namespace, the GitHub repo or
   a Redis never see each other's work.
5. **Build and components.** One image (`docker/app.Dockerfile`) for `web` (temporalAccess, port 80),
   `worker-v1` and `worker-v2` (plain `worker: true` components with fixed build IDs, so they stay
   crashable and autoscaled), plus `redis` (`docker/redis.Dockerfile`). As in the pizza Worker
   Versioning demo, the app registers the versions itself and the console makes one current, so the
   D5 beat needs no redeploy. The Worker Deployment is `<task queue>-pipeline`, so a crashable
   workspace has its own.
6. **Routes.** One ingress host to `web`, behind SSO, which serves the console, `/api`, and the
   storefront environments at `/apps/gh/<env>/`. `/webhooks/github` is a `public: true` route (a new
   registry field: it keeps the auth-header strip and skips forward-auth), because GitHub can't sign
   in. The web server rejects any delivery whose `X-Hub-Signature-256` doesn't verify, and won't start
   without `GITHUB_WEBHOOK_SECRET`.
7. **Temporal configuration.** Platform-injected `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE` and API
   key. Clients load with the config file disabled.
8. **Secrets and external state.** Shared `anthropic`, `openai`, `gemini`; project `github`
   (`GITHUB_TOKEN`, a fine-grained PAT for the storefront repo only, expires 2027-10-22) and
   `runtime` (`REDIS_PASSWORD`, `GITHUB_WEBHOOK_SECRET`). Only the worker holds LLM keys and the GitHub
   token; anything the web server does on GitHub (scenarios, reset, the over-the-cap note) runs on the
   worker as a Standalone Activity. The PAT needs contents, pull requests, issues, commit statuses and
   actions (read/write). These project secrets exist in AWS account 429214323166, us-west-1 (created with `scripts/create-catalog-secrets.sh`). External state: the public sandbox repo `temporal-sa/agentic-sdlc-storefront` (its webhook,
   rulesets, labels and sample branch come from `scripts/setup-github.sh`) and the public dependency
   `temporal-sa/orbit-money`. The namespace needs the `AgenticSdlcGitHubRefs` keywordList search attribute,
   declared in the DemoProject.
9. **Smoke check.** `GET /healthz`, which only answers once the server has connected to Temporal.
10. **Known gaps.**
    - Agent-written tests run in the worker pod with a clean environment (no keys, no token), a
      throwaway HOME and a timeout, not in the platform's `isolatedExecutor`. Moving `run_tests` and
      `run_command` to an executor is the next hardening step.
    - Agents' tool calls are always auto-approved: there's no way to approve a shell command from
      GitHub. The supervised setting now only means the issue's PR waits for `/merge`.
    - Rolling out v2 in the shared demo changes the version for every session's new runs. It's a
      presenter control with a confirmation; a crashable workspace keeps it private.
    - Large-payload offload is off (no shared bucket); tool output is capped to keep payloads small.

## Cost guards

Every run makes real model calls on the shared provider keys.

- `MAX_ACTIVE_GITHUB_RUNS=3` per copy, enforced by the webhook. Over the cap it starts nothing and
  the agent says so on the issue or PR.
- A per-run budget (default $3, capped at $25) that the Workflow enforces before every agent turn.
- Per-turn request limits on each agent.
- Runs have a 3-day execution timeout, and PR conversations close after 24 hours without a comment;
  environments close after 24 hours idle;
  faults expire in 10 minutes.

## GitHub webhooks, no polling

Runs never poll GitHub. Each comment reaches the runs watching its issue or PR as one Signal, found
with a Visibility query (not billable). GitHub doesn't redeliver a failed webhook by itself, so when
a gate opens the run reads the issue and PR comments once and de-duplicates them by comment ID: one
GitHub read and one Activity per gate.

## Actions estimate

Measured on a real run of the console-driven version (before webhooks; gates were Updates rather
than Signals and polling added a few Actions per gate, so the GitHub-native flow should come in a little lower) (free-shipping issue, two coders, the worker killed once mid-build): about
**170 Actions**. That's 1 Workflow start, 4 child agents (8), about 130 Activity attempts (model calls,
tool calls, GitHub, deploys and checks, including the retries after the crash), 12 Updates, 6 canary
timers, 4 Signals and about 10 heartbeats that reached the server. A run with a rollback adds a handful.
The console reads progress with Describe and List, which aren't billable, and streams agent events from
Redis rather than Workflow history. Count it yourself with the history of any run in the Temporal UI.
