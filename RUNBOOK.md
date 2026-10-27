# Runbook: Agentic SDLC on Temporal

How to present the demo and reset it, written so someone who didn't build it can run it. Code links point at files on `main`; refresh them to commit permalinks when this branch merges.

## What it shows

AI agents working where developers already work: GitHub. A dependency bump breaks the build and an agent repairs it, explaining why on the PR. A person opens a PR and an agent reviews it behind a required check, then fixes what it's asked to. An issue goes all the way to production with parallel coding agents. Everyone talks to the agents on GitHub, in comments. The console only watches: it shows what the agents and the Workflows around them are doing, live.

Temporal keeps that work alive through crashes, days-long waits for people, flaky APIs and a bad canary, and records every step.

The seven decision criteria in the console's left rail are the problems, and each beat below ticks one. A tick only appears when a run recorded the evidence.

| | Criterion | Beat |
|---|---|---|
| D1 | An agent crash loses no work and re-spends no tokens | 3 |
| D2 | Humans stay accountable without babysitting | 5 |
| D3 | Parallel agents, flaky APIs and spend stay under control | 3, 5 |
| D4 | A bad change in production is caught and undone | 6 |
| D5 | The pipeline can change while runs are in flight | 7 |
| D6 | Every agent action is auditable | 3, 8 |
| D7 | People can steer agents mid-flight without starting over | 4, 5 |

## Before you start

