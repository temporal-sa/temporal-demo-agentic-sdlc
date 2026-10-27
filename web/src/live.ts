// Live data: one SSE stream per run view (run snapshots + agent events), and a light poll
// for the session list. Mirrors the buildkit's live.js behaviour: reconnect with backoff,
// pause polling while the tab is hidden, and say how fresh the data is.

import { useEffect, useReducer, useRef, useState } from "react";
import { api, type AgentEvent, type RunView, type Overview } from "./api";

export interface ToolCall { id: string; name: string; input: string; status: "running" | "done" | "error" | "awaiting"; result?: string; at: number }
export interface AgentFeed {
  id: string;
  modelCalls: number;
  inTokens: number;
  outTokens: number;
  thinking: boolean;
  replyTail: string;
  tools: ToolCall[];
  approvals: ToolCall[];
  lastAt: number;
}

type Feeds = Record<string, AgentFeed>;

function emptyFeed(id: string): AgentFeed {
  return { id, modelCalls: 0, inTokens: 0, outTokens: 0, thinking: false, replyTail: "", tools: [], approvals: [], lastAt: 0 };
}

function describeInput(input: Record<string, unknown> | undefined): string {
  if (!input) return "";
  const path = input.path ?? input.pattern ?? input.command;
  if (typeof path === "string") return path;
  const first = Object.values(input)[0];
  return typeof first === "string" ? first.slice(0, 80) : "";
}

function reduceFeeds(feeds: Feeds, event: AgentEvent | "reset"): Feeds {
  if (event === "reset") return {};
  const feed = { ...(feeds[event.agent_workflow_id] ?? emptyFeed(event.agent_workflow_id)) };
  feed.lastAt = event.timestamp;
  const id = event.tool_id ?? "";
  switch (event.type) {
    case "model_interaction_started":
      feed.thinking = true;
      feed.replyTail = "";
      break;
    case "model_interaction_ended":
      feed.thinking = false;
      feed.modelCalls += 1;
      feed.inTokens += event.usage?.input_tokens ?? 0;
      feed.outTokens += event.usage?.output_tokens ?? 0;
      break;
    case "reply_delta":
      feed.replyTail = (feed.replyTail + (event.text ?? "")).slice(-220);
      break;
    case "tool_requested":
      // Pydantic AI returns structured output through a tool named final_result; it isn't agent work.
      if (event.tool_name === "final_result") break;
      if (!feed.tools.some((t) => t.id === id)) {
        feed.tools = [...feed.tools, { id, name: event.tool_name ?? "tool", input: describeInput(event.tool_input), status: "running" as const, at: event.timestamp }].slice(-40);
      }
      break;
    case "tool_approval_requested":
      feed.approvals = [...feed.approvals.filter((a) => a.id !== id),
        { id, name: event.tool_name ?? "tool", input: describeInput(event.tool_input), status: "awaiting", at: event.timestamp }];
      feed.tools = feed.tools.map((t) => (t.id === id ? { ...t, status: "awaiting" } : t));
      break;
    case "tool_approval_resolved":
      feed.approvals = feed.approvals.filter((a) => a.id !== id);
      feed.tools = feed.tools.map((t) => (t.id === id && t.status === "awaiting" ? { ...t, status: "running" } : t));
      break;
    case "tool_end":
      feed.tools = feed.tools.map((t) => (t.id === id ? { ...t, status: "done", result: String(event.output ?? "").slice(0, 160) } : t));
      break;
    case "tool_error":
      feed.tools = feed.tools.map((t) => (t.id === id ? { ...t, status: "error", result: String(event.error ?? "") } : t));
      break;
  }
  return { ...feeds, [event.agent_workflow_id]: feed };
}

export type Freshness = "idle" | "live" | "stale";

export function useRunStream(runId: string | null) {
  const [view, setView] = useState<RunView | null>(null);
  const [feeds, dispatch] = useReducer(reduceFeeds, {});
  const [freshness, setFreshness] = useState<Freshness>("idle");
  const lastMessage = useRef(0);

  useEffect(() => {
    setView(null);
    dispatch("reset");
    if (!runId) return;
    let source: EventSource | null = null;
    let retry = 0;
    let closed = false;
    let timer: number | undefined;

    const open = () => {
      source = new EventSource(`/api/runs/${runId}/stream`);
      source.addEventListener("run", (e) => {
        retry = 0;
        lastMessage.current = Date.now();
        setFreshness("live");
        setView(JSON.parse((e as MessageEvent).data));
      });
      source.addEventListener("agent", (e) => {
        lastMessage.current = Date.now();
        dispatch(JSON.parse((e as MessageEvent).data));
      });
      source.onerror = () => {
        setFreshness("stale");
        source?.close();
        if (!closed) timer = window.setTimeout(open, Math.min(8000, 500 * 2 ** retry++));
      };
    };
    // A run's agent events replay from offset 0 on reconnect, so start the feeds afresh.
    const reopen = () => { dispatch("reset"); open(); };
    reopen();
    return () => { closed = true; source?.close(); window.clearTimeout(timer); };
  }, [runId]);

  return { view, feeds, freshness };
}

export function usePoll<T>(path: string, intervalMs: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const run = async () => {
      if (document.visibilityState === "visible") {
        try {
          const result = await api<T>(path);
          if (!cancelled) setData(result);
        } catch { /* keep the last good data; the next poll retries */ }
      }
      if (!cancelled) timer = window.setTimeout(run, intervalMs);
    };
    run();
    return () => { cancelled = true; window.clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, intervalMs, tick, ...deps]);
  return { data, refresh: () => setTick((t) => t + 1) };
}

export type { Overview };

export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now() / 1000), intervalMs);
    return () => window.clearInterval(t);
  }, [intervalMs]);
  return now;
}
