// The live run: the pipeline graph across the top, then the timeline, the agents at work (or
// what the run is waiting for people to say on GitHub), and the proof: code, storefront and GitHub.
// Nothing here acts on the agents; people do that on GitHub.

import { useEffect, useMemo, useState } from "react";
import { ago, api, explainFailure, money, temporalLink, type Config, type Feedback, type Retry, type RunState, type RunView, type Step } from "../api";
import type { AgentFeed } from "../live";
import { useNow } from "../live";
import { StepList } from "../kit/StepList";
import { CodePanel } from "./CodePanel";
import { GraphLegend, PipelineGraph, hiddenUntilUsed } from "./PipelineGraph";
import { Markdown } from "./Markdown";
import { GitHubPanel, StorefrontPanel } from "./Panels";

const RUN_STATUS_LABEL: Record<string, string> = {
  running: "Running", waiting: "Waiting for GitHub", completed: "Done", rolled_back: "Rolled back",
  failed: "Failed", cancelled: "Stopped", stopped: "Stopped before production",
};
const DONE_LABEL: Record<string, string> = { issue: "Shipped", dependency: "Ready to merge", review: "Reviewed" };
const RUN_STATUS_VOCAB: Record<string, string> = {
  running: "active", waiting: "waiting", completed: "completed", rolled_back: "completed", failed: "failed", cancelled: "skipped",
  stopped: "skipped",
};

/** Which step each retrying Activity belongs to, so the graph and timeline turn pink there. */
function retryingSteps(view: RunView): Map<string, Retry> {
  const result = new Map<string, Retry>();
  const byAgent = new Map(view.run.agents.map((a) => [a.workflow_id, a]));
  for (const retry of view.retries) {
    const agent = byAgent.get(retry.workflow_id);
    if (agent) {
      const key = agent.lane ? `code:${agent.lane}` : agent.role.startsWith("reviewer") ? "review" : agent.role === "planner" ? "plan" : "pr";
      result.set(key, retry);
    } else {
      for (const s of view.run.steps) if (s.status === "active") result.set(s.key, retry);
    }
  }
  return result;
}

export function RunConsole({ view, feeds, config, onError }: {
  view: RunView; feeds: Record<string, AgentFeed>; config: Config; onError: (m: string) => void;
}) {
  const { run } = view;
  const now = useNow();
  const retries = useMemo(() => retryingSteps(view), [view]);
  const active = run.steps.find((s) => s.status === "waiting") ?? [...run.steps].reverse().find((s) => s.status === "active" && !s.lane)
    ?? [...run.steps].reverse().find((s) => s.status !== "pending" && s.status !== "skipped" && !s.lane) ?? null;
  const [selected, setSelected] = useState<string | null>(null);
  const focus = selected ?? active?.key ?? null;
  const [side, setSide] = useState<"code" | "talk" | "store" | "github">("code");
  // When someone says something to the agents, show the conversation.
  const said = run.conversation.length;
  useEffect(() => { if (said > 0) setSide("talk"); }, [said]);

  // Agent lanes sit under the build step they belong to.
  const ordered = run.steps.flatMap((s) => (s.lane ? [] : s.key === "build" ? [s, ...run.steps.filter((l) => l.lane)] : [s]));
  const timeline = ordered
    .filter((s) => !hiddenUntilUsed(s))
    .map((s) => toTimelineStep(s, retries.get(s.key), now));
  const noWorker = view.workers.length === 0 && !view.closed;
  const budgetShare = Math.min(1, run.usage.cost_usd / Math.max(0.01, run.budget_usd));

  return (
    <div className="run-console">
      <header className="run-head">
        <div className="run-title">
          <span className="status-badge" data-status={RUN_STATUS_VOCAB[run.status]}>
            {run.status === "completed" ? DONE_LABEL[run.kind] : RUN_STATUS_LABEL[run.status]}
          </span>
          <h2 className="heading">{run.title}</h2>
          {run.version && <span className="badge idle" title="Worker Deployment Version this run is pinned to">pipeline {run.version}</span>}
          <GitHubLinks run={run} repo={config.repo} />
        </div>
        <div className="run-meters">
          <div className="meter" title="Estimated from token counts with the genai-prices database; not a provider bill">
            <span className="stat-label">Estimated model cost</span>
            <span className="meter-value">{money(run.usage.cost_usd)} <span className="muted">of {money(run.budget_usd)} budget</span></span>
            <span className="meter-bar"><span style={{ width: `${budgetShare * 100}%` }} data-status={budgetShare >= 1 ? "waiting" : "active"} /></span>
          </div>
          <div className="meter">
            <span className="stat-label">Tokens</span>
            <span className="meter-value">{(run.usage.input_tokens + run.usage.output_tokens).toLocaleString()}</span>
            <span className="muted">{run.usage.requests} model calls</span>
          </div>
          <a className="button small secondary ultraviolet" href={temporalLink(config.temporal_ui, view.workflow_id, view.temporal_run_id)} target="_blank" rel="noreferrer">Open in Temporal</a>
        </div>
      </header>

      {noWorker && (
        <div className="state-banner" role="status">
          <strong>No worker is running.</strong>
          <span>The run is paused, not lost. Temporal hands every unfinished step to the next worker that starts, and agents continue from their last completed call.</span>
        </div>
      )}

      <section className="card graph-card">
        <PipelineGraph run={run} selected={focus} onSelect={(k) => setSelected(k === selected ? null : k)} retrying={new Set(retries.keys())} />
        <GraphLegend />
      </section>

      <div className="run-grid">
        <section className="card timeline-card">
          <div className="card-head"><span className="eyebrow">What the Workflow has done</span></div>
          <StepList steps={timeline} />
        </section>

        <section className="card focus-card">
          {run.gate ? (
            <GateCard view={view} now={now} />
          ) : (
            <>
              {run.diagnosis && run.steps.some((s) => s.key === "fix" && s.status === "active") && <DiagnosisCard run={run} />}
              <AgentsPanel view={view} feeds={feeds} focus={focus} onError={onError} />
            </>
          )}
        </section>

        <section className="card side-card">
          <div className="segmented" role="tablist">
            {(["code", "talk", "store", "github"] as const).map((tab) => (
              <button key={tab} role="tab" aria-current={side === tab || undefined} onClick={() => setSide(tab)}>
                {tab === "code" ? "Workflow code" : tab === "talk" ? `Conversation${said ? ` (${said})` : ""}` : tab === "store" ? "Storefront" : "GitHub"}
              </button>
            ))}
          </div>
          {side === "code" && <CodePanel kind={run.kind} activeStep={focus} status={retries.has(focus ?? "") ? "retrying" : (run.steps.find((s) => s.key === focus)?.status ?? "active")} />}
          {side === "talk" && <ConversationPanel view={view} />}
          {side === "store" && <StorefrontPanel view={view} />}
          {side === "github" && <GitHubPanel view={view} repo={config.repo} />}
        </section>
      </div>
    </div>
  );
}

