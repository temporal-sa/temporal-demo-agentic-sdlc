// The decision criteria a team needs proven before it trusts agents with its SDLC, ticked
// off from evidence the runs recorded. Nothing ticks unless the system did it.

import { temporalLink, type Criterion, type Proof, type RunSummary } from "../api";
import { CRITERIA } from "../story";

export interface Evidence { proof: Proof; runId: string; workflowId: string; temporalRunId?: string }

/** Every proof across this copy's runs, plus D5, which spans runs: a run that finished on
 *  the old pipeline version after a run on the new version had started. */
export function collectEvidence(runs: (RunSummary & { proofs?: Proof[] })[]): Map<Criterion, Evidence> {
  const found = new Map<Criterion, Evidence>();
  for (const run of [...runs].sort((a, b) => a.started_at - b.started_at)) {
    for (const proof of run.proofs ?? []) {
      if (!found.has(proof.criterion)) found.set(proof.criterion, { proof, runId: run.run_id, workflowId: run.workflow_id, temporalRunId: run.temporal_run_id });
    }
  }
  const v2 = runs.filter((r) => r.version === "v2");
  const firstV2 = v2.length ? Math.min(...v2.map((r) => r.started_at)) : Infinity;
  const pinned = runs.find((r) => r.version === "v1" && r.ended_at && r.ended_at > firstV2 && r.started_at < firstV2);
  if (pinned && !found.has("D5")) {
    found.set("D5", {
      proof: { criterion: "D5", detail: `“${pinned.title}” finished on v1 while new runs used v2`, at: pinned.ended_at! },
      runId: pinned.run_id, workflowId: pinned.workflow_id, temporalRunId: pinned.temporal_run_id,
    });
  }
  return found;
}

export function Scorecard({ evidence, temporalUi, large = false }: { evidence: Map<Criterion, Evidence>; temporalUi: string; large?: boolean }) {
  const done = CRITERIA.filter((c) => evidence.has(c.id)).length;
  return (
    <aside className={`scorecard ${large ? "large" : ""}`} aria-label="Decision criteria">
      <div className="scorecard-head">
        <span className="eyebrow">Decision criteria</span>
        <span className="mono muted">{done} of {CRITERIA.length} shown live</span>
      </div>
      <ol>
        {CRITERIA.map((c) => {
          const e = evidence.get(c.id);
          return (
            <li key={c.id} data-status={e ? "completed" : "pending"}>
              <span className="tick" aria-hidden="true">{e ? "✓" : ""}</span>
              <div>
                <p className="criterion">{c.title}</p>
                {e ? (
                  <p className="evidence">
                    {e.proof.detail}{" "}
                    <a href={temporalLink(temporalUi, e.workflowId, e.temporalRunId)} target="_blank" rel="noreferrer">Evidence</a>
                  </p>
                ) : (
                  large ? <p className="evidence muted">{c.beat}</p> : null
                )}
                {large && <p className="temporal-feature mono">{c.temporal}</p>}
              </div>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}
