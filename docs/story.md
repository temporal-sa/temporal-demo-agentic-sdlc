# The story: why agentic SDLC, and why Temporal

This is the narrative the console, the deck and the RUNBOOK share. It follows the buildkit's
use-case rule (one situation, a few specific problems, each visible on screen) and the three
MEDDPICC letters that shape a demo: the pain in the audience's words, the decision criteria
they need proven, and metrics with sources.

## The sentence

AI agents work in your GitHub repo alongside your developers. When a dependency bump breaks the
build, an agent works out why, fixes it and explains it on the PR. It reviews PRs behind a
required check. It takes an issue to production with parallel coding agents. People steer all of
it in comments. Temporal keeps that work alive through crashes, days-long waits for people,
flaky APIs and a bad canary, and records every step.

## What people mean by agentic SDLC

Agents working across the whole software lifecycle, with people still accountable for the
result. Forrester's June 2026 framing: in 2023–24 agents wrote code and unit tests; in 2026 they
span planning, design, build, test and delivery, increasingly orchestrated together. "Humans
stay accountable, but AI does more of the execution."

The common mental model is a pipeline:

> issue → spec/plan → human approval → parallel coding agents → tests and self-repair →
> AI review and a human merge gate → progressive deploy → monitor and roll back

with autonomy set per action and environment. Port puts it as: "A deploy agent might run free
in staging but need approval in production, or run on its own at noon but check with a human
at 3 a.m."

The products audiences point to in late 2026 include GitHub Agent HQ (Copilot, Claude and Codex
assigned from issues), AWS's Kiro autonomous agent and its "frontier agents", OpenAI Codex,
Google Jules, Devin, Cursor background agents and Factory, plus spec-driven development (Kiro,
GitHub Spec Kit). This demo positions Temporal as the orchestration layer under any of them,
not as another coding agent.

## Delegate-and-review, with people in the loop

