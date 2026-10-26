// Types mirror agentic_sdlc/models.py and the web server's view shapes.

export type StepStatus = "pending" | "active" | "waiting" | "retrying" | "completed" | "failed" | "skipped";

export interface ModelChoice { provider: "anthropic" | "openai" | "google"; model: string }
export interface RoleModels { planner: ModelChoice; coder: ModelChoice; reviewer: ModelChoice }
export interface Usage { model: string; requests: number; input_tokens: number; output_tokens: number; cost_usd: number }

export interface Step {
  key: string; label: string; status: StepStatus; detail: string; note: string; attempt: number;
  started_at: number | null; ended_at: number | null; lane: string | null; agent_workflow_id: string | null;
}
export interface Gate {
  key: "clarify" | "plan" | "merge" | "production" | "budget" | "conversation"; question: string; context: string; waiting_since: number;
  questions: string[]; commands: string[]; closes_at: number | null; github_url: string | null;
}
export interface Feedback {
  id: string; source: "github"; author: string; text: string; url: string | null; at: number; stage: string;
  kind: "answer" | "plan_feedback" | "feedback" | "scope_change" | "steer" | "command" | "noted" | null; reason: string;
  status: "received" | "working" | "addressed" | "replanning" | "queued"; outcome: string;
}
export interface AgentRef { role: string; workflow_id: string; model: string; lane: string | null }
export interface Proof { criterion: Criterion; detail: string; at: number }
export interface Subtask { id: string; title: string; description: string; files: string[] }
export interface Plan { summary: string; acceptance_criteria: string[]; subtasks: Subtask[] }
export interface Review { verdict: "approve" | "request_changes"; summary: string; comments: { file: string; line?: number | null; comment: string }[] }
export interface Diagnosis { what_changed: string; what_broke: string; root_cause: string; fix_plan: string }

/** What started the run on GitHub: a labeled issue, a dependency-bump PR, or a person's PR. */
export type RunKind = "issue" | "dependency" | "review";
export type CommitState = "pending" | "success" | "failure" | "error";

export interface RunState {
  run_id: string; kind: RunKind; version: string; title: string;
  status: "running" | "waiting" | "completed" | "rolled_back" | "failed" | "cancelled" | "stopped";
  steps: Step[]; gate: Gate | null; agents: AgentRef[]; usage: Usage; budget_usd: number;
  issue_url: string | null; pr_url: string | null; revert_pr_url?: string | null; plan: Plan | null; review: Review | null;
  staging_sha: string | null; canary_sha: string | null; proofs: Proof[]; started_at: number; ended_at: number | null;
  conversation: Feedback[]; plan_revision: number;
  check: CommitState | null; check_description: string; diagnosis: Diagnosis | null;
}

export interface Retry { workflow_id: string; activity: string; attempt: number; last_failure: string; next_attempt_in: number | null }
export interface Worker { pid: number; version: string; build_id: string | null; started_at: number }
export interface Environments {
  staging_sha: string | null; stable_sha: string | null; canary_sha: string | null; canary_weight: number;
  canary_fault_until: number; canary_traffic: { requests: number; errors: number };
  history: { action: string; sha: string | null; run_id: string; at: number }[];
}

export interface RunView {
  run: RunState; workflow_id: string; temporal_run_id: string; agent_run_ids: Record<string, string>; closed: boolean; workflow_status: string; retries: Retry[];
  faults: Record<string, string[]>; environments: Environments; workers: Worker[]; apps: string; read_at: number;
}

export interface RunSummary {
  run_id: string; kind: RunKind; title: string; status: RunState["status"]; version: string; started_at: number;
  ended_at: number | null; usage: Usage; workflow_id: string; temporal_run_id: string; closed: boolean;
  issue_url: string | null; pr_url: string | null;
}
/** Everything this copy of the demo is running: its runs, environments and workers. */
export interface Overview { runs: RunSummary[]; environments: Environments; workers: Worker[]; apps: string }

export interface Issue { id: string; title: string; body: string }
export type Autonomy = "auto" | "supervised";
export interface RunSettings { models: RoleModels; autonomy: Autonomy }
export interface Config {
  owner_label: string; models: Record<string, string[]>; settings: RunSettings; issues: Issue[]; budget_usd: number;
  temporal_ui: string; repo: string; local_controls: boolean; task_queue: string;
}

export type Criterion = "D1" | "D2" | "D3" | "D4" | "D5" | "D6" | "D7";

export interface AgentEvent {
  agent_workflow_id: string; offset: number; timestamp: number; type: string;
  tool_id?: string; tool_name?: string; tool_input?: Record<string, unknown>; output?: unknown; error?: string;
  text?: string; model?: string | null; usage?: { input_tokens?: number | null; output_tokens?: number | null } | null;
  [key: string]: unknown;
}

export async function api<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const response = await fetch(path, {
    ...init,
    method: init?.method ?? (init?.json !== undefined ? "POST" : "GET"),
    headers: init?.json !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: init?.json !== undefined ? JSON.stringify(init.json) : init?.body,
    credentials: "same-origin",
  });
  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    try { detail = JSON.parse(text).detail ?? text; } catch { /* plain text */ }
    throw new Error(detail || `${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function money(n: number): string {
  return `$${n.toFixed(2)}`;
}

export function ago(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

/** A link to an execution in the Temporal UI, which needs the run ID; without one, a search for the ID. */
export function temporalLink(ui: string, workflowId: string, runId?: string | null): string {
  if (runId) return `${ui}/workflows/${encodeURIComponent(workflowId)}/${runId}/history`;
  return `${ui}/workflows?query=${encodeURIComponent(`WorkflowId="${workflowId}"`)}`;
}

/** Retry reasons in the words the audience needs. */
export function explainFailure(message: string): string {
  if (/heartbeat timeout/i.test(message)) return "the worker running it stopped responding";
  if (/start_to_close|start-to-close/i.test(message)) return "it ran past its timeout";
  return message;
}

export function shortSha(sha: string | null | undefined): string {
  return sha ? (sha.length > 12 ? sha.slice(0, 7) : sha) : "—";
}