- **Where it runs.** Hosted, the demo is `https://agentic-sdlc.tmprl-demo.cloud`, and a crashable workspace gives you your own copy with a crash button. Locally, run [`scripts/dev.py`](#run-it-locally).
- **Agent comments show your GitHub account as the author.** The demo posts with its PAT, so GitHub attributes every comment to the token's owner. Each one starts with who really wrote it: **🤖 Planner agent**, **🤖 Coding agent**, **🤖 Code review agent**, or **⚙️ Pipeline** for the Workflow's own messages (gates, status, the release). Worth saying out loud the first time the audience sees one.
- **It's all on one real repo:** [temporal-sa/agentic-sdlc-storefront](https://github.com/temporal-sa/agentic-sdlc-storefront). Every copy of the demo shares it. Each copy labels what it starts (`agentic-sdlc:<copy>`) and only acts on its own label, so you never pick up someone else's PR. `main` is locked: the agents get PRs to "ready to merge", and nothing merges.
- **Real calls cost real money.** A dependency repair or review costs about $0.10 to $0.60 in estimated model spend; an issue run $0.20 to $1.50. The per-run budget is $3.
- **Ship one issue run before the audience arrives.** Act 1 opens with the result. Start an issue scenario on auto merge, reply `/approve` and `/ship` on the issue, and let it finish (about 5 minutes).
- **Open three tabs:** the console, the Temporal UI, and [the repo's pull requests](https://github.com/temporal-sa/agentic-sdlc-storefront/pulls). Sign in to GitHub as yourself; you'll be commenting.
- **The GitHub token expires on 2027-10-22.** See [Rotate the GitHub token](#rotate-the-github-token).

## Start it

Open the console. A healthy start shows **v1 + v2 workers** in green in the top right and **Start a scenario** in the bottom bar.

Keys: **1–6** switch acts, **N** starts a scenario.

## The walkthrough

### 1. The result (act 1)

**Do:** press **1**.

**The audience sees** the storefront in production with the change your warm-up run shipped, its time from issue to production, its model calls and its estimated cost.

**Say:** "Agents planned, coded, reviewed and shipped this, and we talked to them on GitHub the whole way. Temporal ran everything around them. Let's see why that matters."

**Proof:** the Issue, Pull request and Event History links on the card.

### 2. Why agentic SDLC, and why it's hard (acts 2 and 3)

**Do:** press **2**, then **3**.

**The audience sees** three cited figures (coding +30–40% but team productivity under +10%; 84–90% of developers use AI tools but about 13% of teams run it across the lifecycle; the bottleneck moving to review and governance), then the seven criteria, each with the pain in someone's words.

**Say:** "Agents across the lifecycle fail the way distributed systems fail. They crash, they wait on people, they run in parallel, they hit rate limits, and they ship bad changes. Those seven cards are what you'd need proven before trusting them. Let's prove them."

**Proof:** every figure links to its source. All the sources are in [docs/story.md](docs/story.md).

### 3. A dependency bump breaks the build; an agent fixes it (D1, D3, D6)

**Do:** press **4**, click **Start a scenario**, choose **Dependency bump that breaks the build**, and click **Start on GitHub**. The dialog says it's waiting for the bump Action; in about 30 seconds the repo's *Dependency bump* Action has pushed a Dependabot-style branch (a commit from `github-actions[bot]`), and the dialog shows **Open the pull request on GitHub ↗**.

**Do:** click it. GitHub's "Open a pull request" page has the title ("Bump orbit-money from 1.0.0 to 2.0.0"), a Dependabot-style description and the labels filled in. Click **Create pull request**.

**Say:** "I'm the developer here: a bump landed and I've opened the PR, like on any Monday."

**The audience sees** the new PR's checks show **agentic-sdlc/agent-review — Required — pending: Agent is checking whether this upgrade breaks anything**. Back in the console the run has appeared (the webhook started it) and the graph moves: **Tests** fail.

**Say:** "I didn't tell our tool anything. GitHub told Temporal a PR was opened, and a Workflow started for it. The agent's check is required, so this PR can't merge until the agent says so."

**Do:** while **Fix** is active, kill the worker: **Kill the worker** locally, or crash the worker from the catalog workspace. Start it again after about 5 seconds.

**The audience sees** **no workers** in yellow and a banner saying the run is paused, not lost. After the restart the coding agent carries on: new tool calls in its card, the token count continuing. **D1** ticks when its turn completes.

**Optionally**, click **Inject 429** on the coder's card: its lane turns pink, "Attempt 2: model provider returned 429", and recovers. **D3** ticks.

**The audience sees**, on the PR: a comment, "🔎 Why orbit-money 2.0.0 breaks the build", citing the package's changelog (amounts are integer cents now; `roundCents` is gone); a commit from the agent on the bump branch; the PR description rewritten with **What changed**, **What broke** (the failing test output), **What we changed, and why**, and **How to verify**; a review from the review agent; then the check turns green: **Upgrade fixed, tested and reviewed: ready to merge**. The merge button stays disabled, because `main` is locked.

**Say:** "Dependabot tells you a bump is available. This tells you why it breaks, fixes it, and explains the fix to the human who'll merge it. Every step is an Activity in a Workflow, so a crash mid-fix cost us nothing."

**Proof:** **Open in Temporal**: the `DependencyRepair` Workflow, its `coder` child with the `diagnose` and `code` turns, and `update_pull_request`. **D6** ticks when the run ends.

### 4. An agent reviews a PR, then fixes it on request (D7)

**Do:** **Start a scenario** → **A PR for the agent to review**. Open the PR on GitHub.

**The audience sees** "Add a bulk discount for 3 or more items", with the agent's check pending, then a review with inline comments on `src/cart.js`. The sample has three planted bugs (see [`sandbox-samples/discount-rounding`](sandbox-samples/discount-rounding/README.md)): a cart of exactly 3 items gets no discount (`>` instead of `>=`), tax is charged on the undiscounted subtotal, and the discount isn't rounded to cents. Reviewers usually catch at least two. If it requests changes, the check says the agent wants changes.

**Do:** reply on the PR: *"Please fix the rounding and the 3-item case."*

**The audience sees** the comment in the console's **Conversation** tab within a second or two. The planner triages it as **feedback**, a coding agent pushes a fix to the PR branch, the tests run, the reviewer looks again, and the agent answers on the PR ("🤖 Coding agent … 🛠️ Addressed: …"). The check turns green. **D7** ticks.

**Say:** "The developer never left GitHub. The comment is a Signal to a Workflow that was waiting, at no cost, for someone to say something."

**Optional:** ask for something out of scope, *"Also add gift cards"*: the agent explains on the PR that it's outside this PR's scope and suggests an issue.

### 5. An issue all the way to production (D2, D3, D7)

**Do:** **Start a scenario** → **An issue, all the way to production**. Choose **Write my own** with something open-ended, such as *"Let customers save items for later"* / *"Customers want to move cart items to a saved-for-later list instead of deleting them."* Keep **Auto merge**. Open the issue on GitHub.

**The audience sees** the planner's questions as a comment on the issue, and in the console the gate card: **Reply on issue #N ↗**.

**Do:** answer on the issue. Then the plan appears as a comment: "Reply `/approve` to start building, or reply with what to change." Reply with a change first if you like (the planner revises it), then `/approve`. **D2** ticks.

**Say:** "The Workflow is parked on a durable wait. No process is holding it open. It could sit here for a weekend and cost nothing; GitHub's webhook delivers each reply to it."

**Do:** while the coders' lanes are active, comment on the issue: *"Keep the saved-items logic pure and tested, like the rest of cart.js."*

**The audience sees** the comment queued on both coders' cards. When each agent's current turn ends, the comment becomes its next turn, and it adjusts the code it already pushed. **D7** ticks; **D3** ticks when the parallel build finishes.

**The audience sees** the PR open with the pending agent check, the reviewer's review, then "👀 Reviewed. Comment here to request changes; the agents merge this in 1 min unless someone does."

**Do:** comment on the PR: *"The button should say 'Move to cart', not just an icon."* A coding agent fixes it on the PR branch and the reviewer looks again. **Optional, a scope change:** comment *"Also show how many items are saved in the header"*. The planner calls it a scope change: the PR closes with an explanation, the plan is revised on the issue and needs `/approve` again.

**Do:** let the window close. After staging, the issue says "🚦 Staging passed its checks. Try it: …/apps/gh/staging/. Reply `/ship` …, `/stop <reason>`, or comment with what to change …" Open staging, then reply `/ship`. (Optional: comment an adjustment first, like "make the discount banner bigger". A coding agent changes it on staging, staging redeploys, and the issue asks for `/ship` again. A new requirement gets a reply suggesting its own issue.)

### 6. Break the canary (D4)

**Do:** while **Canary** is active (it bakes for about 90 seconds), click **Break the canary** in the bottom bar.

**The audience sees** the canary checks fail. The pipeline stops canary traffic, opens and merges a revert PR, reopens the issue with the findings, and the run ends **Rolled back**. **D4** ticks.

**Say:** "That's a saga. Each step has its compensation, and the Workflow runs them in order. Nobody got paged."

To show a clean ship instead, skip this beat. The canary promotes after its checks.

### 7. Change the pipeline mid-run (D5)

**Do:** start an issue scenario and `/approve` its plan. While it's coding, press **5** and click **Roll out v2** (confirm). Then start another issue scenario.

**The audience sees** **New runs start on** change from v1 to v2, and v1 show "draining: pinned runs still finishing". The in-flight run keeps **pipeline v1** and finishes without a security stage. The new run shows **pipeline v2**, with a **Security** step. **D5** ticks when the v1 run finishes after a v2 run has started.

**Say:** "Both versions are deployed. Rolling out v2 is one call to Temporal. Each run is pinned to the version it started on, so running work finishes exactly as it began, new work gets the new pipeline, and nothing is patched in place."

**Note:** the rollout applies to everyone on the same deployment. In the shared hosted demo, switch back with **Make v1 current again** afterwards; a crashable workspace has its own deployment.

### 8. Proof (act 6, D6)

**Do:** press **6**.

**The audience sees** the recap and the scorecard, with each tick linking to the Workflow that shows it.

**Say:** "Every model call, tool call, comment, approval and retry is in Event History. That's your audit trail, and you didn't build it."

## Show it in code

### Each scenario is one Workflow method

**When to use it:** after beat 3, or when someone asks where the orchestration lives.

**Where it is:** [`agentic_sdlc/pipeline/dependency.py`](agentic_sdlc/pipeline/dependency.py), `DependencyRepair.run`

```python
pr = await self._take_pull_request()
...
tests = await self._test_the_bump(workspace, package, after)
if tests.passed:
    await self._compatible(pr, package, after)
else:
    plan = _plan(package, after)
    diagnosis = await self._diagnose(pr, workspace, package, before, after, tests)
    fix = await self._fix(workspace, plan, diagnosis)
    await self._explain(pr, package, before, after, diagnosis, tests, fix)
    await self._review(pr, plan)
    await self._converse_on_pr(pr, plan)
```

**What you need to understand:**

- Agents decide inside a step; the Workflow decides the process: test first, diagnose before editing, verify the fix with the real tests, have a second model review it. That's the split that makes nondeterministic agents safe to run unattended.
- It's ordinary Python. Each line can take seconds or days. `IssueToProduction.run` in [`pipeline/workflow.py`](agentic_sdlc/pipeline/workflow.py) is the same idea for the full lifecycle.

**Docs:** [Workflows](https://docs.temporal.io/workflows)

### GitHub drives the Workflows, with no polling

**When to use it:** after beat 3 or 4, or when someone asks how a comment reaches an agent.

**Where it is:** [`agentic_sdlc/web/webhooks.py`](agentic_sdlc/web/webhooks.py) (`route`, `handle`) and [`pipeline/base.py`](agentic_sdlc/pipeline/base.py) (`github_comment`, `_wait_for_people`)

```python
# webhooks.py: a comment on PR #13 is a Signal to every running Workflow watching it
query = f"AgenticSdlcGitHubRefs = '{action.ref}' AND ExecutionStatus = 'Running'"
async for execution in temporal.list_workflows(query):
    await temporal.get_workflow_handle(execution.id).signal(action.signal, action.arg)
```

```python
# base.py: a gate waits for a comment, or for the PR to close, or for its timeout
await workflow.wait_condition(lambda: bool(self._inbox) or (pr in self._closed_prs if pr else False),
                              timeout=remaining)
```

**What you need to understand:**

- The web server verifies GitHub's signature, then routes: a new labeled issue or a PR starts a Workflow with a deterministic ID (`<queue>.gh.pr-13`), so a redelivered webhook can't start a second one; a comment becomes a `github_comment` Signal. The runs to Signal are found with a Visibility query on a search attribute each run keeps up to date. Visibility queries aren't billable.
- A comment starting with a command the gate is waiting for (`/approve`, `/merge`, `/ship`, `/stop`, `/continue`) decides it. Anything else is conversation, which the planner triages: feedback for a coder, or a scope change that goes back to planning. At the production gate, feedback is made on staging and a scope change gets a reply suggesting a new issue. While coders work and nobody's at a gate, a comment steers them.
- GitHub doesn't redeliver a failed webhook on its own, so each gate reads the comments once when it opens and de-duplicates by comment ID. There's no polling loop.
- Everything the agents post carries a hidden marker, so they never read their own comments back as feedback.

**Docs:** [Message passing](https://docs.temporal.io/develop/python/workflows/message-passing), [List Filters](https://docs.temporal.io/list-filter)

### Agents are Workflows; a dead worker loses nothing

**When to use it:** after beat 3's crash.

**Where it is:** [`agentic_sdlc/agents/workflows.py`](agentic_sdlc/agents/workflows.py) (`CoderAgent.diagnose`, `CoderAgent.code`) and `AgentRun._turn` in [`pipeline/base.py`](agentic_sdlc/pipeline/base.py)

```python
@agent.accepts
async def diagnose(self, request: DiagnoseRequest) -> DiagnoseReply:
    """Work out why a dependency upgrade on this branch breaks the build, without editing."""
    result = await CODER.run(prompt, instructions=DIAGNOSE_INSTRUCTIONS, output_type=Diagnosis,
                             deps=self._deps(), message_history=self._history, model=self._init.model.key, ...)
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

- `CODER` is a Pydantic AI agent wrapped in `TemporalAgent`, so each model request is an Activity. Each tool call (read the changelog in `node_modules`, edit a file, run the tests) is a harness activity tool.
- The run talks to the agent through an Activity. A retried Activity re-sends with the same update ID, Temporal returns the original acceptance, and it waits for the same turn. The agent kept running through the crash.
- One agent class serves every provider: the model is picked per copy in **Start a scenario**, by key, such as `anthropic:claude-sonnet-5-5`.

**Docs:** [Pydantic AI durable execution with Temporal](https://ai.pydantic.dev/durable_execution/temporal/), [temporal-agent-harness](https://github.com/temporal-sa/temporal-agent-harness)

### The branch is the agent's durable workspace

**When to use it:** after beat 3, when someone asks about files on the dead worker.

**Where it is:** [`agentic_sdlc/agents/tools.py`](agentic_sdlc/agents/tools.py) (`checkout`, `_commit_and_push`)

```python
async def checkout(workspace: Workspace) -> Path:
    ...
        await _git(root, "fetch", "--quiet", "origin", workspace.branch)
        await _git(root, "reset", "--quiet", "--hard", f"origin/{workspace.branch}")

async def _commit_and_push(root, workspace, path, message) -> str:
    ...
    await _git(root, "push", "--quiet", "origin", f"HEAD:{workspace.branch}")
```

**What you need to understand:**

- Every write is committed and pushed before the tool returns, and every tool starts by syncing to the branch head. A replacement worker rebuilds the checkout from GitHub, and you can watch the agent's progress as commits on the PR.

### The agent gates the PR

**When to use it:** after beat 3 or 4.

**Where it is:** `AgentRun._set_check` in [`pipeline/base.py`](agentic_sdlc/pipeline/base.py), and the `main` ruleset made by [`scripts/setup-github.sh`](scripts/setup-github.sh)

```python
async def _set_check(self, branch: str, state: str, description: str) -> None:
    await workflow.execute_activity_method(GitHubActivities.set_commit_status, StatusInput(
        repo=self._request.repo, branch=branch, state=state, description=description,
        target_url=self._request.console_url), **GITHUB)
```

**What you need to understand:**

- `agentic-sdlc/agent-review` is a required status check on `main`: pending while the agent works, then success or failure. The check's link opens the console.
- In this shared repo `main` is also locked, so nothing merges. In a real repo, this is the line between "the agent is done and happy" and "a human can merge".

### Canary and rollback as a saga

**When to use it:** after beat 6.

**Where it is:** `_canary` and `_roll_back` in [`pipeline/workflow.py`](agentic_sdlc/pipeline/workflow.py)

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
- `_roll_back` runs the compensations in order: stop canary traffic, revert the merge through a PR, then reopen the issue with the findings. Each is an Activity with its own retries.

**Docs:** [Saga pattern](https://docs.temporal.io/design-patterns/saga-pattern)

### Change the pipeline while runs are in flight

**When to use it:** after beat 7.

**Where it is:** [`agentic_sdlc/versions.py`](agentic_sdlc/versions.py) and [`agentic_sdlc/worker.py`](agentic_sdlc/worker.py)

```python
deployment = WorkerDeploymentConfig(
    version=WorkerDeploymentVersion(deployment_name=config.DEPLOYMENT_NAME, build_id=config.BUILD_ID),
    use_worker_versioning=True,
    default_versioning_behavior=VersioningBehavior.PINNED,
)
```

**What you need to understand:**

- One worker runs per version (build IDs `v1` and `v2`), each joining the same Worker Deployment. Pinned means a run, and the agents it starts, stay on the build they started on.
- Rolling out is changing the deployment's current version: one API call, no redeploy. v1 keeps serving its pinned runs until they finish ("draining").
- No `patched()` branches: the change ships as a new version. In this demo v2 is the same code with `PIPELINE_VERSION=v2`; in a real project v2 is the next commit.
- The deployment is named `<task queue>-pipeline`, so each crashable workspace has its own.

**Docs:** [Worker Versioning](https://docs.temporal.io/worker-versioning)

## Reset and tear down

- **Between audiences:** click **Reset** in the bottom bar. It ends this copy's runs, empties its environments, clears faults, closes its labeled issues and PRs and deletes their branches. Other copies' work isn't touched.
- **One run:** **Stop run** cancels it; its issue or PR stays on GitHub as it is.
- **Back to v1:** press **5** and click **Make v1 current again**. Do this after every D5 beat in the shared hosted demo.
- **Nothing left running:** PR conversations close after a quiet day (the agent closes the PR with a note); every run ends within 3 days; environments close after 24 hours idle; faults expire after 10 minutes.

## When something goes wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| A scenario started on GitHub but no run appears | The webhook didn't arrive | Locally, check `.logs/webhooks.log` and that `dev.py` printed the GitHub line. Hosted, check the repo's **Settings → Webhooks → Recent deliveries** (redeliver from there) |
| The bump scenario says the Action didn't push its branch | The Action failed or is queued | Check the repo's **Actions** tab, then start the scenario again |
| The bump PR is open but no run appears | It was opened without this copy's owner label | Add the label (`agentic-sdlc:<copy>`) to the PR; the run starts then |
| `dev.py` says webhook forwarding didn't start | Someone else is forwarding the repo, or your gh login lacks `admin:repo_hook` | Wait for them to stop, or `gh auth refresh -s admin:repo_hook` |
| The agent comments "This demo already has 3 agent runs in progress" | The per-copy cap | Stop a run, or **Reset**, then reopen the issue or PR |
| A comment doesn't change anything | The gate wants a command (`/continue` or `/stop` at the budget gate), or the comment started with a command the gate isn't waiting for | The **Conversation** tab says "Noted; the run is waiting for …" |
| Top right says **no workers** and nothing moves | The worker was killed or crashed | Click **Start a worker**, or check `.logs/worker-v1.log` |
| A step is pink with `GitHub 401` or `403` | The GitHub token expired or lacks a permission | [Rotate the GitHub token](#rotate-the-github-token) |
| A step is pink with `ProviderRejected` | A model name or key is wrong for that provider | Pick another model in **Start a scenario**, or check the key |
| The run stops at **Budget** | Estimated spend reached the run's budget | Reply `/continue` on the issue or PR |
| **Roll out v2** says the version has no pollers | The v2 worker isn't running | Locally see `.logs/worker-v2.log`; hosted, check the `worker-v2` component |

## Rotate the GitHub token

The fine-grained PAT is scoped to `temporal-sa/agentic-sdlc-storefront` with Contents, Pull requests, Issues, Commit statuses and Actions set to read/write. It expires **2027-10-22**.

1. Open https://github.com/settings/personal-access-tokens/new?name=agentic-sdlc-demo&target_name=temporal-sa&expires_in=366&contents=write&pull_requests=write&issues=write&statuses=write&actions=write
2. Under **Repository access**, choose **Only select repositories → agentic-sdlc-storefront**, then **Generate token**.
3. Put it in `.env.local` as `GITHUB_TOKEN`. For the catalog, run `ROTATE_RUNTIME=0 scripts/create-catalog-secrets.sh` to update the `github` project secret.

## Set up GitHub

`scripts/setup-github.sh` (as a repo admin) creates the labels, puts `sandbox-template/` on `main` and the `baseline` tag, pushes the review sample branch, and requires the agent's check on `main`. With `--webhook-url https://agentic-sdlc.tmprl-demo.cloud/webhooks/github` it also points the repo's webhook at the hosted demo, signed with the `GITHUB_WEBHOOK_SECRET` from the catalog's `runtime` secret. Re-run it after changing the template or rotating that secret.

## Run it locally

Tools: [uv](https://docs.astral.sh/uv/) 0.13+, Python 3.13 (uv installs it), Node 20+, the Temporal CLI 1.9+, Redis 7+, and the GitHub CLI signed in with `admin:repo_hook`.

```bash
uv sync
npm --prefix web ci && npm --prefix web run build
uv run python scripts/dev.py
```

`.env.local` (gitignored) needs at least one of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, plus `GITHUB_TOKEN`. `scripts/dev.py` starts a Temporal dev server (persisted to `.logs/temporal.db`) with the search attribute the webhook needs, Redis, the web server on :8000, a worker per pipeline version, and `gh webhook forward` relaying the repo's events to the web server. It also listens on 127.0.0.1:8765 for the console's kill and start buttons.

Your laptop is its own copy of the demo: task queue `local-<your GitHub login>`, acting only on items labeled `agentic-sdlc:local-<login>`. "Start a scenario" labels what it opens; to drive the laptop from GitHub by hand, add that label to your issue or PR. GitHub allows one forwarder per repo at a time.

For a cheaper rehearsal, pick `claude-haiku-5-5` or `gemini-3.5-flash` for every role in **Start a scenario**.

Local troubleshooting:

- `uv run pytest` runs the Workflow tests with fake agents and the webhook routing tests in about 90 seconds.
- `uv run python scripts/drive_run.py dependency` (or `review`, `issue`) runs a scenario from the terminal, answering on GitHub with `gh`, and prints each step. `scripts/drive_conversation.py` rehearses every steering beat of the issue flow.
- Logs for every process are in `.logs/`.
