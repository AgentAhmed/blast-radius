import { useState } from "react";
import type { IncidentDetail, QueryInfo } from "./api";
import { ago } from "./api";
import Timeline from "./Timeline";

interface Props {
  detail: IncidentDetail;
  queries: Record<string, QueryInfo>;
  onResolve: (fix: string, kind: string) => Promise<void>;
  onResume: (runId: string) => void;
}

const FIX_KINDS = ["rollback", "flag-off", "config", "restart", "scale", "fix"];

/** One-line cost/memory summary of the investigation: how much work it took and what it remembered. */
function RunStats({ detail }: { detail: IncidentDetail }) {
  const run = detail.runs.find((r) => r.agent === "investigator" && r.status === "done");
  if (!run) return null;
  const llm = run.steps.filter((s) => s.kind === "llm").length;
  const tools = run.steps.filter((s) => s.kind === "tool" && s.tool !== "submit_findings").length;
  const seconds = Math.max(1, run.updated_at - run.started_at);
  const recalled = detail.evidence.context.filter((c) => c.label === "Incident");
  const fresh = recalled.filter((c) => !/^INC-10[1-8]$/.test(c.key));   // learned during this session
  return (
    <div className="run-stats">
      <span><b>{llm}</b> LLM calls</span>
      <span><b>{tools}</b> graph queries</span>
      <span><b>{seconds}s</b> to root cause</span>
      {recalled.length > 0 && (
        <span className={fresh.length ? "memory-hit" : ""}>
          🧠 recalled {recalled.map((c) => c.key).join(", ")}
        </span>
      )}
    </div>
  );
}

export default function IncidentPanel({ detail, queries, onResolve, onResume }: Props) {
  const { incident, finding } = detail;
  const [fix, setFix] = useState("");
  const [kind, setKind] = useState("rollback");
  const [busy, setBusy] = useState(false);
  const working = ["investigating", "resolving"].includes(incident.status);

  return (
    <div className="incident">
      <div className="inc-title">
        <span className={`sev ${incident.severity}`}>{incident.severity}</span>
        <h2>{incident.id} · {incident.title}</h2>
      </div>
      <div className="inc-meta">
        <span className={`status s-${incident.status}`}>{incident.status}{working && <span className="spinner" />}</span>
        <span className="muted">opened {ago(incident.opened_at)}</span>
        <span className="muted">{detail.alerts.length} alerts</span>
      </div>
      <p className="muted small">{incident.summary}</p>

      {finding && <RunStats detail={detail} />}

      {finding ? (
        <section className="card finding">
          <div className="card-head">
            <span>Root cause</span>
            <strong className="root-name">{finding.root_cause}</strong>
            <span className="conf">
              <span className="conf-bar"><i style={{ width: `${Math.round(finding.confidence * 100)}%` }} /></span>
              {Math.round(finding.confidence * 100)}%
            </span>
          </div>
          <p>{finding.summary}</p>
          <h4>Evidence from the graph</h4>
          <ul>{finding.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul>
          <h4>Recommended action</h4>
          <p className="action">{finding.recommended_action}</p>
        </section>
      ) : (
        <section className="card waiting">
          <span className="spinner" /> Investigator is following the graph…
        </section>
      )}

      {(detail.paged.length > 0 || detail.actions.length > 0) && (
        <section className="card">
          <h4>Actions taken</h4>
          {detail.paged.map((p) => (
            <div key={p.handle} className="act">📟 Paged <strong>@{p.handle}</strong> ({p.name}) — {p.reason}</div>
          ))}
          {detail.actions.filter((a) => a.kind === "github_issue").map((a) => (
            <div key={a.id} className="act">
              🐙 GitHub issue — {a.url ? <a href={a.url} target="_blank" rel="noreferrer">{a.url}</a>
                : <span className="muted">{a.status === "dry_run" ? "dry run (set GITHUB_TOKEN + GITHUB_REPO)" : a.status}</span>}
            </div>
          ))}
          {detail.actions.filter((a) => a.kind === "slack" || a.kind === "discord").map((a) => (
            <div key={a.id} className="act">
              💬 Posted to {a.kind === "slack" ? "Slack" : "Discord"} ({a.detail.split(":")[0]}) —{" "}
              <span className={a.status === "sent" ? "" : "muted"}>{a.status}</span>
            </div>
          ))}
        </section>
      )}

      {incident.status === "identified" && (
        <section className="card resolve">
          <h4>Resolve and teach the agents</h4>
          <p className="muted small">The scribe writes a postmortem and a lesson into the graph. Next time a similar incident
            happens, the investigator recalls it.</p>
          <div className="row">
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              {FIX_KINDS.map((k) => <option key={k}>{k}</option>)}
            </select>
            <input placeholder="What fixed it? e.g. Rolled back payment 1.4.0" value={fix}
                   onChange={(e) => setFix(e.target.value)} />
            <button disabled={!fix || busy} onClick={async () => {
              setBusy(true); try { await onResolve(fix, kind); setFix(""); } finally { setBusy(false); }
            }}>Resolve</button>
          </div>
        </section>
      )}
      {detail.resolution.length > 0 && (
        <section className="card resolved">
          <h4>Resolved</h4>
          {detail.resolution.map((r) => <div key={r.id}>✅ {r.kind}: {r.detail}</div>)}
        </section>
      )}

      <h3 className="section-title">Agent trail <span className="muted small">— every step is a node in FalkorDB</span></h3>
      <Timeline runs={detail.runs} queries={queries} onResume={onResume} />
    </div>
  );
}
