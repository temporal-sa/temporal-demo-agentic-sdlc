// The run drawn as a git graph: the pipeline is the main line, each coding agent forks off on
// its own branch and merges back at the pull request. Shapes say who acts: a circle is an agent,
// a diamond is a person, a square is the system. Colours are the buildkit status vocabulary.

import type { RunState, Step } from "../api";

const W = 1200;
const MAIN_Y = 62;
const LANE_GAP = 40;

type Kind = "agent" | "person" | "system";

const KIND: Record<string, Kind> = {
  issue: "system", clarify: "agent", "gate:clarify": "person", plan: "agent", "gate:plan": "person", feedback: "agent", build: "system", pr: "system", review: "agent",
  security: "agent", "gate:merge": "person", merge: "system", staging: "system", "gate:production": "person",
  canary: "system", rollback: "system", "gate:budget": "person", done: "system",
};

const SHORT: Record<string, string> = {
  issue: "Issue", clarify: "Questions", "gate:clarify": "Answer", plan: "Plan", "gate:plan": "Approve plan", feedback: "Fix", build: "Fork", pr: "Pull request",
  review: "Review", security: "Security", "gate:merge": "Comments", merge: "Merge", staging: "Staging",
  "gate:production": "Approve prod", canary: "Canary", rollback: "Roll back", "gate:budget": "Budget", done: "Report",
};

export function PipelineGraph({ run, selected, onSelect, retrying }: {
  run: RunState; selected: string | null; onSelect: (key: string) => void; retrying: Set<string>;
}) {
  const lanes = run.steps.filter((s) => s.lane && s.key.startsWith("code:"));
  const main = run.steps.filter((s) => !s.lane && s.key !== "rollback" && !hiddenUntilUsed(s));
  const rollback = run.steps.find((s) => s.key === "rollback");
  const showRollback = rollback && rollback.status !== "pending" && rollback.status !== "skipped";

  // The parallel build gets three units of width so the agent branches have room.
  const weights: number[] = main.map((s, i) => (i > 0 && s.key === "pr" && lanes.length ? 3 : i > 0 ? 1 : 0));
  const total = weights.reduce((a, b) => a + b, 0) || 1;
  const xs = weights.map((_, i) => 40 + (weights.slice(0, i + 1).reduce((a, b) => a + b, 0) / total) * (W - 80));
  const x = (key: string) => xs[main.findIndex((s) => s.key === key)];
  const height = MAIN_Y + Math.max(lanes.length, showRollback ? 1 : 0) * LANE_GAP + 34;
  const forkX = x("build");
  const mergeX = x("pr");
  const status = (s: Step) => (retrying.has(s.key) ? "retrying" : s.status);

  return (
    <svg className={`pipeline-graph ${main.length > 12 ? "dense" : ""}`} viewBox={`0 0 ${W} ${height}`} role="img"
      aria-label={`Pipeline: ${main.map((s) => `${SHORT[s.key] ?? s.label} ${status(s)}`).join(", ")}`}>
      {/* main line, coloured up to the furthest step that has started */}
      <line x1={40} y1={MAIN_Y} x2={W - 40} y2={MAIN_Y} className="edge" />
      {main.map((s, i) => i > 0 && s.status !== "pending" && s.status !== "skipped" ? (
        <line key={`e-${s.key}`} x1={xs[i - 1]} y1={MAIN_Y} x2={xs[i]} y2={MAIN_Y}
          className="edge done" data-status={status(main[i - 1])} />
      ) : null)}

      {/* agent branches */}
      {lanes.map((lane, i) => {
        const y = MAIN_Y + (i + 1) * LANE_GAP;
        const midX = forkX + 90;
        const st = status(lane);
        const path = `M ${forkX} ${MAIN_Y} C ${forkX + 30} ${MAIN_Y}, ${forkX + 20} ${y}, ${forkX + 60} ${y} L ${mergeX - 60} ${y} C ${mergeX - 20} ${y}, ${mergeX - 30} ${MAIN_Y}, ${mergeX} ${MAIN_Y}`;
        return (
          <g key={lane.key} data-status={st} className="lane" onClick={() => onSelect(lane.key)}>
            <path d={path} className={`edge branch ${lane.status !== "pending" ? "done" : ""}`} />
            <Node kind="agent" cx={midX} cy={y} status={st} selected={selected === lane.key} />
          </g>
        );
      })}

      {/* Labels last, with a halo, so no branch line crosses them. */}
      {lanes.map((lane, i) => (
        <text key={`label-${lane.key}`} x={forkX + 106} y={MAIN_Y + (i + 1) * LANE_GAP + 4} className="lane-label"
          onClick={() => onSelect(lane.key)}>{lane.label.slice(0, 44)}</text>
      ))}

      {showRollback && rollback && (
        <g data-status={status(rollback)} onClick={() => onSelect("rollback")} className="lane">
          <path d={`M ${x("canary")} ${MAIN_Y} C ${x("canary") + 20} ${MAIN_Y}, ${x("canary") + 10} ${MAIN_Y + LANE_GAP}, ${x("canary") + 40} ${MAIN_Y + LANE_GAP}`}
            className="edge branch done" />
          <Node kind="system" cx={x("canary") + 52} cy={MAIN_Y + LANE_GAP} status={status(rollback)} selected={selected === "rollback"} />
          <text x={x("canary") + 68} y={MAIN_Y + LANE_GAP + 4} className="lane-label">Roll back</text>
        </g>
      )}

      {main.map((s) => (
        <g key={s.key} data-status={status(s)} onClick={() => onSelect(s.key)} className="node-group">
          <Node kind={KIND[s.key] ?? "system"} cx={x(s.key)} cy={MAIN_Y} status={status(s)} selected={selected === s.key} />
          <text x={x(s.key)} y={MAIN_Y - 22} textAnchor="middle" className="node-label">{SHORT[s.key] ?? s.label}</text>
        </g>
      ))}
    </svg>
  );
}

/** Steps that only appear when the run actually takes them. */
export function hiddenUntilUsed(s: Step): boolean {
  const unused = s.status === "pending" || s.status === "skipped";
  if (["gate:budget", "feedback", "rollback"].includes(s.key)) return unused;
  return s.key === "gate:clarify" && s.status === "skipped";
}

function Node({ kind, cx, cy, status, selected }: { kind: Kind; cx: number; cy: number; status: string; selected: boolean }) {
  const cls = `node ${status} ${selected ? "selected" : ""}`;
  if (kind === "person") {
    return <rect x={cx - 9} y={cy - 9} width={18} height={18} transform={`rotate(45 ${cx} ${cy})`} className={cls} />;
  }
  if (kind === "system") return <rect x={cx - 8} y={cy - 8} width={16} height={16} className={cls} />;
  return <circle cx={cx} cy={cy} r={10} className={cls} />;
}

export function GraphLegend() {
  return (
    <div className="graph-legend">
      <span><svg width="14" height="14"><circle cx="7" cy="7" r="5.5" className="node completed" /></svg> agent</span>
      <span><svg width="14" height="14"><rect x="3" y="3" width="8" height="8" transform="rotate(45 7 7)" className="node completed" /></svg> person</span>
      <span><svg width="14" height="14"><rect x="2.5" y="2.5" width="9" height="9" className="node completed" /></svg> system</span>
    </div>
  );
}
