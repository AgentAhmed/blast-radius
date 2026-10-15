import { useState } from "react";
import type { ConnectJob } from "./api";
import { api } from "./api";

/** Connect a public GitHub repo as a new tenant graph, and show how to send real alerts to the current one. */
export default function ConnectPanel({ tenant, onConnected }: { tenant: string; onConnected: (t: string) => void }) {
  const [repo, setRepo] = useState("");
  const [realNames, setRealNames] = useState(false);
  const [job, setJob] = useState<ConnectJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showWebhook, setShowWebhook] = useState(false);

  async function start() {
    setError(null);
    try {
      let j = await api.connect(repo, realNames);
      setJob(j);
      while (j.status === "running") {
        await new Promise((r) => setTimeout(r, 1500));
        j = await api.job(j.id);
        setJob(j);
      }
      if (j.status === "done" && j.result) onConnected(j.result.tenant);
    } catch (e) {
      setError(String(e));
    }
  }

  const running = job?.status === "running";
  const origin = window.location.origin;
  const curl = `curl -X POST "${origin}/api/alerts?tenant=${tenant}" \\
  -H "Content-Type: application/json" \\
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"HighErrorRate","service":"<component>","severity":"critical"}}]}'`;

  return (
    <section className="card connect">
      <h4>Connect a GitHub repo</h4>
      <p className="muted small">Builds a new graph from a public repo: services and dependencies from its compose
        files (or <code>blastradius.yaml</code>), owners from <code>CODEOWNERS</code>, real merged PRs, and ADRs.</p>
      <div className="row">
        <input placeholder="owner/repo" value={repo} onChange={(e) => setRepo(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter" && repo && !running) start(); }} />
        <button disabled={!repo || running} onClick={start}>{running ? <span className="spinner" /> : "Connect"}</button>
      </div>
      <label className="check small muted">
        <input type="checkbox" checked={realNames} onChange={(e) => setRealNames(e.target.checked)} />
        show real GitHub usernames (default: stable pseudonyms)
      </label>
      {job && (
        <div className={`job ${job.status}`}>
          {job.status === "running" && <>{job.step}…</>}
          {job.status === "failed" && <>✗ {job.error}</>}
          {job.status === "done" && job.result && (
            <>✓ {job.result.components} components, {job.result.dependencies} dependencies, {job.result.pull_requests} PRs,
              {" "}{job.result.people} people, {job.result.decisions} decisions → tenant <b>{job.result.tenant}</b></>
          )}
        </div>
      )}
      {error && <div className="error-box">{error}</div>}
      <button className="link" onClick={() => setShowWebhook(!showWebhook)}>
        {showWebhook ? "hide" : "send real alerts to"} this tenant (Alertmanager webhook)
      </button>
      {showWebhook && <pre className="cypher small-pre">{curl}</pre>}
    </section>
  );
}
