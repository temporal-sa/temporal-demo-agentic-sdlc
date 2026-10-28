# Catalog readiness

The decisions [buildkit guide 12](https://github.com/temporal-sa/temporal-toolkit-demo-buildkit/blob/main/guides/12-demo-catalog-readiness.md)
asks every demo to write down. The draft resource is [`deploy/demoproject.yaml`](../deploy/demoproject.yaml);
it passes `scripts/validate_projects.py` in the registry and is not committed there yet.

1. **Session boundary and identity.** The boundary is a *session slot*, leased by a browser when
   it starts a scenario (opening the console claims nothing). A slot is a task queue,
   `<copy queue>--sNN`, and its lease lives in a durable `Slot` Workflow (`<copy queue>.slot-NN`),
   not in Redis: claims are serialized Updates, so two browsers can't both win, and a Redis restart
   loses nothing. The browser holds a signed, HttpOnly cookie with a random session ID and a hint
   (the slot it last claimed); the Workflow is the authority. The cookie key is derived from
   `GITHUB_WEBHOOK_SECRET` with a fixed context string, so the catalog needs no new secret. A lease
   ends 2 hours after the last demo action (`SESSION_IDLE_MINUTES`; polling never extends it) or
   when the browser presses Reset; either way the slot's runs and Workers stop, its faults clear
   and its GitHub items close. Each copy has `SESSION_SLOTS` slots (8 hosted, 2 per crashable
   workspace, at most 99). Issues and PRs carry the slot's label (`agentic-sdlc:<slot queue>`), and
   the webhook starts a run only for a currently leased slot, and only for items created after the
   lease began. Unlabeled items are ignored. The hosted copy also starts workspace slots' runs on
   their queues, since GitHub reaches only the hosted URL. The console sits behind catalog SSO and
   only watches; its writes are demo machinery (start a scenario, stop a run, faults, crash and
   restore, rollouts, reset), scoped to the caller's slot.
2. **What the identity scopes.** Run Workflow IDs are `<slot queue>.gh.issue-<n>`, `.pr-<n>` or
   `.review-<n>`, so a redelivered webhook can't start a second run; agents are `<run id>.<role>`;
   environments are `<slot queue>.gh.environments`. Every API read, stream, fault and stop checks
   the caller's `<slot queue>.gh.` prefix and answers 404 otherwise. Run branches are
   `runs/<slot queue>/<run id>/…`; scenario branches are `dependency-bump/…` and `review-sample/…`.
   Reset closes the slot's labeled issues and PRs and deletes their branches. Faults live under
   `faults:<slot queue>:…` (a provider outage, the canary, a 429 count); run settings (models,
   autonomy) under `run-settings:<slot queue>`. Storefront routes are `/apps/<slot queue>/<env>/`.
3. **Task queue.** `TEMPORAL_TASK_QUEUE`, base `agentic-sdlc`, is the *copy* queue. It is never
   leased: it registers both pipeline versions and runs the GitHub Standalone Activities, so those
   keep working while a session's Workers are crashed. Runs start on their slot queue; child agents
   and Activities inherit it. Inside Worker code a session's scope is the Activity's or Workflow's
   own task queue, never `config.TASK_QUEUE`. Unversioned host queues (`<copy>-host`,
   `<copy>-host-v1`, `-v2`) run the `Slot` Workflow and the Activities that serve a slot's Workers.
4. **Distinct across crashable workspaces.** The task queue is in every slot queue, Workflow ID,
   branch name, Redis key and worker liveness key, so clones sharing the Temporal namespace, the
   GitHub repo or a Redis never see each other's work. Each Worker Deployment Version allows 100
   task queues on Cloud: the copy queue plus up to 99 slot queues (`SESSION_SLOTS` is checked at
   startup). The label `agentic-sdlc:<slot queue>` must fit GitHub's 50 characters, which the worker
   also checks at startup.
5. **Build and components.** One image (`docker/app.Dockerfile`) for `web` (temporalAccess, port 80),
   `worker-v1` and `worker-v2` (plain `worker: true` components with fixed build IDs, so they stay
   crashable and autoscaled), plus `redis` (`docker/redis.Dockerfile`). As in the pizza Worker
   Versioning demo, the app registers the versions itself and the console makes one current, so the
   D5 beat needs no redeploy. The Worker Deployment is `<task queue>-pipeline`, so a crashable
   workspace has its own.
6. **Routes.** One ingress host to `web`, behind SSO, which serves the console, `/api`, and the
   storefront environments at `/apps/<slot queue>/<env>/`. `/webhooks/github` is a `public: true` route (a new
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
    - Rolling out v2 in the shared demo changes the version for every session's new runs (one Worker
      Deployment per copy). The console asks for confirmation and says so; a crashable workspace
      keeps it private.
    - A crash stops a slot's Workers without draining them, so an in-flight model call is cut off
      and retried; that is the beat. A deploy's SIGTERM does the same to slot Workers (only the copy
      queue's Worker drains for up to 540 s).
    - Large-payload offload is off (no shared bucket); tool output is capped to keep payloads small.

## Cost guards

Every run makes real model calls on the shared provider keys.

- `MAX_ACTIVE_GITHUB_RUNS=3` per slot and `MAX_ACTIVE_RUNS_ALL_SLOTS=12` across a copy's slots, enforced by the webhook. Over the cap it starts nothing and
  the agent says so on the issue or PR.
- A per-run budget (default $3, capped at $25) that the Workflow enforces before every agent turn.
- Per-turn request limits on each agent.
- Runs have a 3-day execution timeout, and PR conversations close after 24 hours without a comment;
  environments close after 24 hours idle;
  faults expire in 10 minutes; a slot's lease ends after 2 hours idle; a crash restores itself after 5 minutes.

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

Slots add a few Actions per lease (the `Slot` Workflow, its Updates and timers, and the cleanup
Activities) plus heartbeats from the two host Activities (one per pipeline version) of each *leased*
slot, at one every few seconds, so an idle leased slot costs a small steady trickle. A provider
outage adds one Activity attempt per retry, at most one every 30 seconds per waiting agent.
