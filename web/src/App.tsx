import { useEffect, useMemo, useState } from "react";
import { api, type Config, type SessionView } from "./api";
import { usePoll, useRunStream } from "./live";
import { ACTS, type ActKey } from "./story";
import { ChangeAct, HardAct, ProofAct, ResultAct, WhyAct } from "./components/Acts";
import { NewRunDialog } from "./components/NewRunDialog";
import { RunConsole } from "./components/RunConsole";
import { Scorecard, collectEvidence } from "./components/Scorecard";
import logo from "./kit/brand/temporal-logo.svg";

export function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const { data: session, refresh } = usePoll<SessionView>("/api/session", 3000);
  const [act, setAct] = useState<ActKey>("live");
  const [runId, setRunId] = useState<string | null>(() => new URLSearchParams(location.search).get("run"));
  const [dialog, setDialog] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const { view, feeds, freshness } = useRunStream(runId);

  useEffect(() => { api<Config>("/api/config").then(setConfig).catch((e) => setToast(e.message)); }, []);
  useEffect(() => {
    if (!runId && session?.runs.length) setRunId(session.runs[0].run_id);
  }, [session, runId]);
  useEffect(() => {
    const url = new URL(location.href);
    if (runId) url.searchParams.set("run", runId); else url.searchParams.delete("run");
    history.replaceState(null, "", url);
  }, [runId]);
  useEffect(() => { if (toast) { const t = setTimeout(() => setToast(null), 6000); return () => clearTimeout(t); } }, [toast]);

  // Keyboard: 1–6 switch acts, N starts a run. Keys aimed at form fields are ignored.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable || e.metaKey || e.ctrlKey) return;
      const n = Number(e.key);
      if (n >= 1 && n <= ACTS.length) setAct(ACTS[n - 1].key);
      if (e.key === "n") setDialog(true);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const evidence = useMemo(() => collectEvidence(session?.runs ?? []), [session]);
  const fail = (message: string) => setToast(message);
  const control = (path: string, body?: unknown) =>
    api(path, { method: "POST", json: body }).then(refresh).catch((e: Error) => fail(e.message));

  if (!config || !session) {
    return <div className="loading"><img src={logo} alt="Temporal" /><p className="muted">Connecting…</p></div>;
  }

  const live = view && !view.closed;
  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <img src={logo} alt="Temporal" />
          <span className="brand-divider" />
          <span className="heading">Agentic SDLC</span>
        </div>
        <nav className="segmented acts" aria-label="Story">
          {ACTS.map((a, i) => (
            <button key={a.key} aria-current={act === a.key || undefined} onClick={() => setAct(a.key)}>
              <span className="mono muted">{i + 1}</span> {a.label}
            </button>
          ))}
        </nav>
        <div className="topbar-actions">
          <span className="freshness" data-state={runId ? freshness : "idle"}><span className="dot" />{runId ? (freshness === "live" ? "Live" : freshness === "stale" ? "Reconnecting" : "Connecting") : "Idle"}</span>
          <span className="workers mono" data-status={session.workers.length ? "completed" : "waiting"}>
            <span className="status-dot" />
            {session.workers.length ? session.workers.map((w) => w.version).join(" + ") + " workers" : "no workers"}
          </span>
        </div>
      </header>

      <div className={`body ${act === "proof" ? "no-rail" : ""}`}>
        {act !== "proof" && <Scorecard evidence={evidence} temporalUi={config.temporal_ui} />}
        <main className="stage">
          {act === "result" && <ResultAct session={session} config={config} onOpenRun={(id) => { setRunId(id); setAct("live"); }} />}
          {act === "why" && <WhyAct />}
          {act === "hard" && <HardAct />}
          {act === "change" && <ChangeAct session={session} config={config} onError={fail} />}
          {act === "proof" && <ProofAct evidence={evidence} config={config} />}
          {act === "live" && (view ? <RunConsole view={view} feeds={feeds} config={config} onError={fail} /> : (
            <div className="empty-stage">
              <h1 className="display">Give the agents an issue.</h1>
              <p className="lede">Pick a prepared issue or write one. You'll approve the plan and the release; agents do the rest.</p>
              <button className="button primary ultraviolet" onClick={() => setDialog(true)}>Start a run</button>
            </div>
          ))}
        </main>
      </div>

      <footer className="presenter-bar">
        <div className="bar-group">
          <button className="button small primary ultraviolet" onClick={() => setDialog(true)} title="Shortcut: N">New run</button>
          <select className="run-picker" value={runId ?? ""} onChange={(e) => { setRunId(e.target.value || null); setAct("live"); }} aria-label="Run">
            {session.runs.length === 0 && <option value="">No runs yet</option>}
            {session.runs.map((r) => <option key={r.run_id} value={r.run_id}>{r.title.slice(0, 48)} · {r.status.replace("_", " ")} · {r.version}</option>)}
          </select>
        </div>
        <div className="bar-group">
          <span className="eyebrow">Break things</span>
          {config.local_controls ? (
            <>
              <button className="button small secondary ultraviolet" onClick={() => control("/api/worker/kill")} disabled={!session.workers.length}>Kill the worker</button>
              <button className="button small secondary ultraviolet" onClick={() => control("/api/worker/start")} disabled={session.workers.length > 0}>Start a worker</button>
            </>
          ) : <span className="muted">Crash the worker from the catalog workspace</span>}
          <button className="button small secondary ultraviolet" disabled={!live}
            onClick={() => view && control("/api/faults", { kind: "github_rate_limit", workflow_id: view.workflow_id, attempts: 2 })}>GitHub 429</button>
          <button className="button small secondary ultraviolet" disabled={!session.environments.canary_sha}
            onClick={() => control("/api/environments/break-canary", { seconds: 180 })}>Break the canary</button>
        </div>
        <div className="bar-group">
          <button className="button small secondary ultraviolet" onClick={() => {
            if (confirm("Stop this session's runs, empty its environments and clean up its GitHub branches?")) {
              control("/api/reset").then(() => setRunId(null));
            }
          }}>Reset session</button>
        </div>
      </footer>

      {dialog && <NewRunDialog config={config} onClose={() => setDialog(false)} onStarted={(id) => { setDialog(false); setRunId(id); setAct("live"); refresh(); }} />}
      {toast && <div className="toast-stack"><div className="toast" role="status">{toast}</div></div>}
    </div>
  );
}
