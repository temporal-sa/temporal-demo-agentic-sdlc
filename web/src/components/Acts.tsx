// The story acts around the live run. Each one makes a single point the presenter can
// leave on screen while talking, and each problem it names is one the live run resolves.

import { ago, api, money, temporalLink, type Config, type RunSummary, type SessionView } from "../api";
import { useState } from "react";
import { usePoll } from "../live";
import { CRITERIA, LIFECYCLE, RECAP, STATS } from "../story";
import { Scorecard, type Evidence } from "./Scorecard";
import { StorefrontPanel } from "./Panels";
import type { Criterion } from "../api";

export function ResultAct({ session, config, onOpenRun }: { session: SessionView; config: Config; onOpenRun: (id: string) => void }) {
  const shipped = session.runs.find((r) => r.status === "completed");
  return (
    <div className="act act-result">
      <section className="act-copy">
        <h1 className="display">This change was planned, coded, reviewed and shipped by AI agents.</h1>
        <p className="lede">Temporal ran the process around them: the approvals, the retries, the canary, and the record of every step.</p>
        {shipped ? <RunCard run={shipped} config={config} onOpen={() => onOpenRun(shipped.run_id)} /> : (
          <div className="state-banner">
            <strong>No run has shipped in this session yet.</strong>
            <span>Start one before the talk (prepared issue, auto merge) so the audience sees the result first.</span>
          </div>
        )}
      </section>
      <section className="card act-store">
        <StorefrontPanel view={session} initial="prod" large />
      </section>
    </div>
  );
}

function RunCard({ run, config, onOpen }: { run: RunSummary & { issue_url?: string; pr_url?: string }; config: Config; onOpen: () => void }) {
  const duration = run.ended_at ? ago(run.ended_at - run.started_at) : "";
  return (
    <div className="card run-card">
      <strong>{run.title}</strong>
      <div className="stats">
        <div className="stat"><span className="stat-label">Issue to production</span><span className="stat-value">{duration}</span></div>
        <div className="stat"><span className="stat-label">Model calls</span><span className="stat-value">{run.usage.requests}</span></div>
        <div className="stat"><span className="stat-label">Est. model cost</span><span className="stat-value">{money(run.usage.cost_usd)}</span></div>
      </div>
      <div className="run-card-links">
        <button className="button small primary ultraviolet" onClick={onOpen}>Replay the run</button>
        {run.issue_url && <a href={run.issue_url} target="_blank" rel="noreferrer">Issue</a>}
        {run.pr_url && <a href={run.pr_url} target="_blank" rel="noreferrer">Pull request</a>}
        <a href={temporalLink(config.temporal_ui, run.workflow_id, run.temporal_run_id)} target="_blank" rel="noreferrer">Event History</a>
      </div>
    </div>
  );
}

export function WhyAct() {
  return (
    <div className="act act-why">
      <h1 className="display">Coding got faster. Shipping didn't.</h1>
      <p className="lede">Agentic SDLC means agents working across the whole lifecycle, with people accountable for the outcome.</p>
      <ol className="lifecycle" aria-label="The software lifecycle">
        {LIFECYCLE.map((stage, i) => (
          <li key={stage} data-status={i === 1 ? "completed" : "pending"}>
            <span className="lifecycle-stage">{stage}</span>
            <span className="muted">{i === 1 ? "Where most AI help is today" : "Still mostly manual"}</span>
          </li>
        ))}
      </ol>
      <div className="stat-grid">
        {STATS.map((s) => (
          <figure key={s.value} className="card stat-card">
            <span className="stat-value">{s.value}</span>
            <blockquote>{s.claim}</blockquote>
            <figcaption><a href={s.source.url} target="_blank" rel="noreferrer">{s.source.label}</a></figcaption>
          </figure>
        ))}
      </div>
    </div>
  );
}

