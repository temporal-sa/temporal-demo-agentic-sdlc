import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Config, type Overview } from "./api";
import { usePoll, useRunStream } from "./live";
import { ACTS, type ActKey } from "./story";
import { ChangeAct, HardAct, ProofAct, ResultAct, WhyAct } from "./components/Acts";
import { ScenarioDialog } from "./components/ScenarioDialog";
import { RunConsole } from "./components/RunConsole";
import { BreakThings } from "./components/BreakThings";
import { Scorecard, collectEvidence } from "./components/Scorecard";
import logo from "./kit/brand/temporal-logo.svg";

const RUN_KIND: Record<string, string> = { issue: "Issue", dependency: "Bump", review: "Review" };

export function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const { data: session, refresh } = usePoll<Overview>("/api/overview", 3000);
  const [act, setAct] = useState<ActKey>("live");
  const [runId, setRunId] = useState<string | null>(() => {
    // A stale link (an old run ID format) would 400 on every stream reconnect.
    const fromUrl = new URLSearchParams(location.search).get("run");
    return fromUrl && /^(issue|pr|review)-\d{1,7}$/.test(fromUrl) ? fromUrl : null;
  });
  // After Reset, stay idle instead of jumping back to the run it just stopped.
  const [idleAfterReset, setIdleAfterReset] = useState(false);
  const [dialog, setDialog] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  // After starting a scenario, switch to the next new run once its webhook has started it.
  const [followNext, setFollowNext] = useState<string | null>(null);
  // Only a browser that holds a slot has runs; without one the stream would 404 forever.
  const hasSlot = Boolean(session?.slot);
  const { view, feeds, freshness } = useRunStream(hasSlot ? runId : null);
  const hadSlot = useRef(false);
  const resetting = useRef(false);

  useEffect(() => { api<Config>("/api/config").then(setConfig).catch((e) => setToast(e.message)); }, []);
  useEffect(() => {
    if (!runId && session?.runs.length) {
      const open = session.runs.find((r) => !r.closed);
      if (open) { setRunId(open.run_id); setIdleAfterReset(false); }
      else if (!idleAfterReset) setRunId(session.runs[0].run_id);
    }
    if (followNext !== null && session?.runs.length && session.runs[0].run_id !== followNext) {
      setRunId(session.runs[0].run_id);
      setAct("live");
      setFollowNext(null);
    }
  }, [session, runId, followNext, idleAfterReset]);
  useEffect(() => {
    if (!session) return;
    if (session.slot) { hadSlot.current = true; return; }
    if (hadSlot.current) {
      // The lease ended without this browser pressing Reset (it expired, or was released in another tab).
      hadSlot.current = false;
      if (!resetting.current) setToast("Your session has ended. Start a scenario to begin a new one.");
      resetting.current = false;
    }
    setRunId(null);
  }, [session]);
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
          {session.slot && (
            <span className="workers mono" data-status={session.workers.length ? "completed" : "waiting"}>
              <span className="status-dot" />
              {session.workers.length ? session.workers.map((w) => w.version).join(" + ") + " workers" : "no workers"}
            </span>
          )}
        </div>
      </header>

      <div className={`body ${act === "proof" ? "no-rail" : ""}`}>
        {act !== "proof" && <Scorecard evidence={evidence} temporalUi={config.temporal_ui} />}
        <main className="stage">
          {act === "result" && <ResultAct session={session} config={config} onOpenRun={(id) => { setRunId(id); setAct("live"); }} onStart={() => setDialog(true)} />}
          {act === "why" && <WhyAct />}
          {act === "hard" && <HardAct />}
          {act === "change" && <ChangeAct session={session} config={config} onError={fail} />}
          {act === "proof" && <ProofAct evidence={evidence} config={config} />}
          {act === "live" && (view ? <RunConsole view={view} feeds={feeds} config={config} onError={fail} /> : (
            <div className="empty-stage">
              <h1 className="display">The agents work on GitHub. This is the view from inside.</h1>
              <p className="lede">
                Agents answer on <span className="mono">{config.repo}</span>, and every step they take shows up here.
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
        <BreakThings session={session} config={config} view={view} live={live} refresh={refresh} notify={fail} />
        <div className="bar-group">
          <button className="button small secondary ultraviolet" disabled={!session.slot} onClick={() => {
            if (confirm("Stop this session's runs and close its issues and PRs?")) {
              resetting.current = true;
              control("/api/reset").then(() => { setIdleAfterReset(true); setRunId(null); });
            }
          }}>Reset</button>
        </div>
      </footer>

      {dialog && <ScenarioDialog config={config} onClose={() => setDialog(false)} onStarted={(started) => {
        setDialog(false);
        if (!started.open_pr) setToast(`Started on GitHub: ${started.url}. The run appears here when GitHub's webhook arrives.`);
        setFollowNext(session.runs[0]?.run_id ?? "");
        refresh();
        api<Config>("/api/config").then(setConfig).catch(() => undefined);
      }} />}
      {toast && <div className="toast-stack"><div className="toast" role="status">{toast}</div></div>}
    </div>
  );
}
