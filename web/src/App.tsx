import { useEffect, useMemo, useState } from "react";
import { api, type Config, type Overview } from "./api";
import { usePoll, useRunStream } from "./live";
import { ACTS, type ActKey } from "./story";
import { ChangeAct, HardAct, ProofAct, ResultAct, WhyAct } from "./components/Acts";
import { ScenarioDialog } from "./components/ScenarioDialog";
import { RunConsole } from "./components/RunConsole";
import { Scorecard, collectEvidence } from "./components/Scorecard";
import logo from "./kit/brand/temporal-logo.svg";

const RUN_KIND: Record<string, string> = { issue: "Issue", dependency: "Bump", review: "Review" };

export function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const { data: session, refresh } = usePoll<Overview>("/api/overview", 3000);
  const [act, setAct] = useState<ActKey>("live");
  const [runId, setRunId] = useState<string | null>(() => new URLSearchParams(location.search).get("run"));
  const [dialog, setDialog] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  // After starting a scenario, switch to the next new run once its webhook has started it.
  const [followNext, setFollowNext] = useState<string | null>(null);
  const { view, feeds, freshness } = useRunStream(runId);

  useEffect(() => { api<Config>("/api/config").then(setConfig).catch((e) => setToast(e.message)); }, []);
  useEffect(() => {
    if (!runId && session?.runs.length) setRunId(session.runs[0].run_id);
    if (followNext !== null && session?.runs.length && session.runs[0].run_id !== followNext) {
      setRunId(session.runs[0].run_id);
      setAct("live");
      setFollowNext(null);
    }
  }, [session, runId, followNext]);
  useEffect(() => {
    const url = new URL(location.href);
    if (runId) url.searchParams.set("run", runId); else url.searchParams.delete("run");
    history.replaceState(null, "", url);
  }, [runId]);
  useEffect(() => { if (toast) { const t = setTimeout(() => setToast(null), 6000); return () => clearTimeout(t); } }, [toast]);

  // Keyboard: 1–6 switch acts, N starts a scenario. Keys aimed at form fields are ignored.
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
              <h1 className="display">The agents work on GitHub. This is the view from inside.</h1>
              <p className="lede">
                Open an issue labeled <span className="mono">agent</span>, open a PR, or let a dependency bump land on{" "}
                <span className="mono">{config.repo}</span>. Agents answer there, and every step they take shows up here.
              </p>
              <button className="button primary ultraviolet" onClick={() => setDialog(true)}>Start a scenario</button>
            </div>
          ))}
        </main>
      </div>

      <footer className="presenter-bar">
        <div className="bar-group">
          <button className="button small primary ultraviolet" onClick={() => setDialog(true)} title="Shortcut: N">Start a scenario</button>
          <select className="run-picker" value={runId ?? ""} onChange={(e) => { setRunId(e.target.value || null); setAct("live"); }} aria-label="Run">
            {session.runs.length === 0 && <option value="">No runs yet</option>}
            {session.runs.map((r) => <option key={r.run_id} value={r.run_id}>{RUN_KIND[r.kind]} · {r.title.slice(0, 44)} · {r.status.replace("_", " ")} · {r.version}</option>)}
          </select>
          <button className="button small secondary ultraviolet" disabled={!live} title="Cancel this run; its issue or PR stays on GitHub"
            onClick={() => view && confirm(`Stop “${view.run.title}”?`) && control(`/api/runs/${view.run.run_id}/stop`)}>Stop run</button>
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
            if (confirm(`Stop this copy's runs, empty its environments, and close its issues and PRs labeled ${config.owner_label}?`)) {
              control("/api/reset").then(() => setRunId(null));
            }
          }}>Reset</button>
        </div>
      </footer>

      {dialog && <ScenarioDialog config={config} onClose={() => setDialog(false)} onStarted={(started) => {
        setDialog(false);
        setToast(`Started on GitHub: ${started.url}. The run appears here when GitHub's webhook arrives.`);
        setFollowNext(session.runs[0]?.run_id ?? "");
        api<Config>("/api/config").then(setConfig).catch(() => undefined);
      }} />}
      {toast && <div className="toast-stack"><div className="toast" role="status">{toast}</div></div>}
    </div>
  );
}
