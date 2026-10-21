# Runbook: Agentic SDLC on Temporal

How to present the demo and reset it, written so someone who didn't build it can run it. Permalinks point at commit [`7a32f22`](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/tree/7a32f22e8407c56c5d07da68db3ee1c23715a5a1).

## What it shows

A GitHub issue becomes a reviewed, deployed change. A team of AI agents plans it, codes it in parallel, tests it, reviews it and ships it. Temporal keeps that work alive through crashes, hours-long human approvals, flaky APIs and a bad canary, and records every step.

The six decision criteria in the console's left rail are the problems, and each beat below ticks one. A tick only appears when a run recorded the evidence.

| | Criterion | Beat |
|---|---|---|
| D1 | An agent crash loses no work and re-spends no tokens | 4 |
| D2 | Humans stay accountable without babysitting | 3, 6 |
| D3 | Parallel agents, flaky APIs and spend stay under control | 5 |
| D4 | A bad change in production is caught and undone | 7 |
| D5 | The pipeline can change while runs are in flight | 8 |
| D6 | Every agent action is auditable | 9 |

## Before you start

- **Where it runs.** Today the demo runs locally (see [Run it locally](#run-it-locally)). The catalog resource is drafted in [`deploy/demoproject.yaml`](deploy/demoproject.yaml) but not onboarded yet. Once it is, the hosted copy is `https://agentic-sdlc.tmprl-demo.cloud`, and a crashable workspace replaces the local "Kill the worker" button.
- **Real calls cost real money.** Every run calls Anthropic, OpenAI or Gemini. A run usually costs $0.20 to $1.50 in estimated model spend; the per-run budget is $3.
- **Ship one run before the audience arrives.** Act 1 opens with the result. Start a prepared issue on auto merge, approve its two gates, and let it finish (about 5 minutes).
- **Open three tabs:** the console, the Temporal UI, and [the storefront repo's pull requests](https://github.com/temporal-sa/agentic-sdlc-storefront/pulls).
- **The GitHub token expires on 2027-10-22.** See [Rotate the GitHub token](#rotate-the-github-token).

## Start it

`uv run python scripts/dev.py`, then open http://localhost:8000.

A healthy start shows **v1 workers** in green in the top right, **0 of 6 shown live** in the left rail, and the run picker at the bottom. Press **4** for the live run.

Keys: **1–6** switch acts, **N** starts a run.

## The walkthrough

### 1. The result (act 1)

**Do:** press **1**.

**The audience sees** the storefront in production with the change your warm-up run shipped, its time from issue to production, its model calls and its estimated cost.

**Say:** "Agents planned, coded, reviewed and shipped this. Temporal ran everything around them. Let's see why that matters."

**Proof:** the Issue, Pull request and Event History links on the card.

### 2. Why agentic SDLC, and why it's hard (acts 2 and 3)

**Do:** press **2**, then **3**.

**The audience sees** three cited figures (coding +30–40% but team productivity under +10%; 84–90% of developers use AI tools but about 13% of teams run it across the lifecycle; the bottleneck moving to review and governance), then the six criteria, each with the pain in someone's words.

**Say:** "Agents across the lifecycle fail the way distributed systems fail. They crash, they wait on people, they run in parallel, they hit rate limits, and they ship bad changes. Those six cards are what you'd need proven before trusting them. Let's prove them."

**Proof:** every figure links to its source. All the sources are in [docs/story.md](docs/story.md).

### 3. Start a run; a person approves the plan (D2)

**Do:** press **4**, click **New run**, pick **Let customers apply the discount code ORBIT10**, leave **Auto merge**, and click **Start the run**.

**The audience sees** the issue open on GitHub, the planner agent read the repo, and the run stop at the yellow **Approve plan** diamond with the plan and its subtasks.

**Say:** "The Workflow is parked on a durable wait. No process is holding it open. It could sit here for a weekend and cost nothing." Then click **Approve**.

**Proof:** **Open in Temporal**: a `WorkflowExecutionUpdateAccepted` event for `decide`.

### 4. Kill the worker mid-coding (D1)

**Do:** while the two coder branches are active in the graph, click **Kill the worker** in the bottom bar. Wait about 5 seconds, then click **Start a worker**.

**The audience sees** the top right turn yellow with **no workers**, and a yellow banner saying the run is paused, not lost. Within about 15 seconds of the restart, the coders continue: new tool calls appear in their cards, and the token count carries on from where it was. **D1** ticks when that agent's turn completes.

**Say:** "Every model call and every tool call is an Activity. What finished is in the agent's Event History, so the new worker continues from the agent's last completed step. And every edit the agent made was already pushed to its branch."

**Proof:** the D1 evidence link opens the run. In the coder's Workflow (`…coder-<subtask>`), the in-flight Activity shows attempt 2, and every earlier model call ran once.

### 5. Rate limits and parallel agents (D3)

**Do:** in one coder's card, click **Inject 429**.

**The audience sees** that lane turn pink with "Attempt 2: Injected: model provider returned 429", while the other coder keeps working. Then it recovers. **D3** ticks when the build finishes with agents in parallel.

**Say:** "That retry is a Retry Policy. There's no hand-written loop. The other agent never noticed."

Optionally, click **GitHub 429** in the bottom bar before the PR opens; the pipeline's next GitHub call retries after the `Retry-After` it was given.

**Proof:** the pending Activity in Temporal shows attempt 2 with the injected failure.

### 6. Review, merge, staging; a person approves production (D2)

**The audience sees** the reviewer agent (a different model family) review the PR, the merge, the staging deploy and its checks. Then the run waits at **Approve prod**.

**Do:** click **Open the change on staging** to show the feature working, then click **Approve**.

### 7. Break the canary (D4)

**Do:** while **Canary** is active (it bakes for about 90 seconds), click **Break the canary** in the bottom bar.

**The audience sees** the canary checks fail. The pipeline stops canary traffic, opens and merges a revert PR, reopens the issue with the findings, and the run ends **Rolled back**. **D4** ticks.

**Say:** "That's a saga. Each step has its compensation, and the Workflow runs them in order. Nobody got paged."

**Proof:** the revert PR on GitHub, and the `rollback` step's Activities in history.

To show a clean ship instead, skip this beat. The canary promotes after its checks.

### 8. Change the pipeline mid-run (D5)

**Do:** start a second run and approve its plan. While it's coding, press **5** and click **Roll out v2**. Start a third run.

**The audience sees** the in-flight run keep **pipeline v1** and finish without a security stage. The new run shows **pipeline v2** with a **Security** step. **D5** ticks when the v1 run finishes after a v2 run has started.

**Say:** "Each run is pinned to the version it started on. Running work finishes exactly as it began, new work gets the new pipeline, and nothing is patched in place."

**Proof:** `temporal worker deployment describe --name agentic-sdlc` shows both versions. Each run's history shows its pinned version.

### 9. Proof (act 6, D6)

**Do:** press **6**.

**The audience sees** the recap and the scorecard, with each tick linking to the Workflow that shows it.

**Say:** "Every model call, tool call, approval and retry is in Event History. That's your audit trail, and you didn't build it."

## Show it in code

### The whole lifecycle is one Workflow method

**When to use it:** after beat 3, or when someone asks where the orchestration lives.

**Where it is:** [`agentic_sdlc/pipeline/workflow.py` lines 121–146](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/7a32f22e8407c56c5d07da68db3ee1c23715a5a1/agentic_sdlc/pipeline/workflow.py#L121-L146) (`code -g agentic_sdlc/pipeline/workflow.py:121`)

```python
async def run(self, request: RunRequest) -> RunState:
    self._publish()
    try:
        repo = await self._open_issue()
        plan = await self._plan(repo)
        branches = await self._build(repo, plan)
        pr = await self._open_pull_request(repo, plan, branches)
        await self._review(repo, plan, pr)
        merge_sha = await self._merge(repo, pr)
        await self._deploy_to_staging(merge_sha)
        await self._gate("production", "Ship this change to production?", ...)
        shipped = await self._canary(repo, merge_sha)
        await self._finish(repo, shipped)
```

**What you need to understand:**

- Agents decide inside a step; the Workflow decides the process. That's the split that makes nondeterministic agents safe to run unattended.
- It's ordinary Python. Each line can take seconds or days, and the code doesn't change.
- The customer doesn't write a state machine, a job queue, a checkpoint store or a resume path.

**Docs:** [Workflows](https://docs.temporal.io/workflows)

### Agents are Workflows; a dead worker loses nothing

**When to use it:** after beat 4.

**Where it is:** [`agentic_sdlc/agents/workflows.py` lines 103–130](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/7a32f22e8407c56c5d07da68db3ee1c23715a5a1/agentic_sdlc/agents/workflows.py#L103-L130) and [`pipeline/workflow.py` lines 409–423](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/7a32f22e8407c56c5d07da68db3ee1c23715a5a1/agentic_sdlc/pipeline/workflow.py#L409-L423)

```python
@agent.defn(name="CoderAgent")
class CoderAgent(_AgentBase):
    @agent.accepts
    async def code(self, request: CodeRequest) -> CodeReply:
        result = await CODER.run(prompt, deps=self._deps(), message_history=self._history,
                                 model=self._init.model.key, ...)
```

```python
result = await workflow.execute_activity_method(
    AgentTurnActivities.run_agent_turn,
    AgentTurn(workflow_id=agent_id, handler=handler, payload=...,
              # Deterministic, so a retried Activity re-attaches to the same turn.
              update_id=f"{agent_id}:{handler}:{self._turns[agent_id]}"),
    **AGENT_TURN,
)
```

**What you need to understand:**

- `CODER` is a Pydantic AI agent wrapped in `TemporalAgent`, so each model request is an Activity, with a heartbeat from `DemoModel` in `agents/harness_ext.py`. Each tool call is a harness activity tool.
- The pipeline talks to the agent through an Activity, because Python Workflows can't send Updates. The retried Activity re-sends with the same update ID, Temporal returns the original acceptance, and it waits for the same turn. The agent itself kept running through the crash.
- One agent class serves every provider: the model is picked per run by key, such as `anthropic:claude-sonnet-5-5`.

**Docs:** [Pydantic AI durable execution with Temporal](https://ai.pydantic.dev/durable_execution/temporal/), [temporal-agent-harness](https://github.com/temporal-sa/temporal-agent-harness)

### The branch is the agent's durable workspace

**When to use it:** after beat 4, when someone asks about files on the dead worker.

**Where it is:** [`agentic_sdlc/agents/tools.py` lines 179–205](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/7a32f22e8407c56c5d07da68db3ee1c23715a5a1/agentic_sdlc/agents/tools.py#L179-L205)

```python
async def checkout(workspace: Workspace) -> Path:
    ...
    else:
        await _git(root, "fetch", "--quiet", "origin", workspace.branch)
        await _git(root, "reset", "--quiet", "--hard", f"origin/{workspace.branch}")

async def _commit_and_push(root, workspace, path, message) -> str:
    ...
    await _git(root, "push", "--quiet", "origin", f"HEAD:{workspace.branch}")
```

**What you need to understand:**

- Every write is committed and pushed before the tool returns, and every tool starts by syncing to the branch head. A replacement worker rebuilds the checkout from GitHub, and you can watch the agent's progress as commits.
- The tools are idempotent: a retried edit whose first attempt already pushed reports "already contains the new text".

### A person approves, for as long as it takes

**When to use it:** after beat 3 or 6.

**Where it is:** [`pipeline/workflow.py` lines 356–386](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/7a32f22e8407c56c5d07da68db3ee1c23715a5a1/agentic_sdlc/pipeline/workflow.py#L356-L386)

```python
await workflow.wait_condition(lambda: self._decision is not None)
...
@workflow.update
def decide(self, response: GateResponse) -> RunState:
    self._decision = response.decision
    ...
@decide.validator
def _validate_decide(self, response: GateResponse) -> None:
    if self._state.gate is None or self._state.gate.key != response.gate:
        raise ValueError(f"The run isn't waiting at the {response.gate} gate.")
```

**What you need to understand:**

- The wait is durable. No worker holds it, it survives restarts and deploys, and it costs nothing while it waits.
- The Update returns the new run state to the browser in the same call. The validator rejects a stale click (for example a second tab answering a gate that's already gone) before anything is written to history.
- The same gate enforces the budget: `_ensure_budget` (lines 426–436) stops before the next agent turn once spend reaches it.

**Docs:** [Message passing](https://docs.temporal.io/develop/python/workflows/message-passing)

### Parallel agents and retries without loops

**When to use it:** after beat 5.

**Where it is:** [`pipeline/workflow.py` lines 184–197](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/7a32f22e8407c56c5d07da68db3ee1c23715a5a1/agentic_sdlc/pipeline/workflow.py#L184-L197) and [`agents/runtime.py` lines 29–39](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/7a32f22e8407c56c5d07da68db3ee1c23715a5a1/agentic_sdlc/agents/runtime.py#L29-L39)

```python
await asyncio.gather(*(
    self._code_subtask(repo, plan, subtask, branch) for subtask, branch in zip(plan.subtasks, branches)
))
```

```python
MODEL_ACTIVITY = ActivityConfig(
    start_to_close_timeout=timedelta(minutes=10),
    heartbeat_timeout=timedelta(seconds=10),
    retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                             maximum_interval=timedelta(seconds=30), maximum_attempts=6,
                             non_retryable_error_types=["ProviderRejected"]),
)
```

**What you need to understand:**

- Each coder is its own child Workflow, so one agent's retries or failure don't stall another's work.
- Provider SDK retries are off (`agents/registry.py`), so every retry is a visible Activity attempt. A rejected request (most 4xx errors) is non-retryable; rate limits and 5xx errors back off.
- GitHub's `Retry-After` becomes `next_retry_delay` in `activities/github.py`, so Temporal waits exactly as long as GitHub asked.

### Canary and rollback as a saga

**When to use it:** after beat 7.

**Where it is:** [`pipeline/workflow.py` lines 298–339](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/7a32f22e8407c56c5d07da68db3ee1c23715a5a1/agentic_sdlc/pipeline/workflow.py#L298-L339)

```python
for n in range(1, checks + 1):
    await workflow.sleep(timedelta(seconds=every))
    check = await self._check("canary")
    if not check.healthy:
        await self._roll_back(repo, sha, check)
        return False
```

**What you need to understand:**

- The bake is a durable timer. A worker restart mid-bake doesn't reset it.
- `_roll_back` runs the compensations in order: stop canary traffic, revert the merge through a PR, then reopen the issue with the findings. Each is an Activity with its own retries, so the rollback completes even if GitHub misbehaves.

**Docs:** [Saga pattern](https://docs.temporal.io/design-patterns/saga-pattern)

### Change the pipeline while runs are in flight

**When to use it:** after beat 8.

**Where it is:** [`agentic_sdlc/worker.py` lines 38–44](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/7a32f22e8407c56c5d07da68db3ee1c23715a5a1/agentic_sdlc/worker.py#L38-L44) and [`pipeline/workflow.py` line 70](https://github.com/temporal-sa/temporal-demo-agentic-sdlc/blob/7a32f22e8407c56c5d07da68db3ee1c23715a5a1/agentic_sdlc/pipeline/workflow.py#L70)

```python
deployment = WorkerDeploymentConfig(
    version=WorkerDeploymentVersion(deployment_name=config.DEPLOYMENT_NAME, build_id=config.BUILD_ID),
    use_worker_versioning=True,
    default_versioning_behavior=VersioningBehavior.PINNED,
)
```

**What you need to understand:**

- Pinned means a run, and the agents it starts, stay on the build they started on. v1 keeps serving them while v2 takes new runs.
- No `patched()` branches: the change ships as a new version.
- In this demo, v2 is the same code with `PIPELINE_VERSION=v2`, which turns on the security agent. Each version always runs with one value, so replay is safe. In a real project v2 is the next commit.

**Docs:** [Worker Versioning](https://docs.temporal.io/worker-versioning)

## Reset and tear down

- **Between audiences:** click **Reset session** in the bottom bar. It terminates this browser session's runs, empties its environments, clears faults, and closes its issues and PRs and deletes its branches on GitHub. That cleanup runs as a Standalone Activity on the worker. Other sessions aren't touched.
- **Back to v1:** press **5** and click **Make v1 current again**, or restart `scripts/dev.py`.
- **Nothing left running:** runs end on their own; environments close after 24 hours idle; faults expire after 10 minutes. Stop `scripts/dev.py` with Ctrl-C.

## When something goes wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| Top right says **no workers** and nothing moves | The worker was killed or crashed | Click **Start a worker**, or check `.logs/worker-v1.log` |
| A step is pink for a long time with `GitHub 401` or `403` | The GitHub token expired or lost access | [Rotate the GitHub token](#rotate-the-github-token) |
| A step is pink with `ProviderRejected` | A model name or key is wrong for that provider | Pick another model in **New run**, or check the key in `.env.local` |
| **Couldn't start the run: You already have 2 runs in progress** | The per-session cap | Let one finish or **Reset session** |
| The run stops at **Budget** | Estimated spend reached the run's budget | **Raise budget by $3**, or start runs with a higher budget |
| **Roll out v2** does nothing | The v2 worker didn't start | See `.logs/worker-v2.log` and `.logs/dev.log` |
| Storefront tab says nothing is deployed | No run has reached staging in this session | Expected until a run merges |

## Rotate the GitHub token

The fine-grained PAT is scoped to `temporal-sa/agentic-sdlc-storefront` with Contents, Pull requests and Issues set to read/write. It expires **2027-10-22**.

1. Open https://github.com/settings/personal-access-tokens/new?name=agentic-sdlc-demo&target_name=temporal-sa&expires_in=366&contents=write&pull_requests=write&issues=write
2. Under **Repository access**, choose **Only select repositories → agentic-sdlc-storefront**, then **Generate token**.
3. Put it in `.env.local` as `GITHUB_TOKEN`. For the catalog, update the `github` project secret.

## Run it locally

Tools: [uv](https://docs.astral.sh/uv/) 0.13+, Python 3.13 (uv installs it), Node 20+, the Temporal CLI 1.9+, Redis 7+. Optional: Docker, for `docker compose up --build`.

```bash
uv sync
npm --prefix web ci && npm --prefix web run build
uv run python scripts/dev.py
```

`.env.local` (gitignored) needs at least one of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, plus `GITHUB_TOKEN`. `scripts/dev.py` starts a Temporal dev server (persisted to `.logs/temporal.db`), Redis, the web server on :8000 and a v1 worker. It also listens on 127.0.0.1:8765 for the console's kill, start and roll-out buttons.

For a cheaper rehearsal, pick `claude-haiku-5-5` or `gemini-3.5-flash` for every role in **New run**.

Local troubleshooting:

- `uv run pytest` runs the pipeline tests with fake agents in about 20 seconds.
- `uv run python scripts/drive_run.py discount-code` runs a whole pipeline from the terminal and prints each step.
- Logs for every process are in `.logs/`.
