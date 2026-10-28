// Start a scenario: do on GitHub what a person would (open an issue, run the dependency bump,
// open a PR), labeled for this copy of the demo. The run starts when GitHub's webhook arrives.
// For the bump, the Action pushes the branch and the PR is opened from a prefilled page.

import { useState } from "react";
import { api, type Autonomy, type Config, type ModelChoice, type RoleModels, type RunKind } from "../api";

const PROVIDER_NAME: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI", google: "Google" };
const ROLES: { key: keyof RoleModels; label: string; hint: string }[] = [
  { key: "planner", label: "Planner", hint: "Plans issues; triages comments" },
  { key: "coder", label: "Coders", hint: "Write code, diagnose broken upgrades" },
  { key: "reviewer", label: "Reviewer", hint: "A second model family checks the first one's work" },
];
const SCENARIOS: { kind: RunKind; name: string; note: string }[] = [
  { kind: "dependency", name: "Dependency bump that breaks the build",
    note: "Runs the repo's bump Action, which pushes a Dependabot-style branch upgrading orbit-money to 2.0 (it has breaking changes). You open the PR; the agent works out why the tests fail, fixes the code on the bump branch and rewrites the PR to explain it." },
  { kind: "review", name: "A PR for the agent to review",
    note: "Opens a PR with a subtle bug. The agent's required check stays pending until it has reviewed it inline. Reply on the PR to ask for a fix." },
  { kind: "issue", name: "An issue, all the way to production",
    note: "Opens an issue labeled agent. The planner asks and plans on the issue; reply /approve, comment to steer, review the PR, then /ship." },
];

interface Started { url: string; kind: RunKind; open_pr: boolean }

export function ScenarioDialog({ config, onClose, onStarted }: {
  config: Config; onClose: () => void; onStarted: (started: Started) => void;
}) {
  const [kind, setKind] = useState<RunKind>("dependency");
  const [issueId, setIssueId] = useState(config.issues[0]?.id ?? "custom");
  const preset = config.issues.find((i) => i.id === issueId);
  const [title, setTitle] = useState(preset?.title ?? "");
  const [body, setBody] = useState(preset?.body ?? "");
  const [models, setModels] = useState<RoleModels>(config.settings.models);
  const [autonomy, setAutonomy] = useState<Autonomy>(config.settings.autonomy);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [ready, setReady] = useState<Started | null>(null);  // a bump branch, waiting for its PR to be opened

  const pick = (id: string) => {
    setIssueId(id);
    const issue = config.issues.find((i) => i.id === id);
    setTitle(issue?.title ?? "");
    setBody(issue?.body ?? "");
  };

  const options = Object.entries(config.models).flatMap(([provider, names]) => names.map((model) => ({ provider, model })));
  const start = async () => {
    setStarting(true);
    setError(null);
    try {
      const issue = kind === "issue" ? { issue_id: issueId === "custom" ? null : issueId, title, body } : {};
      const started = await api<Started>("/api/scenarios", { json: { kind, ...issue, settings: { models, autonomy } } });
      if (started.open_pr) setReady(started); else onStarted(started);
    } catch (e) {
      setError((e as Error).message);
      setStarting(false);
    }
  };

  if (ready) {
    return (
      <div className="dialog-scrim" onClick={() => onStarted(ready)}>
        <div className="dialog card" role="dialog" aria-modal="true" aria-labelledby="open-pr-title" onClick={(e) => e.stopPropagation()}>
          <h2 id="open-pr-title" className="heading">The bump branch is ready. Open the PR.</h2>
          <p className="muted">
            The Dependency bump Action pushed it, Dependabot style. GitHub's "Open a pull request" page has the title,
            description and labels filled in: <strong>Create pull request</strong> there, as a developer would.
            GitHub's webhook then starts the agents, and the run appears here.
          </p>
          <div className="dialog-actions">
            <button className="button secondary ultraviolet" onClick={() => onStarted(ready)}>Close</button>
            <a className="button primary ultraviolet" href={ready.url} target="_blank" rel="noreferrer" onClick={() => onStarted(ready)}>
              Open the pull request on GitHub ↗
            </a>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="dialog-scrim" onClick={onClose}>
      <div className="dialog card" role="dialog" aria-modal="true" aria-labelledby="scenario-title" onClick={(e) => e.stopPropagation()}>
        <h2 id="scenario-title" className="heading">Start a scenario on GitHub</h2>
        <p className="muted">
          This does what a person would do on <span className="mono">{config.repo}</span>. Everything after that happens on GitHub; this console only watches.
        </p>
        <div className="scenario-list" role="radiogroup" aria-label="Scenario">
          {SCENARIOS.map((s) => (
            <button key={s.kind} className="scenario" aria-pressed={kind === s.kind} onClick={() => setKind(s.kind)}>
              <span className="scenario-name">{s.name}</span>
              <span className="scenario-note">{s.note}</span>
            </button>
          ))}
        </div>

        {kind === "issue" && (
          <>
            <div className="chips" role="radiogroup" aria-label="Issue">
              {config.issues.map((issue) => (
                <button key={issue.id} className="chip" aria-pressed={issueId === issue.id} onClick={() => pick(issue.id)}>{issue.title}</button>
              ))}
              <button className="chip" aria-pressed={issueId === "custom"} onClick={() => pick("custom")}>Write my own</button>
            </div>
            <label className="field wide"><span>Issue title</span><input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
            <label className="field wide"><span>Issue body</span><textarea rows={5} value={body} onChange={(e) => setBody(e.target.value)} /></label>
          </>
        )}

        <span className="eyebrow">Settings for your runs</span>
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
        {kind === "issue" && (
          <div className="field">
            <span>Merging the issue's PR</span>
            <div className="segmented">
              <button aria-current={autonomy === "auto" || undefined} onClick={() => setAutonomy("auto")}>Auto merge</button>
              <button aria-current={autonomy === "supervised" || undefined} onClick={() => setAutonomy("supervised")}>Wait for /merge</button>
            </div>
            <small className="muted">{autonomy === "auto"
              ? "After an approving review, agents merge unless someone comments on the PR within a minute."
              : "The PR waits until someone replies /merge on it."}</small>
          </div>
        )}

        {error && <div className="state-banner is-error"><strong>Couldn't start the scenario.</strong><span>{error}</span></div>}
        <div className="dialog-actions">
          <button className="button secondary ultraviolet" onClick={onClose}>Cancel</button>
          <button className="button primary ultraviolet" disabled={starting || (kind === "issue" && !title.trim())} onClick={start}>
            {starting ? (kind === "dependency" ? "Waiting for the bump Action…" : "Starting…") : "Start on GitHub"}
          </button>
        </div>
      </div>
    </div>
  );
}