const CHECK_LABEL: Record<string, string> = {
  pending: "Agent check pending", success: "Agent check passed", failure: "Agent wants changes", error: "Agent check errored",
};
// A failed check means the agent wants changes, which is not an error: red stays for errors.
const CHECK_VOCAB: Record<string, string> = { pending: "active", success: "completed", failure: "waiting", error: "failed" };

/** Where the run lives on GitHub, always in view: its issue, its PR, the agent's required check. */
function GitHubLinks({ run, repo }: { run: RunState; repo: string }) {
  const number = (url: string) => url.split("/").pop();
  return (
    <nav className="gh-chips" aria-label="On GitHub">
      {run.kind === "issue" && (run.issue_url
        ? <a className="gh-chip" href={run.issue_url} target="_blank" rel="noreferrer">Issue #{number(run.issue_url)} ↗</a>
        : <span className="gh-chip pending">Taking the issue…</span>)}
      {run.pr_url && <a className="gh-chip" href={run.pr_url} target="_blank" rel="noreferrer">PR #{number(run.pr_url)} ↗</a>}
      {run.check && (
        <a className="gh-chip quiet" data-status={CHECK_VOCAB[run.check]} href={run.pr_url ? `${run.pr_url}/checks` : undefined}
          target="_blank" rel="noreferrer" title={run.check_description}>
          <span className="status-dot" /> {CHECK_LABEL[run.check]}
        </a>
      )}
      {run.revert_pr_url && <a className="gh-chip revert" href={run.revert_pr_url} target="_blank" rel="noreferrer">Revert PR #{number(run.revert_pr_url)} ↗</a>}
      {run.kind === "issue" && run.plan && <a className="gh-chip quiet" href={`https://github.com/${repo}/branches/all?query=${encodeURIComponent(run.run_id)}`} target="_blank" rel="noreferrer">Agent branches ↗</a>}
    </nav>
  );
}

/** Why the upgrade broke, as the coding agent worked it out, while it fixes the code. */
function DiagnosisCard({ run }: { run: RunState }) {
  const d = run.diagnosis!;
  return (
    <div className="diagnosis">
      <span className="eyebrow">The coding agent's diagnosis</span>
      <dl>
        <dt>What changed</dt><dd>{d.what_changed}</dd>
        <dt>What broke</dt><dd>{d.what_broke}</dd>
        <dt>Root cause</dt><dd>{d.root_cause}</dd>
        <dt>Fix</dt><dd>{d.fix_plan}</dd>
      </dl>
    </div>
  );
}

const KIND_LABEL: Record<string, string> = {
  answer: "Answer", plan_feedback: "Plan feedback", feedback: "Feedback", scope_change: "Scope change", steer: "Steering",
  command: "Command", noted: "Noted",
};
const STATUS_LABEL: Record<string, string> = {
  received: "Received", working: "Agent working", addressed: "Addressed", replanning: "Re-planning", queued: "Queued",
};

/** Everything people said to the agents on GitHub, and what the agents did with it. */
function ConversationPanel({ view }: { view: RunView }) {
  const items = [...view.run.conversation].reverse();
  if (items.length === 0) {
    return <p className="empty muted">Comments on the run's issue and PR appear here as GitHub delivers them, with what the agents did about each.</p>;
  }
  return (
    <ol className="conversation">
      {items.map((f) => (
        <li key={f.id} data-status={f.status === "addressed" ? "completed" : f.status === "queued" ? "waiting" : "active"}>
          <header>
            <strong>@{f.author}</strong>
            {f.kind && <span className={`badge ${f.kind === "scope_change" ? "progress" : "idle"}`}>{KIND_LABEL[f.kind]}</span>}
            <span className="status-text mono">{STATUS_LABEL[f.status]}</span>
            {f.url && <a href={f.url} target="_blank" rel="noreferrer" className="mono">↗</a>}
          </header>
          <Markdown text={f.text} />
          {f.reason && <p className="muted">Triage: {f.reason}</p>}
          {f.outcome && <p className="outcome">{f.outcome}</p>}
        </li>
      ))}
    </ol>
  );
}

function toTimelineStep(s: Step, retry: Retry | undefined, now: number) {
  const status = retry ? "retrying" : s.status;
  const started = s.started_at;
  const time = started ? (s.ended_at ? ago(s.ended_at - started) : `${ago(now - started)}\nso far`) : "";
  let note = s.note;
  if (retry) note = `Attempt ${retry.attempt}: ${explainFailure(retry.last_failure)}. Temporal retries it.`;
  return { key: s.key, label: s.lane ? `↳ ${s.label}` : s.label, status, detail: s.detail, note, time, attempt: retry?.attempt };
}

// -- Person in the loop -------------------------------------------------------------------

const WHERE: Record<string, string> = { merge: "PR", conversation: "PR" };

/** What the run is waiting for people to say on GitHub, and where to say it. */
function GateCard({ view, now }: { view: RunView; now: number }) {
  const gate = view.run.gate!;
  const waited = now - gate.waiting_since;
  const number = gate.github_url?.split("/").pop();
  const where = gate.github_url ? `${WHERE[gate.key] ?? "issue"} #${number}` : "GitHub";
  // Production and budget decisions take a command; at other gates any comment is conversation.
  const conversational = gate.key !== "production" && gate.key !== "budget";

  return (
    <div className="gate" data-status="waiting">
      <span className="eyebrow">Waiting on GitHub · {ago(waited)}</span>
      <h3 className="gate-question">{gate.question}</h3>
      {gate.key === "clarify" && <ol className="questions">{gate.questions.map((q) => <li key={q}>{q}</li>)}</ol>}
      {gate.closes_at && gate.key === "merge" && <ReviewWindow closesAt={gate.closes_at} opened={gate.waiting_since} now={now} />}
      <p className="gate-reply">
        {gate.commands.length > 0 ? "Reply " : "Comment "}
        {gate.commands.map((c, i) => <span key={c}>{i > 0 && " or "}<code>{c}</code></span>)}
        {gate.commands.length > 0 && conversational && ", or comment,"}
        {" on "}
        {gate.github_url
          ? <a className="gh-chip" href={gate.github_url} target="_blank" rel="noreferrer">{where} ↗</a>
          : <span className="mono">{where}</span>}
      </p>
      <p className="gate-explain">
        The Workflow is parked on a durable wait. No process is holding it open: it costs nothing, survives
        restarts and deploys, and could wait like this for days. GitHub's webhook delivers each comment to it as a
        Signal; nothing polls.
      </p>
      <div className="gate-details">
        {gate.context && <p className="gate-context">{gate.context}</p>}
        {gate.key === "plan" && view.run.plan && <PlanSummary view={view} />}
        {gate.key === "production" && view.environments.staging_sha && (
          <p><a href={`${view.apps}/staging/`} target="_blank" rel="noreferrer">Open the change on staging</a></p>
        )}
      </div>
    </div>
  );
}

function ReviewWindow({ closesAt, opened, now }: { closesAt: number; opened: number; now: number }) {
  const left = Math.max(0, closesAt - now);
  const share = Math.min(1, (now - opened) / Math.max(1, closesAt - opened));
  return (
    <div className="review-window">
      <span>Agents merge in <strong className="mono">{Math.floor(left / 60)}:{String(Math.floor(left % 60)).padStart(2, "0")}</strong> unless someone comments.</span>
      <span className="meter-bar"><span style={{ width: `${share * 100}%` }} data-status="waiting" /></span>
    </div>
  );
}

function PlanSummary({ view }: { view: RunView }) {
  const plan = view.run.plan!;
  return (
    <div className="plan">
      <ul className="criteria">{plan.acceptance_criteria.map((c) => <li key={c}>{c}</li>)}</ul>
      <div className="subtasks">
        {plan.subtasks.map((s) => (
          <div key={s.id} className="subtask"><strong>{s.title}</strong><span className="muted">{s.files.join(", ")}</span></div>
        ))}
      </div>
    </div>
  );
}

// -- Agents at work -----------------------------------------------------------------------

function AgentsPanel({ view, feeds, focus, onError }: { view: RunView; feeds: Record<string, AgentFeed>; focus: string | null; onError: (m: string) => void }) {
  const agents = view.run.agents;
  if (agents.length === 0) {
    return <p className="empty muted">Agents appear here as the pipeline starts them. Each one is its own Workflow.</p>;
  }
  const focused = focus?.startsWith("code:") ? agents.filter((a) => a.lane === focus.slice(5)) : [];
  const live = agents.filter((a) => (feeds[a.workflow_id]?.thinking || (feeds[a.workflow_id]?.tools ?? []).some((t) => t.status === "running")));
  const ordered = [...new Set([...focused, ...live, ...[...agents].reverse()])].slice(0, 4);
  return (
    <div className="agents">
      <div className="card-head"><span className="eyebrow">Agents at work · {agents.length} started</span></div>
      <div className="agent-grid">
        {ordered.map((a) => (
          <AgentCard key={a.workflow_id} agentId={a.workflow_id} role={a.role} model={a.model} feed={feeds[a.workflow_id]}
            retry={view.retries.find((r) => r.workflow_id === a.workflow_id)} faults={view.faults[a.workflow_id] ?? []}
            closed={view.closed} onError={onError}
            queued={a.lane ? view.run.conversation.filter((f) => f.kind === "steer" && f.status !== "addressed") : []} />
        ))}
      </div>
    </div>
  );
}

const ROLE_NAME: Record<string, string> = { planner: "Planner", "reviewer-quality": "Code reviewer", "reviewer-security": "Security reviewer", "coder-integration": "Integration coder" };

function AgentCard({ agentId, role, model, feed, retry, faults, closed, onError, queued }: {
  agentId: string; role: string; model: string; feed?: AgentFeed; retry?: Retry; faults: string[]; closed: boolean;
  onError: (m: string) => void; queued: Feedback[];
}) {
  const name = ROLE_NAME[role] ?? (role.startsWith("coder-") ? `Coder · ${role.slice(6)}` : role);
  const status = retry ? "retrying" : feed?.thinking || feed?.tools.some((t) => t.status === "running") ? "active" : "completed";
  const injectRateLimit = () => api("/api/faults", { json: { kind: "model_rate_limit", workflow_id: agentId, attempts: 2 } }).catch((e) => onError(e.message));

  return (
    <article className="agent-card" data-status={status}>
      <header>
        <span className="status-dot" />
        <strong>{name}</strong>
        <span className="mono muted">{model.replace(":", " · ")}</span>
        {!closed && <button className="link-button" onClick={injectRateLimit} title="Make this agent's next two model calls fail with a 429">Inject 429</button>}
      </header>
      <div className="agent-stats mono">
        <span>{feed?.modelCalls ?? 0} model calls</span>
        <span>{((feed?.inTokens ?? 0) + (feed?.outTokens ?? 0)).toLocaleString()} tokens</span>
        <span>{feed?.tools.length ?? 0} tool calls</span>
      </div>
      {faults.length > 0 && <div className="injected mono">Injected fault armed: {faults.join(", ").replace(/_/g, " ")}</div>}
      {retry && <div className="agent-retry mono">Attempt {retry.attempt}: {explainFailure(retry.last_failure)}</div>}
      <ol className="tool-log">
        {(feed?.tools ?? []).slice(-6).map((t) => (
          <li key={t.id} data-status={t.status === "done" ? "completed" : t.status === "error" ? "failed" : t.status === "awaiting" ? "waiting" : "active"}>
            <span className="status-dot" /><code>{t.name}</code><span className="muted">{t.input}</span>
          </li>
        ))}
      </ol>
      {feed?.thinking && <p className="reply-tail">{feed.replyTail || "Thinking…"}</p>}
      {queued.map((q) => (
        <p key={q.id} className="queued-steer mono">✉ @{q.author} on GitHub, {q.status === "working" ? "working on it" : "queued for its next turn"}: “{q.text}”</p>
      ))}
    </article>
  );
}

