// The faults a session can inject: crash its Workers, rate limit GitHub, take a model provider
// down, break the canary. Every fault is scoped to this session's slot on the server.

import { useState } from "react";
import { api, type Config, type Overview, type RunView } from "../api";
import { useNow } from "../live";

const PROVIDER_NAME: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI", google: "Google" };

function clock(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function BreakThings({ session, config, view, live, refresh, notify }: {
  session: Overview; config: Config; view: RunView | null; live: boolean | null;
  refresh: () => void; notify: (m: string) => void;
}) {
  const now = useNow();
  const slot = session.slot;
  const [provider, setProvider] = useState<string>(config.settings.models.coder.provider);
  const [pending, setPending] = useState<string | null>(null);
  const providers = Object.keys(config.models);
  const outageUntil = session.outages?.[provider as keyof NonNullable<Overview["outages"]>];
  const canary = session.canary_fault;

  const run = async (name: string, path: string, body?: unknown, done?: (r: any) => string | void) => {
    setPending(name);
    try {
      const result = await api<any>(path, { method: "POST", json: body });
      const message = done?.(result);
      if (message) notify(message);
    } catch (e) {
      notify((e as Error).message);
    } finally {
      setPending(null);
      refresh();
    }
  };

  const restoresIn = slot?.crashed && slot.auto_restore_at ? slot.auto_restore_at - now : null;
  return (
    <div className="bar-group">
      <span className="eyebrow">Break things</span>
      {config.local_controls && (
        <>
          <button className="button small secondary ultraviolet" onClick={() => run("kill", "/api/worker/kill")} disabled={!session.workers.length}>Kill the process</button>
          <button className="button small secondary ultraviolet" onClick={() => run("start", "/api/worker/start")} disabled={session.workers.length > 0}>Start a worker</button>
        </>
      )}
      {slot?.crashed ? (
        <>
          <button className="button small primary ultraviolet" disabled={pending === "restore"} onClick={() => run("restore", "/api/slot/restore")}>Bring it back</button>
          {restoresIn !== null && <span className="muted mono">Restores by itself in {clock(restoresIn)}</span>}
        </>
      ) : (
        <button className="button small secondary ultraviolet" disabled={!slot || pending === "crash"}
          title={slot ? "Stop this session's Workers abruptly, as if their process had died" : "Available once a scenario has started"}
          onClick={() => run("crash", "/api/slot/crash")}>{pending === "crash" ? "Crashing…" : "Crash my worker"}</button>
      )}
      <button className="button small secondary ultraviolet" disabled={!live}
        onClick={() => view && run("429", "/api/faults", { kind: "github_rate_limit", workflow_id: view.workflow_id, attempts: 2 })}>GitHub 429</button>
      <span className="outage-control">
        <select className="run-picker" value={provider} onChange={(e) => setProvider(e.target.value)} aria-label="Model provider" disabled={!slot}>
          {providers.map((p) => <option key={p} value={p}>{PROVIDER_NAME[p] ?? p}</option>)}
        </select>
        <button className="button small secondary ultraviolet" disabled={!slot || pending === "outage"} aria-pressed={Boolean(outageUntil)}
          title="Every model call to this provider fails with a 503 until it is brought back; agents retry with backoff"
          onClick={() => run("outage", "/api/faults/outage", { provider, on: !outageUntil })}>
          {outageUntil ? `Bring ${PROVIDER_NAME[provider] ?? provider} back` : "Take it down"}
        </button>
      </span>
      {canary ? (
        <button className="button small secondary ultraviolet" disabled>Canary broken · {clock(canary.until - now)}</button>
      ) : (
        <button className="button small secondary ultraviolet" disabled={!session.environments.canary_sha || pending === "canary"}
          title={session.environments.canary_sha ? "Make the canary's assets return 503" : "Available while a canary is taking traffic"}
          onClick={() => run("canary", "/api/environments/break-canary", { seconds: 180 },
            () => "The canary's assets now return 503. The pipeline checks the canary every 15 s.")}>Break the canary</button>
      )}
    </div>
  );
}
