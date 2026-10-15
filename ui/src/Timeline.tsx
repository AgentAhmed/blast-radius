import { useState } from "react";
import type { QueryInfo, Run, Step } from "./api";
import { ago } from "./api";

const AGENT_LABEL: Record<string, string> = {
  triage: "Triage agent", investigator: "Investigator agent", scribe: "Scribe agent", brain: "Company brain",
};

function parse(s: string | null): any {
  if (!s) return null;
  try { return JSON.parse(s); } catch { return s; }
}

function StepRow({ step, queries }: { step: Step; queries: Record<string, QueryInfo> }) {
  const [open, setOpen] = useState(false);
  const [showCypher, setShowCypher] = useState(false);
  const msg = parse(step.message);
  const args = parse(step.args);
  const output = parse(step.output);

  if (step.kind === "llm") {
    const calls: string[] = (msg?.tool_calls ?? []).map((c: any) => c.function.name);
    const fellBack = output?.fallbacks?.length;
    return (
      <div className="step llm">
        <div className="step-head">
          <span className="badge llm">LLM</span>
          <span className="mono">{step.provider}/{step.model}</span>
          <span className="muted">{step.latency_ms} ms{step.tokens ? ` · ${step.tokens} tok` : ""}</span>
          {fellBack ? <span className="badge warn" title={JSON.stringify(output.fallbacks)}>fallback ×{fellBack}</span> : null}
        </div>
        {calls.length > 0 && <div className="step-body">decided → {calls.map((c) => <code key={c}>{c}</code>)}</div>}
        {msg?.content && !calls.length && <div className="step-body">{String(msg.content).slice(0, 400)}</div>}
      </div>
    );
  }
  if (step.kind === "pause") {
    return (
      <div className="step pause">
        <span className="badge warn">PAUSED</span> <span>{output?.reason}</span>
      </div>
    );
  }
  if (step.kind === "note") {
    return <div className="step note"><span className="badge">NOTE</span> {msg?.content}</div>;
  }
  const q = step.tool ? queries[step.tool] : undefined;
  const isError = output && typeof output === "object" && "error" in output;
  return (
    <div className={`step tool ${isError ? "error" : ""}`}>
      <div className="step-head" onClick={() => setOpen(!open)}>
        <span className="badge tool">TOOL</span>
        <code className="tool-name">{step.tool}</code>
        <span className="muted mono args">{args ? JSON.stringify(args).slice(0, 90) : ""}</span>
        <span className="chev">{open ? "▾" : "▸"}</span>
      </div>
      {step.touched.length > 0 && (
        <div className="chips">
          {step.touched.slice(0, 12).map((t) => (
            <span key={`${t.label}:${t.key}`} className={`chip chip-${t.label}`}>{t.key}</span>
          ))}
          {step.touched.length > 12 && <span className="chip">+{step.touched.length - 12}</span>}
        </div>
      )}
      {open && (
        <div className="step-detail">
          {q && (
            <button className="link" onClick={() => setShowCypher(!showCypher)}>
              {showCypher ? "hide" : "show"} Cypher ({q.name})
            </button>
          )}
          {showCypher && q && <pre className="cypher">{q.cypher}</pre>}
          <pre className="json">{JSON.stringify(output, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

export default function Timeline({ runs, queries, onResume }: {
  runs: Run[]; queries: Record<string, QueryInfo>; onResume?: (runId: string) => void;
}) {
  return (
    <div className="timeline">
      {runs.map((run) => (
        <div key={run.id} className="run">
          <div className="run-head">
            <strong>{AGENT_LABEL[run.agent] ?? run.agent}</strong>
            <span className={`status s-${run.status}`}>{run.status}</span>
            <span className="muted mono small">{run.id}</span>
            <span className="muted small">{ago(run.started_at)}</span>
          </div>
          {run.status === "paused" && (
            <div className="paused-box">
              All LLM providers are busy; the run is checkpointed in the graph and resumes automatically
              {run.resume_at ? ` at ${new Date(run.resume_at * 1000).toLocaleTimeString()}` : ""}.
              {onResume && <button onClick={() => onResume(run.id)}>Resume now</button>}
            </div>
          )}
          {run.steps.map((s) => <StepRow key={s.n} step={s} queries={queries} />)}
        </div>
      ))}
    </div>
  );
}