Comparison guides split agentic coding tools into *delegate-and-review* (Devin, Jules, Codex,
Factory: hand over an issue, get back a PR) and *in-the-loop* (Cursor, Copilot, Claude Code: a
developer steers as the agent works). An agentic SDLC needs both: the long, unattended
issue-to-production loop, and developers steering it where they already work. This demo keeps
people in it as collaborators, not only approvers, and entirely on GitHub: GitHub's webhooks start
the runs (a bump PR, a person's PR, an issue labeled `agent`), and every comment reaches the
running Workflow as a Signal. The planner asks questions on the issue; developers reply, steer
coders mid-task and comment on PRs; the Workflow routes each comment to the right agent. Feedback
is fixed on the spot; a new requirement voids the plan approval and goes back through planning,
without starting the run over. The console is only a window onto what the agents are doing.

The dependency-repair scenario is the everyday version: a Dependabot-style bump breaks the build,
and instead of a red PR waiting for someone, an agent diagnoses it from the failures and the
changelog, fixes the code on the bump branch and rewrites the PR to explain the upgrade, what broke
and what changed. Its required check holds the PR until it's done.

## Identify pain (in their words)

| Pain | Their words | Source |
|---|---|---|
| Faster coding didn't speed up delivery | "Coding may improve by 30% to 40%, but if planning, testing, and release remain manual, overall team productivity often increases by less than 10%." | [Forrester, Diego Lo Giudice, June 8 2026][forrester] |
| The bottleneck moved | "The new bottleneck is shifting from code generation to review, quality, governance, and architecture." | [Ciklum, Julian Beckers, July 12 2026][ciklum] |
| Agents crash and hang | "Even a well prompted agent may go off the rails and crash, fail to produce any changes, hang/freeze, or otherwise fail to complete, and need to be retried." | [Duolingo Engineering, Jesse Welch, Dec 4 2025][duolingo] |
| Parallel agents multiply cost | "Four agents in parallel is four times the burn." | [KanBots][kanbots] |
| State loss, rate limits, governance | "Hallucinations on edge cases, multi-step workflows that lose state, rate limits that spike costs, and security risks and governance failures as agents proliferate." | [Veza Digital, Matt Biggin, April 17 2026][veza] |
| Speed without a way back | "An ungoverned one makes change failure rate and MTTR worse, because nothing is there to catch a bad change before it spreads." | [Port, Zohar Einy][port] |
| No audit trail | "What you get is agent sprawl: agents taking destructive actions with no audit trail, no respect for PII, and no scoped credentials." | [Port][port] |

## Decision criteria (each one is a live beat and a slide)

| | Criterion | Problem appears | Resolved on screen | Proof | Temporal feature |
|---|---|---|---|---|---|
| D1 | An agent crash loses no work and re-spends no tokens | Kill the worker while the agent fixes a broken upgrade; the console says no worker is running | A new worker picks the turn up; the agent continues from its last completed call; the token meter doesn't jump | The agent's Event History: each model and tool call ran once | Activities with heartbeats; Event History |
| D2 | Humans stay accountable without babysitting | The run waits for `/approve` on the plan and `/ship` for production, and the agent's required check holds PRs | A reply on GitHub resumes it, minutes or days later | The gate's wait time; the Signal in history | Durable waits; Signals from webhooks |
| D3 | Parallel agents, flaky APIs and spend stay under control | An injected 429 on one coder; the run reaches its budget | That lane retries (pink) while the others finish; the run pauses until someone replies `/continue` | Retry attempts in history; the budget gate | Child Workflows; Retry Policies |
| D4 | A bad change in production is caught and undone | "Break the canary" makes its assets fail | The pipeline stops canary traffic, reverts the merge through a PR and reopens the issue | Compensation steps in history; the revert PR | Durable timers; saga |
| D5 | The pipeline can change while runs are in flight | Roll out v2 (adds a security-review agent) mid-run | The running run finishes on v1; new runs show the new stage | Each run's pinned version | Worker Versioning, Pinned |
| D6 | Every agent action is auditable | Shown throughout | The upgrade's diagnosis, fix, PR rewrite and review, step by step | Open any run in Temporal | Event History |
| D7 | People can steer agents mid-flight without starting over | The planner asks on the issue; a developer comments on a PR, or on the issue while coders work | Answers go into the plan; feedback is fixed on the PR branch and re-reviewed; a new requirement closes the PR and re-plans; an issue comment becomes the coders' next turn | The Conversation tab; the agent's reply on the PR; the `github_comment` Signals in history | Signals from GitHub webhooks; durable waits |

## Metrics

Only cited figures appear on slides and in the console's story acts:

- Coding +30–40%, team productivity often under +10% while planning, testing and release stay manual ([Forrester][forrester]).
- Roughly 84–90% of developers use AI tools for coding; about 13% of teams have AI operating across the full lifecycle ([Keyhole Software, Sept 2 2026][keyhole], which cites Gartner, DORA and others side by side).

Live numbers in the console come from the running system and are labeled: tokens, estimated
model cost (from the genai-prices database, not a provider bill), attempts and time waiting.

## Claims we don't make

- That Codex runs on Temporal. OpenAI has spoken about building durable orchestration on
  Temporal, but we found no primary source tying Codex to it, and the press release we tried to
  check refused automated access.
- Any company's AI budget story or funding figure we couldn't read in a primary source.

## The acts

1. **The result.** A run that shipped before the talk: the storefront change is live.
2. **Why agentic SDLC.** Coding got faster; shipping didn't.
3. **Why it's hard.** The seven criteria, each with the pain in someone's words.
4. **Live run.** A broken dependency bump repaired (D1, D3, D6), a PR reviewed and fixed on
   request (D7), an issue taken to production by comments (D2, D7) and a bad canary undone (D4).
5. **Change the pipeline.** D5.
6. **Proof.** The scorecard, each tick linked to the Workflow that shows it.

## Customer story

Duolingo's feature-flag-removal agent: "We built this agent on the workflow orchestration
service Temporal using the Codex CLI, a pattern we intend to extend to future agents." The
agent clones the repo, runs Codex CLI, and opens a PR, and Temporal retries the runs that "go off
the rails" ([Duolingo Engineering][duolingo]).

[forrester]: https://www.forrester.com/blogs/agentic-software-development-takes-the-lead-from-code-assistants-to-orchestrated-sdlc-agents/
[keyhole]: https://keyholesoftware.com/agentic-ai-in-the-software-development-lifecycle-2026-adoption-and-impact-data/
[ciklum]: https://www.ciklum.com/blog/ai-revolutionize-software-development-lifecycle/
[duolingo]: https://blog.duolingo.com/buildingaiagents
[kanbots]: https://kanbots.dev/learn/parallelize-ai-coding-agents
[veza]: https://vezadigital.com/post/best-ai-agents
[port]: https://www.port.io/blog/agentic-sdlc-software-lifecycle-rebuilt-around-agents
