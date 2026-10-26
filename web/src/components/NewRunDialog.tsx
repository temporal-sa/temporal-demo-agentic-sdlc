// Start a run: pick a prepared issue or write one, choose each agent's model and the autonomy.

import { useState } from "react";
import { api, randomId, type Config, type ModelChoice, type RoleModels } from "../api";

const PROVIDER_NAME: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI", google: "Google" };
const ROLES: { key: keyof RoleModels; label: string; hint: string }[] = [
  { key: "planner", label: "Planner", hint: "Turns the issue into a plan of parallel subtasks" },
  { key: "coder", label: "Coders", hint: "One per subtask, each on its own branch" },
  { key: "reviewer", label: "Reviewer", hint: "A second model family checks the first one's work" },
];

export function NewRunDialog({ config, onClose, onStarted }: {
  config: Config; onClose: () => void; onStarted: (runId: string) => void;
}) {
  const [issueId, setIssueId] = useState(config.issues[0]?.id ?? "custom");
  const preset = config.issues.find((i) => i.id === issueId);
  const [title, setTitle] = useState(preset?.title ?? "");
  const [body, setBody] = useState(preset?.body ?? "");
  const [models, setModels] = useState<RoleModels>(config.defaults);
  const [autonomy, setAutonomy] = useState<"auto" | "supervised">("auto");
  const [budget, setBudget] = useState(config.budget_usd);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const pick = (id: string) => {
    setIssueId(id);
    const issue = config.issues.find((i) => i.id === id);
    setTitle(issue?.title ?? "");
    setBody(issue?.body ?? "");
  };

  const options = Object.entries(config.models).flatMap(([provider, names]) => names.map((model) => ({ provider, model })));
  const start = async () => {
    const runId = randomId();
    setStarting(true);
    setError(null);
    try {
      await api("/api/runs", { json: { run_id: runId, issue_title: title, issue_body: body, models, autonomy, budget_usd: budget } });
      onStarted(runId);
    } catch (e) {
      setError((e as Error).message);
      setStarting(false);
    }
  };

  return (
    <div className="dialog-scrim" onClick={onClose}>
      <div className="dialog card" role="dialog" aria-modal="true" aria-labelledby="new-run-title" onClick={(e) => e.stopPropagation()}>
        <h2 id="new-run-title" className="heading">Start a run from a GitHub issue</h2>
        <div className="chips" role="radiogroup" aria-label="Issue">
          {config.issues.map((issue) => (
            <button key={issue.id} className="chip" aria-pressed={issueId === issue.id} onClick={() => pick(issue.id)}>{issue.title}</button>
          ))}
          <button className="chip" aria-pressed={issueId === "custom"} onClick={() => pick("custom")}>Write my own</button>
        </div>
        <label className="field wide"><span>Issue title</span><input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <label className="field wide"><span>Issue body</span><textarea rows={6} value={body} onChange={(e) => setBody(e.target.value)} /></label>

        <div className="role-models">
          {ROLES.map((role) => (
            <label key={role.key} className="field">
              <span>{role.label}</span>
              <select value={`${models[role.key].provider}:${models[role.key].model}`}
                onChange={(e) => {
                  const [provider, ...rest] = e.target.value.split(":");
                  setModels({ ...models, [role.key]: { provider, model: rest.join(":") } as ModelChoice });
                }}>
                {options.map((o) => (
                  <option key={`${o.provider}:${o.model}`} value={`${o.provider}:${o.model}`}>{PROVIDER_NAME[o.provider] ?? o.provider} · {o.model}</option>
                ))}
              </select>
              <small className="muted">{role.hint}</small>
            </label>
          ))}
        </div>

        <div className="role-models">
          <div className="field">
            <span>Autonomy</span>
            <div className="segmented">
              <button aria-current={autonomy === "auto" || undefined} onClick={() => setAutonomy("auto")}>Auto merge</button>
              <button aria-current={autonomy === "supervised" || undefined} onClick={() => setAutonomy("supervised")}>Supervised</button>
            </div>
            <small className="muted">{autonomy === "auto"
              ? "After an approving review, agents merge unless someone comments within a minute. You still approve the plan and production."
              : "The PR waits for your approval, and you approve every shell command an agent wants to run."}</small>
          </div>
          <label className="field">
            <span>Budget for this run (USD)</span>
            <input type="number" min={0.5} max={25} step={0.5} value={budget} onChange={(e) => setBudget(Number(e.target.value))} />
            <small className="muted">The run pauses for a person when estimated model spend reaches this.</small>
          </label>
        </div>

        {error && <div className="state-banner is-error"><strong>Couldn't start the run.</strong><span>{error}</span></div>}
        <div className="dialog-actions">
          <button className="button secondary ultraviolet" onClick={onClose}>Cancel</button>
          <button className="button primary ultraviolet" disabled={starting || !title.trim()} onClick={start}>{starting ? "Starting…" : "Start the run"}</button>
        </div>
      </div>
    </div>
  );
}
