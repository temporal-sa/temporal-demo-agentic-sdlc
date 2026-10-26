// The story the console tells, shared with the deck (deck/outline.md) and docs/story.md.
// Every number here has a source; live numbers on screen come from the running system.

import type { Criterion } from "./api";

export interface Source { label: string; url: string }

export const SOURCES = {
  forrester: {
    label: "Diego Lo Giudice, Forrester (June 8, 2026)",
    url: "http://www.forrester.com/blogs/agentic-software-development-takes-the-lead-from-code-assistants-to-orchestrated-sdlc-agents/",
  },
  keyhole: {
    label: "Keyhole Software, 2026 adoption and impact data (Sept 2, 2026)",
    url: "https://keyholesoftware.com/agentic-ai-in-the-software-development-lifecycle-2026-adoption-and-impact-data/",
  },
  ciklum: { label: "Julian Beckers, Ciklum (July 12, 2026)", url: "https://www.ciklum.com/blog/ai-revolutionize-software-development-lifecycle/" },
  duolingo: { label: "Jesse Welch, Duolingo Engineering (Dec 4, 2025)", url: "https://blog.duolingo.com/buildingaiagents" },
  port: { label: "Zohar Einy, Port (2026)", url: "https://www.port.io/blog/agentic-sdlc-software-lifecycle-rebuilt-around-agents" },
  kanbots: { label: "KanBots, Parallelize AI coding agents", url: "https://kanbots.dev/learn/parallelize-ai-coding-agents" },
  veza: { label: "Matt Biggin, Veza Digital (April 17, 2026)", url: "https://vezadigital.com/post/best-ai-agents" },
} satisfies Record<string, Source>;

export const ACTS = [
  { key: "result", label: "The result" },
  { key: "why", label: "Why agentic SDLC" },
  { key: "hard", label: "Why it's hard" },
  { key: "live", label: "Live run" },
  { key: "change", label: "Change the pipeline" },
  { key: "proof", label: "Proof" },
] as const;
export type ActKey = (typeof ACTS)[number]["key"];

export const STATS = [
  {
    value: "30–40% → under 10%",
    claim: "Coding may improve by 30% to 40%, but if planning, testing, and release remain manual, overall team productivity often increases by less than 10%.",
    source: SOURCES.forrester,
  },
  {
    value: "84–90% vs 13%",
    claim: "Roughly 84–90% of developers use AI tools for coding, but only about 13% of teams have AI operating across the full development lifecycle.",
    source: SOURCES.keyhole,
  },
  {
    value: "Review, quality, governance",
    claim: "The new bottleneck is shifting from code generation to review, quality, governance, and architecture.",
    source: SOURCES.ciklum,
  },
];

export const LIFECYCLE = ["Plan", "Code", "Test", "Review", "Release", "Operate"];

export interface CriterionInfo {
  id: Criterion;
  title: string; // what a team needs proven, in its words
  problem: string; // what goes wrong without it
  quote?: { text: string; source: Source };
  beat: string; // what the audience sees in the live run
  temporal: string; // the Temporal feature that resolves it
}

export const CRITERIA: CriterionInfo[] = [
  {
    id: "D1",
    title: "An agent crash loses no work and re-spends no tokens",
    problem: "A coding agent dies 15 minutes into a task. You start over and pay for every model call again.",
    quote: {
      text: "Even a well prompted agent may go off the rails and crash, fail to produce any changes, hang/freeze, or otherwise fail to complete, and need to be retried.",
      source: SOURCES.duolingo,
    },
    beat: "Kill the worker mid-turn. The agent continues on a new worker from its last completed step.",
    temporal: "Model and tool calls are Activities; Event History keeps the ones that finished",
  },
  {
    id: "D2",
    title: "Humans stay accountable without babysitting",
    problem: "Approvals take hours. A script holding a process open for a reviewer is a process waiting to crash.",
    quote: { text: "A deploy agent might run free in staging but need approval in production, or run on its own at noon but check with a human at 3 a.m.", source: SOURCES.port },
    beat: "The run waits at plan approval and the production gate, for minutes or days, using no compute.",
    temporal: "Durable waits answered by Updates",
  },
  {
    id: "D3",
    title: "Parallel agents, flaky APIs and spend stay under control",
    problem: "Four agents cost four times as much, and every provider and API rate limits you.",
    quote: { text: "Four agents in parallel is four times the burn.", source: SOURCES.kanbots },
    beat: "Coding agents run in parallel. One is rate limited and retries while the others keep going. The run stops at its budget.",
    temporal: "Child Workflows, Retry Policies, and a budget the Workflow enforces",
  },
  {
    id: "D4",
    title: "A bad change in production is caught and undone",
    problem: "Agents ship more changes, faster. Without a canary and a way back, a bad one reaches every customer.",
    quote: { text: "An ungoverned one makes change failure rate and MTTR worse, because nothing is there to catch a bad change before it spreads.", source: SOURCES.port },
    beat: "The canary fails its checks. The pipeline stops its traffic, reverts the merge and reopens the issue.",
    temporal: "Durable timers and a saga of compensations",
  },
  {
    id: "D5",
    title: "The pipeline can change while runs are in flight",
    problem: "Teams change agents, prompts and stages every week. A run that started yesterday mustn't pick up today's change halfway.",
    beat: "Roll out v2 with a security agent. Running work finishes on v1; new runs get the new stage.",
    temporal: "Worker Versioning with Pinned Workflows",
  },
  {
    id: "D6",
    title: "Every agent action is auditable",
    problem: "Agent sprawl, inherited credentials, no record of which agent did what.",
    quote: { text: "What you get is agent sprawl: agents taking destructive actions with no audit trail, no respect for PII, and no scoped credentials.", source: SOURCES.port },
    beat: "Open the run in Temporal: every model call, tool call, approval and retry, in order.",
    temporal: "Event History",
  },
  {
    id: "D7",
    title: "People can steer agents mid-flight without starting over",
    problem: "An approve-or-reject button makes a person a gatekeeper. Developers need to redirect agents where they already work, and a new requirement mustn't mean starting from scratch.",
    beat: "Answer the planner's questions, message a coder while it works, comment on the PR from GitHub. Feedback is fixed on the spot; a new requirement sends the run back to planning.",
    temporal: "Updates and durable waits that read GitHub; the Workflow routes each comment to the right agent",
  },
];

export const RECAP = [
  "A crashed worker cost seconds, not the agent's work.",
  "People approved the plan and the release, on their own schedule.",
  "Agents worked in parallel and retried through rate limits, inside a budget the Workflow enforces.",
  "A bad canary was rolled back and reported without anyone paging in.",
  "The pipeline itself changed while work was in flight.",
  "Developers steered the agents from the console and from GitHub, and nobody had to start over.",
  "Every step is on the record.",
];
