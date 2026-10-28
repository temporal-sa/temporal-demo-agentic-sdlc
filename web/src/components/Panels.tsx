// Proof panels: the storefront each environment is serving, and what the run did on GitHub.

import { useState } from "react";
import { shortSha, type Environments, type RunView } from "../api";

type Env = "prod" | "staging" | "canary";

export function StorefrontPanel({ view, initial = "prod", large = false }: { view: Pick<RunView, "environments"> & { apps: string | null; canary_fault?: RunView["canary_fault"] }; initial?: Env; large?: boolean }) {
  const [env, setEnv] = useState<Env>(initial);
  const e = view.environments;
  const sha = env === "staging" ? e.staging_sha : env === "canary" ? e.canary_sha : e.stable_sha;
  const faulty = Boolean(view.canary_fault);
  if (!view.apps) return <p className="empty muted">The storefront appears once a scenario has started.</p>;
  return (
    <div className={`storefront ${large ? "large" : ""}`}>
      <div className="storefront-bar">
        <div className="segmented">
          {(["prod", "staging", "canary"] as const).map((name) => (
            <button key={name} aria-current={env === name || undefined} onClick={() => setEnv(name)}>
              {name === "prod" ? "Production" : name === "staging" ? "Staging" : `Canary${e.canary_sha ? ` ${e.canary_weight}%` : ""}`}
            </button>
          ))}
        </div>
        <span className="mono muted">{sha ? shortSha(sha) : env === "prod" ? "baseline" : "nothing deployed"}</span>
        <a className="mono" href={`${view.apps}/${env}/`} target="_blank" rel="noreferrer">Open</a>
      </div>
      {env === "canary" && e.canary_sha && <CanaryStats env={e} faulty={faulty} />}
      <iframe key={`${env}-${sha}`} title={`${env} storefront`} src={`${view.apps}/${env}/`} className="storefront-frame" />
    </div>
  );
}

function CanaryStats({ env, faulty }: { env: Environments; faulty: boolean }) {
  return (
    <div className="canary-stats mono">
      <span>{env.canary_traffic.requests} browser page loads</span>
      <span>{env.canary_traffic.errors} browser errors</span>
      {faulty && <span className="injected">Injected fault: canary assets return 503</span>}
    </div>
  );
}

export function GitHubPanel({ view, repo }: { view: RunView; repo: string }) {
  const { run } = view;
  return (
    <div className="github">
      <div className="gh-links">
        {run.issue_url ? <a href={run.issue_url} target="_blank" rel="noreferrer">Issue #{run.issue_url.split("/").pop()}</a> : <span className="muted">Issue not opened yet</span>}
        {run.pr_url && <a href={run.pr_url} target="_blank" rel="noreferrer">Pull request #{run.pr_url.split("/").pop()}</a>}
        {run.revert_pr_url && <a href={run.revert_pr_url} target="_blank" rel="noreferrer">Revert PR #{run.revert_pr_url.split("/").pop()}</a>}
        <a href={`https://github.com/${repo}/branches/all?query=${encodeURIComponent(run.run_id)}`} target="_blank" rel="noreferrer">Agent branches</a>
      </div>
      {run.plan && (
        <section>
          <span className="eyebrow">Plan</span>
          <p>{run.plan.summary}</p>
          <ul className="criteria">{run.plan.acceptance_criteria.map((c) => <li key={c}>{c}</li>)}</ul>
        </section>
      )}
      {run.review && (
        <section>
          <span className="eyebrow">Review · {run.review.verdict === "approve" ? "approved" : "changes requested"}</span>
          <p>{run.review.summary}</p>
          <ul className="criteria">{run.review.comments.map((c, i) => <li key={i}><code>{c.file}</code> {c.comment}</li>)}</ul>
        </section>
      )}
      <section>
        <span className="eyebrow">Deployments</span>
        <ul className="criteria">
          {view.environments.history.slice().reverse().slice(0, 6).map((d, i) => (
            <li key={i}><span className="mono">{d.action}</span> {shortSha(d.sha)} <span className="muted">· {new Date(d.at * 1000).toLocaleTimeString()}</span></li>
          ))}
        </ul>
      </section>
    </div>
  );
}
