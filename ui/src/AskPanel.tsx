import { useState } from "react";
import type { QueryInfo, Run } from "./api";
import { api } from "./api";
import Timeline from "./Timeline";

const EXAMPLES = [
  "Who should I page for payment right now, and who knows it best?",
  "Why does checkout publish orders to Kafka, and who decided that?",
  "What breaks for users if valkey-cart goes down?",
  "Which services are a knowledge risk because only one person knows them?",
  "What were the last incidents involving payment and how were they fixed?",
];

const SESSION = `web-${Math.random().toString(36).slice(2, 8)}`;

export default function AskPanel({ tenant, queries }: { tenant: string; queries: Record<string, QueryInfo> }) {
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<Run[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openTrail, setOpenTrail] = useState<string | null>(null);

  async function ask(q: string) {
    if (!q.trim()) return;
    setBusy(true); setError(null);
    try {
      const run = await api.ask(tenant, q, SESSION);
      setHistory((h) => [run, ...h]);
      setQuestion("");
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ask">
      <p className="muted small">Ask about owners, experts, design decisions, dependencies or past incidents.
        Answers come from graph tools and cite their sources. Follow-up questions remember this conversation.</p>
      <div className="ask-box">
        <textarea rows={2} value={question} placeholder="Ask the company brain…"
                  onChange={(e) => setQuestion(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(question); } }} />
        <button disabled={busy || !question.trim()} onClick={() => ask(question)}>
          {busy ? <span className="spinner" /> : "Ask"}
        </button>
      </div>
      {history.length === 0 && (
        <div className="examples">
          {EXAMPLES.map((e) => <button key={e} className="example" onClick={() => ask(e)} disabled={busy}>{e}</button>)}
        </div>
      )}
      {error && <div className="error-box">{error}</div>}
      {history.map((run) => {
        const result = run.result as { answer?: string; sources?: string[] } | null;
        return (
          <div key={run.id} className="qa">
            <div className="q">{run.goal}</div>
            {run.status === "paused" ? (
              <div className="paused-box">All LLM providers are busy — this question is checkpointed and will resume.</div>
            ) : (
              <div className="a">{result?.answer ?? run.error ?? "No answer"}</div>
            )}
            {result?.sources && result.sources.length > 0 && (
              <div className="chips">{result.sources.map((s) => <span key={s} className="chip">{s}</span>)}</div>
            )}
            <button className="link" onClick={() => setOpenTrail(openTrail === run.id ? null : run.id)}>
              {openTrail === run.id ? "hide" : "show"} how it found this ({run.steps.filter((s) => s.kind === "tool").length} graph queries)
            </button>
            {openTrail === run.id && <Timeline runs={[run]} queries={queries} />}
          </div>
        );
      })}
    </div>
  );
}
