// The run Workflow's own source, with the line the run is on highlighted. Shows run() (the
// whole lifecycle) and, below it, the method behind the active step: from the run's own file, or
// from base.py for what all three kinds of run share.

import { useEffect, useMemo, useRef, useState } from "react";
import type { RunKind } from "../api";

const FILE: Record<RunKind, string> = { issue: "pipeline/workflow.py", dependency: "pipeline/dependency.py", review: "pipeline/review.py" };

// For each step: the call in run() to highlight, and the method to show below it.
const STEPS: Record<RunKind, Record<string, [call: string, method: string]>> = {
  issue: {
    issue: ["_take_issue", "_take_issue"], clarify: ["_clarify", "_clarify"], "gate:clarify": ["_clarify", "_wait_for_people"],
    plan: ["_plan", "_plan"], "gate:plan": ["_plan", "_wait_for_people"], build: ["_build", "_build"],
    pr: ["_open_pull_request", "_open_pull_request"], review: ["_review(", "_review"], security: ["_review(", "_review"],
    "gate:merge": ["_review_window", "_review_window"], feedback: ["_review_window", "_address_feedback"], merge: ["_merge", "_merge"],
    staging: ["_deploy_to_staging", "_deploy_to_staging"], "gate:production": ["_ask_to_ship", "_ask_to_ship"],
    adjust: ["_ask_to_ship", "_adjust_on_staging"],
    canary: ["_canary", "_canary"], rollback: ["_canary", "_roll_back"], done: ["_finish", "_finish"], "gate:budget": ["", "_ensure_budget"],
  },
  dependency: {
    pr: ["_take_pull_request", "_take_pull_request"], tests: ["_test_the_bump", "_test_the_bump"], diagnose: ["_diagnose", "_diagnose"],
    fix: ["_fix", "_fix"], explain: ["_explain", "_explain"], review: ["_review(", "_review"],
    "gate:conversation": ["_converse_on_pr", "_converse_on_pr"], feedback: ["_converse_on_pr", "_address_on_pr"], "gate:budget": ["", "_ensure_budget"],
  },
  review: {
    pr: ["_take_pull_request", "_take_pull_request"], review: ["_review_pr", "_review_pr"],
    "gate:conversation": ["_converse_on_pr", "_converse_on_pr"], feedback: ["_converse_on_pr", "_address_on_pr"], "gate:budget": ["", "_ensure_budget"],
  },
};

function stepCode(kind: RunKind, step: string | null): [string, string] | undefined {
  if (!step) return undefined;
  if (step.startsWith("code:")) return ["_build", "_code_subtask"];  // a coding agent's lane
  return STEPS[kind][step];
}

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

export function CodePanel({ kind, activeStep, status }: { kind: RunKind; activeStep: string | null; status: string }) {
  const [source, setSource] = useState<string>("");
  const [shared, setShared] = useState<string>("");
  const methodRef = useRef<HTMLPreElement>(null);
  useEffect(() => { fetch(`/api/source/${kind}`).then((r) => r.text()).then(setSource).catch(() => setSource("")); }, [kind]);
  useEffect(() => { fetch("/api/source/base").then((r) => r.text()).then(setShared).catch(() => setShared("")); }, []);

  const code = stepCode(kind, activeStep);
  const run = useMemo(() => (source ? extract(source, "run") : null), [source]);
  const method = useMemo(() => {
    if (!code?.[1]) return null;
    const own = source ? extract(source, code[1]) : null;
    return own ? { ...own, file: FILE[kind] } : shared ? { ...extract(shared, code[1]), file: "pipeline/base.py" } : null;
  }, [source, shared, code, kind]);
  const call = code?.[0];

  useEffect(() => { methodRef.current?.scrollTo({ top: 0 }); }, [activeStep]);

  if (!run) return <p className="muted">Loading the Workflow source…</p>;
  return (
    <div className="code-panel">
      <div className="code-caption">
        <span className="mono">{FILE[kind]}</span>
        <span className="muted">The whole lifecycle is one Workflow method</span>
      </div>
      <pre className="codeview compact" data-status={status} style={{ counterReset: `line ${run.start - 1}` }}>
        {run.lines.map((line, i) => (
          <span key={i} className={`ln ${call && line.includes(call) ? "hl" : ""}`} data-ln={run.start + i}
            dangerouslySetInnerHTML={{ __html: highlight(line) || " " }} />
        ))}
      </pre>
      {method?.lines && (
        <>
          <div className="code-caption"><span className="mono">{code?.[1]}()</span><span className="muted">The step running now · {method.file}</span></div>
          <pre className="codeview method" ref={methodRef} style={{ counterReset: `line ${(method.start ?? 1) - 1}` }}>
            {method.lines.map((line, i) => (
              <span key={i} className="ln" data-ln={(method.start ?? 1) + i} dangerouslySetInnerHTML={{ __html: highlight(line) || " " }} />
            ))}
          </pre>
        </>
      )}
    </div>
  );
}
