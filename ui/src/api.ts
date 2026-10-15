export type Kind = "Service" | "Datastore" | "Topic";

export interface TopoNode { id: string; kind: Kind; tier: string; criticality: number; team: string; firing: number }
export interface TopoEdge { source: string; target: string; via: string[] }
export interface Topology { nodes: TopoNode[]; edges: TopoEdge[] }

export interface IncidentRow {
  id: string; title: string; severity: string; status: string;
  opened_at: number; resolved_at: number | null; root_cause: string | null;
}

export interface Alert { id: string; component: string; signal: string; severity: string; value: number; fired_at: number }
export interface Finding {
  id: string; root_cause: string; confidence: number; summary: string;
  recommended_action: string; evidence: string[];
}
export interface Step {
  n: number; kind: "llm" | "tool" | "note" | "pause"; tool: string | null; args: string | null;
  output: string | null; message: string | null; provider: string | null; model: string | null;
  tokens: number | null; latency_ms: number | null; at: number;
  touched: { label: string; key: string }[];
}
export interface Run {
  id: string; agent: string; goal: string; status: string; started_at: number; updated_at: number;
  resume_at: number | null; error: string | null; provider: string | null;
  result: Record<string, unknown> | null; incident: string | null; session: string | null; steps: Step[];
}
export interface ContextItem {
  label: string; key: string; title: string; rel: string | null; component: string | null; author: string | null;
}
export interface Evidence {
  root_cause: string | null; alerting: string[]; paths: string[][]; context: ContextItem[]; paged: string[];
}
export interface IncidentDetail {
  incident: { id: string; title: string; severity: string; status: string; opened_at: number;
              resolved_at: number | null; summary: string };
  alerts: Alert[]; impacted: string[]; root_cause: string | null; finding: Finding | null;
  paged: { handle: string; name: string; reason: string }[];
  resolution: { id: string; kind: string; detail: string }[];
  actions: { id: string; kind: string; status: string; url: string | null; detail: string }[];
  runs: Run[]; evidence: Evidence;
}
export interface LlmStatus { provider: string; configured: boolean; ready: boolean; models: string[]; local: boolean }
export interface QueryInfo { name: string; cypher: string }

async function req<T>(path: string, tenant: string, init?: RequestInit): Promise<T> {
  const sep = path.includes("?") ? "&" : "?";
  const res = await fetch(`/api${path}${sep}tenant=${encodeURIComponent(tenant)}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

export const api = {
  tenants: () => fetch("/api/tenants").then((r) => r.json() as Promise<string[]>),
  llm: () => fetch("/api/llm/status").then((r) => r.json() as Promise<LlmStatus[]>),
  queries: () => fetch("/api/queries").then((r) => r.json() as Promise<Record<string, QueryInfo>>),
  faults: () => fetch("/api/faults").then((r) => r.json() as Promise<{ flag: string; component: string }[]>),
  stats: (t: string) => req<Record<string, number>>("/stats", t),
  topology: (t: string) => req<Topology>("/topology", t),
  incidents: (t: string) => req<IncidentRow[]>("/incidents", t),
  incident: (t: string, id: string) => req<IncidentDetail>(`/incidents/${id}`, t),
  component: (t: string, name: string) => req<Record<string, any>>(`/components/${name}`, t),
  simulate: (t: string, flag: string) =>
    req<{ incident: { id: string } | null }>("/simulate", t, { method: "POST", body: JSON.stringify({ flag }) }),
  resolve: (t: string, id: string, fix: string, fix_kind: string) =>
    req(`/incidents/${id}/resolve`, t, { method: "POST", body: JSON.stringify({ fix, fix_kind }) }),
  resume: (t: string, runId: string) => req(`/runs/${runId}/resume`, t, { method: "POST" }),
  ask: (t: string, question: string, session: string) =>
    req<Run>("/ask", t, { method: "POST", body: JSON.stringify({ question, session }) }),
};

export function ago(ts: number | null | undefined): string {
  if (!ts) return "";
  const d = Math.floor(Date.now() / 1000) - ts;
  if (d < 60) return `${Math.max(d, 0)}s ago`;
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}