export function HardAct() {
  return (
    <div className="act act-hard">
      <h1 className="display">Agents across the lifecycle fail in the ways distributed systems fail.</h1>
      <p className="lede">They crash, wait on people, run in parallel, hit rate limits, ship bad changes, change underneath you, and need steering. These are the seven things to prove before you trust them.</p>
      <div className="problem-grid">
        {CRITERIA.map((c) => (
          <article key={c.id} className="card problem">
            <span className="mono criterion-id">{c.id}</span>
            <h3 className="heading">{c.title}</h3>
            <p>{c.problem}</p>
            {c.quote && (
              <blockquote>
                “{c.quote.text}” <a href={c.quote.source.url} target="_blank" rel="noreferrer">{c.quote.source.label}</a>
              </blockquote>
            )}
            <p className="beat"><span className="eyebrow">In the live run</span> {c.beat}</p>
          </article>
        ))}
      </div>
    </div>
  );
}

interface Versions { deployment: string; current: string | null; versions: { build_id: string; current: boolean; drainage: string }[] }

export function ChangeAct({ session, onError }: { session: SessionView; config: Config; onError: (m: string) => void }) {
  const { data: versions, refresh } = usePoll<Versions>("/api/versions", 3000);
  const [busy, setBusy] = useState<string | null>(null);
  const current = versions?.current ?? null;
  const rollOut = async (buildId: "v1" | "v2") => {
    if (!confirm(`Make ${buildId} current for this deployment? New runs from everyone using it will start on ${buildId}.`)) return;
    setBusy(buildId);
    try { await api("/api/versions/current", { json: { build_id: buildId } }); refresh(); }
    catch (e) { onError((e as Error).message); }
    finally { setBusy(null); }
  };
  const live = session.runs.filter((r) => !r.closed);
  return (
    <div className="act act-change">
      <h1 className="display">Change the pipeline while agents are mid-run.</h1>
      <p className="lede">
        v2 adds a security-review agent. Both versions' workers are running. Rolling out v2 makes it the current
        version: new runs start on it, and runs already in flight stay pinned to the version they started on.
        Nothing is patched in place.
      </p>
      <div className="change-grid">
        <section className="card">
          <span className="eyebrow">New runs start on</span>
          <div className="current-version">
            <span className="stat-value">{current ?? "…"}</span>
            <span className="muted">Worker Deployment <span className="mono">{versions?.deployment ?? ""}</span></span>
          </div>
          <ul className="versions">
            {(versions?.versions ?? []).map((v) => (
              <li key={v.build_id}>
                <span className={`badge ${v.current ? "good" : "idle"}`}>pipeline {v.build_id}</span>{" "}
                <span className="mono muted">
                  {v.current ? "current" : v.drainage === "draining" ? "draining: pinned runs still finishing" : v.drainage || "registered"}
                  {" · "}{session.workers.filter((w) => w.version === v.build_id).length || "no"} worker(s) live
                </span>
              </li>
            ))}
          </ul>
          <div className="gate-actions">
            <button className="button primary ultraviolet" onClick={() => rollOut("v2")} disabled={!!busy || current === "v2"}>
              {busy === "v2" ? "Rolling out…" : "Roll out v2"}
            </button>
            <button className="button secondary ultraviolet" onClick={() => rollOut("v1")} disabled={!!busy || current === "v1"}>
              {busy === "v1" ? "Switching…" : "Make v1 current again"}
            </button>
          </div>
          {live.length > 0 && current && (
            <p className="muted">{live.filter((r) => r.version && r.version !== current).length} of your {live.length} in-flight run(s) are pinned to an older version and will finish on it.</p>
          )}
        </section>
        <section className="card">
          <span className="eyebrow">Runs and the version they're pinned to</span>
          <table className="data-table">
            <thead><tr><th>Run</th><th>Version</th><th>Status</th></tr></thead>
            <tbody>
              {session.runs.slice(0, 8).map((r) => (
                <tr key={r.run_id}><td>{r.title}</td><td className="mono">{r.version}</td><td>{r.status.replace("_", " ")}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  );
}

export function ProofAct({ evidence, config }: { evidence: Map<Criterion, Evidence>; config: Config }) {
  return (
    <div className="act act-proof">
      <div className="proof-copy">
        <h1 className="display">What you saw, and where to check it.</h1>
        <ol className="recap">{RECAP.map((line) => <li key={line}>{line}</li>)}</ol>
        <p className="lede">Each tick links to the Workflow whose Event History shows it.</p>
      </div>
      <Scorecard evidence={evidence} temporalUi={config.temporal_ui} large />
    </div>
  );
}
