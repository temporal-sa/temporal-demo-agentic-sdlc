// The live run: the pipeline graph across the top, then the timeline, the agents at work (or
// the person the run is waiting for), and the proof: code, storefront and GitHub.

import { useEffect, useMemo, useState } from "react";
import { ago, api, explainFailure, money, temporalLink, type Config, type Feedback, type Retry, type RunView, type Step } from "../api";
import type { AgentFeed } from "../live";
import { useNow } from "../live";
import { StepList } from "../kit/StepList";
import { CodePanel } from "./CodePanel";
import { GraphLegend, PipelineGraph, hiddenUntilUsed } from "./PipelineGraph";
import { GitHubPanel, StorefrontPanel } from "./Panels";

const RUN_STATUS_LABEL: Record<string, string> = {
  running: "Running", waiting: "Waiting for a person", completed: "Shipped", rolled_back: "Rolled back",
  failed: "Failed", cancelled: "Stopped", stopped: "Stopped before production",
};
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
          <span className="status-badge" data-status={RUN_STATUS_VOCAB[run.status]}>{RUN_STATUS_LABEL[run.status]}</span>
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
            <GateCard view={view} now={now} onError={onError} />
          ) : (
            <AgentsPanel view={view} feeds={feeds} focus={focus} onError={onError} />
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
          {side === "code" && <CodePanel activeStep={focus} status={retries.has(focus ?? "") ? "retrying" : (run.steps.find((s) => s.key === focus)?.status ?? "active")} />}
          {side === "talk" && <ConversationPanel view={view} />}
          {side === "store" && <StorefrontPanel view={view} />}
          {side === "github" && <GitHubPanel view={view} repo={config.repo} />}
        </section>
      </div>
    </div>
  );
}

