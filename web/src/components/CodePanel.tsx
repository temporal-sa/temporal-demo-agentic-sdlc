// The pipeline Workflow's own source, with the line the run is on highlighted. Shows run()
// (the whole lifecycle) and, below it, the method behind the active step.

import { useEffect, useMemo, useRef, useState } from "react";

const CALL_FOR_STEP: Record<string, string> = {
  issue: "_open_issue", plan: "_plan", "gate:plan": "_plan", build: "_build", pr: "_open_pull_request",
  review: "_review", security: "_review", "gate:merge": "_merge", merge: "_merge", staging: "_deploy_to_staging",
  "gate:production": "_gate(\"production\"", canary: "_canary", rollback: "_canary", done: "_finish",
  "gate:budget": "_turn",
};

const METHOD_FOR_STEP: Record<string, string> = {
  issue: "_open_issue", plan: "_plan", "gate:plan": "_gate", build: "_build", pr: "_open_pull_request",
  review: "_review", security: "_review", "gate:merge": "_gate", merge: "_merge", staging: "_deploy_to_staging",
  "gate:production": "_gate", canary: "_canary", rollback: "_roll_back", done: "_finish", "gate:budget": "_ensure_budget",
};

function extract(source: string, name: string): { start: number; lines: string[] } | null {
  const all = source.split("\n");
  const start = all.findIndex((l) => l.match(new RegExp(`^    (async )?def ${name}\\(`)));
  if (start < 0) return null;
  let end = start + 1;
  while (end < all.length && !/^    (async def|def|@)/.test(all[end]) && !/^\S/.test(all[end])) end++;
  while (end > start && all[end - 1].trim() === "") end--;
  return { start: start + 1, lines: all.slice(start, end) };
}

const KEYWORDS = /\b(async|await|def|return|if|else|elif|for|in|while|try|except|finally|raise|with|as|not|and|or|is|None|True|False|class|lambda|from|import|yield|break|continue)\b/g;

function highlight(line: string): string {
  const esc = line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const hash = esc.search(/#(?=(?:[^"]*"[^"]*")*[^"]*$)/);
  const code = hash >= 0 ? esc.slice(0, hash) : esc;
  const comment = hash >= 0 ? `<span class="c">${esc.slice(hash)}</span>` : "";
  return code
    .replace(/(f?"[^"]*"|'[^']*')/g, "\u0000s$1\u0001")
    .replace(KEYWORDS, '<span class="k">$1</span>')
    .replace(/\b(self\.)?(_?[a-z_][a-z0-9_]*)(?=\()/g, '$1<span class="f">$2</span>')
    .replace(/\u0000s(.*?)\u0001/g, '<span class="s">$1</span>') + comment;
}

export function CodePanel({ activeStep, status }: { activeStep: string | null; status: string }) {
  const [source, setSource] = useState<string>("");
  const methodRef = useRef<HTMLPreElement>(null);
  useEffect(() => { fetch("/api/source/pipeline").then((r) => r.text()).then(setSource).catch(() => setSource("")); }, []);

  const run = useMemo(() => (source ? extract(source, "run") : null), [source]);
  const method = useMemo(() => (source && activeStep ? extract(source, METHOD_FOR_STEP[activeStep] ?? "") : null), [source, activeStep]);
  const call = activeStep ? CALL_FOR_STEP[activeStep] : undefined;

  useEffect(() => { methodRef.current?.scrollTo({ top: 0 }); }, [activeStep]);

  if (!run) return <p className="muted">Loading the Workflow source…</p>;
  return (
    <div className="code-panel">
      <div className="code-caption">
        <span className="mono">pipeline/workflow.py</span>
        <span className="muted">The whole lifecycle is one Workflow method</span>
      </div>
      <pre className="codeview compact" data-status={status} style={{ counterReset: `line ${run.start - 1}` }}>
        {run.lines.map((line, i) => (
          <span key={i} className={`ln ${call && line.includes(call) ? "hl" : ""}`} data-ln={run.start + i}
            dangerouslySetInnerHTML={{ __html: highlight(line) || " " }} />
        ))}
      </pre>
      {method && (
        <>
          <div className="code-caption"><span className="mono">{METHOD_FOR_STEP[activeStep ?? ""]}()</span><span className="muted">The step running now</span></div>
          <pre className="codeview method" ref={methodRef} style={{ counterReset: `line ${method.start - 1}` }}>
            {method.lines.map((line, i) => (
              <span key={i} className="ln" data-ln={method.start + i} dangerouslySetInnerHTML={{ __html: highlight(line) || " " }} />
            ))}
          </pre>
        </>
      )}
    </div>
  );
}