/** What the run opened on GitHub, always in view: the issue, the PR, and a revert if it rolled back. */
function GitHubLinks({ run, repo }: { run: RunView["run"]; repo: string }) {
  const number = (url: string) => url.split("/").pop();
  return (
    <nav className="gh-chips" aria-label="On GitHub">
      {run.issue_url
        ? <a className="gh-chip" href={run.issue_url} target="_blank" rel="noreferrer">Issue #{number(run.issue_url)} ↗</a>
        : <span className="gh-chip pending">Issue not opened yet</span>}
      {run.pr_url && <a className="gh-chip" href={run.pr_url} target="_blank" rel="noreferrer">PR #{number(run.pr_url)} ↗</a>}
      {run.revert_pr_url && <a className="gh-chip revert" href={run.revert_pr_url} target="_blank" rel="noreferrer">Revert PR #{number(run.revert_pr_url)} ↗</a>}
      {run.plan && <a className="gh-chip quiet" href={`https://github.com/${repo}/branches/all?query=${encodeURIComponent(run.run_id)}`} target="_blank" rel="noreferrer">Agent branches ↗</a>}
    </nav>
  );
}

const KIND_LABEL: Record<string, string> = {
  answer: "Answer", plan_feedback: "Plan feedback", feedback: "Feedback", scope_change: "Scope change", steer: "Steering",
};
const STATUS_LABEL: Record<string, string> = {
  received: "Received", working: "Agent working", addressed: "Addressed", replanning: "Re-planning", queued: "Queued",
};

/** Everything people said to the agents, where they said it, and what the agents did with it. */
function ConversationPanel({ view }: { view: RunView }) {
  const items = [...view.run.conversation].reverse();
  if (items.length === 0) {
    return <p className="empty muted">Answers, comments and messages to agents appear here, from the console or from GitHub.</p>;
  }
  return (
    <ol className="conversation">
      {items.map((f) => (
        <li key={f.id} data-status={f.status === "addressed" ? "completed" : f.status === "queued" ? "waiting" : "active"}>
          <header>
            <span className={`source ${f.source}`}>{f.source === "github" ? "GitHub" : "Console"}</span>
            <strong>{f.source === "github" ? `@${f.author}` : f.author}</strong>
            {f.kind && <span className={`badge ${f.kind === "scope_change" ? "progress" : "idle"}`}>{KIND_LABEL[f.kind]}</span>}
            <span className="status-text mono">{STATUS_LABEL[f.status]}</span>
            {f.url && <a href={f.url} target="_blank" rel="noreferrer" className="mono">↗</a>}
          </header>
          <blockquote>{f.text}</blockquote>
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

function GateCard({ view, now, onError }: { view: RunView; now: number; onError: (m: string) => void }) {
  const gate = view.run.gate!;
  const [text, setText] = useState("");
  const [sending, setSending] = useState<string | null>(null);
  const waited = now - gate.waiting_since;
  useEffect(() => { setSending(null); setText(""); }, [gate.key, gate.waiting_since]);

  const send = async (label: string, path: "decide" | "comment", body: object) => {
    setSending(label);
    try {
      await api(`/api/runs/${view.run.run_id}/${path}`, { json: body });
    } catch (e) {
      onError((e as Error).message);
      setSending(null);
    }
  };
  const decide = (label: string, approve: boolean, feedback: string | null = null) =>
    send(label, "decide", { gate: gate.key, approve, feedback, raise_budget_usd: gate.key === "budget" ? 3 : null });
  const comment = () => send("comment", "comment", { text });
  const reply = gate.github_url && (
    <a className="gh-chip" href={gate.github_url} target="_blank" rel="noreferrer">
      or reply on GitHub {gate.key === "merge" ? `PR #${gate.github_url.split("/").pop()}` : "issue"} ↗
    </a>
  );
  const busy = !!sending;

  return (
    <div className="gate" data-status="waiting">
      <span className="eyebrow">Waiting for a person · {ago(waited)}</span>
      <h3 className="gate-question">{gate.question}</h3>

      {gate.key === "clarify" && (
        <>
          <ol className="questions">{gate.questions.map((q) => <li key={q}>{q}</li>)}</ol>
          <textarea className="gate-text" rows={3} value={text} onChange={(e) => setText(e.target.value)}
            placeholder="Answer in a sentence or two" aria-label="Your answers" />
          <div className="gate-actions">
            <button className="button primary green" disabled={busy || !text.trim()} onClick={() => decide("answer", true, text)}>
              {sending === "answer" ? "Sending…" : "Send answers"}
            </button>
            <button className="button secondary ultraviolet" disabled={busy} onClick={() => decide("skip", true)}>
              {sending === "skip" ? "Sending…" : "Use your judgment"}
            </button>
            {reply}
          </div>
        </>
      )}

      {gate.key === "plan" && (
        <>
          <div className="gate-actions">
            <button className="button primary green" disabled={busy} onClick={() => decide("approve", true)}>
              {sending === "approve" ? "Approving…" : "Approve"}
            </button>
            <button className="button secondary ultraviolet" disabled={busy} onClick={() => decide("reject", false, text || null)}>
              {sending === "reject" ? "Sending…" : "Send back"}
            </button>
            <input className="gate-feedback" aria-label="Feedback for the planner" value={text}
              onChange={(e) => setText(e.target.value)} placeholder="Feedback for the planner" />
            {reply}
          </div>
        </>
      )}

      {gate.key === "merge" && (
        <>
          {gate.closes_at && (
            <ReviewWindow closesAt={gate.closes_at} opened={gate.waiting_since} now={now} />
          )}
          <textarea className="gate-text" rows={2} value={text} onChange={(e) => setText(e.target.value)}
            placeholder="Comment on the change, like a reviewer would" aria-label="Your comment" />
          <div className="gate-actions">
            <button className="button primary green" disabled={busy} onClick={() => decide("approve", true)}>
              {sending === "approve" ? "Merging…" : "Approve and merge"}
            </button>
            <button className="button secondary ultraviolet" disabled={busy || !text.trim()} onClick={comment}
              title="A coding agent addresses feedback; a new requirement sends the run back to planning">
              {sending === "comment" ? "Sending…" : "Comment"}
            </button>
            {reply}
          </div>
        </>
      )}

      {(gate.key === "production" || gate.key === "budget") && (
        <div className="gate-actions">
          <button className="button primary green" disabled={busy} onClick={() => decide("approve", true)}>
            {sending === "approve" ? "Approving…" : gate.key === "budget" ? "Raise budget by $3" : "Ship it"}
          </button>
          <button className="button secondary ultraviolet" disabled={busy} onClick={() => decide("reject", false, text || null)}>
            {sending === "reject" ? "Stopping…" : gate.key === "budget" ? "Stop the run" : "Stop the release"}
          </button>
          {gate.key === "production" && (
            <input className="gate-feedback" aria-label="Why, if you stop it" value={text}
              onChange={(e) => setText(e.target.value)} placeholder="Why, if you stop it" />
          )}
        </div>
      )}

      <p className="gate-explain">
        The Workflow is parked on a durable wait. No process is holding it open: it costs nothing,
        survives restarts and deploys, and could wait like this for days.
        {gate.github_url && " While it waits it reads new comments on GitHub every few seconds."}
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
            closed={view.closed} onError={onError} runId={view.run.run_id}
            lane={a.lane && view.run.steps.some((s) => s.key === `code:${a.lane}` && (s.status === "active" || s.status === "retrying")) ? `code:${a.lane}` : null}
            queued={view.run.conversation.filter((f) => f.kind === "steer" && f.stage === `code:${a.lane}` && f.status !== "addressed")} />
        ))}
      </div>
    </div>
  );
}

const ROLE_NAME: Record<string, string> = { planner: "Planner", "reviewer-quality": "Code reviewer", "reviewer-security": "Security reviewer", "coder-integration": "Integration coder" };

function AgentCard({ agentId, role, model, feed, retry, faults, closed, onError, runId, lane, queued }: {
  agentId: string; role: string; model: string; feed?: AgentFeed; retry?: Retry; faults: string[]; closed: boolean;
  onError: (m: string) => void; runId: string; lane: string | null; queued: Feedback[];
}) {
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const steer = async () => {
    setSending(true);
    try {
      await api(`/api/runs/${runId}/steer`, { json: { lane, text: message } });
      setMessage("");
    } catch (e) { onError((e as Error).message); }
    finally { setSending(false); }
  };
  const name = ROLE_NAME[role] ?? (role.startsWith("coder-") ? `Coder · ${role.slice(6)}` : role);
  const status = retry ? "retrying" : feed?.approvals.length ? "waiting" : feed?.thinking || feed?.tools.some((t) => t.status === "running") ? "active" : "completed";
  const injectRateLimit = () => api("/api/faults", { json: { kind: "model_rate_limit", workflow_id: agentId, attempts: 2 } }).catch((e) => onError(e.message));
  const approve = (toolId: string, approved: boolean) =>
    api(`/api/agents/${encodeURIComponent(agentId)}/approve`, { json: { tool_id: toolId, approved } }).catch((e) => onError(e.message));

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
      {feed?.approvals.map((a) => (
        <div key={a.id} className="approval">
          <span>Wants to run <code>{a.name}</code>: <code>{a.input}</code></span>
          <div>
            <button className="button small primary green" onClick={() => approve(a.id, true)}>Allow</button>
            <button className="button small secondary ultraviolet" onClick={() => approve(a.id, false)}>Deny</button>
          </div>
        </div>
      ))}
      <ol className="tool-log">
        {(feed?.tools ?? []).slice(-6).map((t) => (
          <li key={t.id} data-status={t.status === "done" ? "completed" : t.status === "error" ? "failed" : t.status === "awaiting" ? "waiting" : "active"}>
            <span className="status-dot" /><code>{t.name}</code><span className="muted">{t.input}</span>
          </li>
        ))}
      </ol>
      {feed?.thinking && <p className="reply-tail">{feed.replyTail || "Thinking…"}</p>}
      {queued.map((q) => (
        <p key={q.id} className="queued-steer mono">✉ {q.status === "working" ? "Working on" : "Queued for its next turn"}: “{q.text}”</p>
      ))}
      {lane && (
        <form className="steer" onSubmit={(e) => { e.preventDefault(); if (message.trim()) steer(); }}>
          <input value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Message this agent"
            aria-label={`Message ${name}`} />
          <button className="button small secondary ultraviolet" disabled={sending || !message.trim()}>Send</button>
        </form>
      )}
    </article>
  );
}

