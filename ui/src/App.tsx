import { useCallback, useEffect, useState } from "react";
import { api, ago } from "./api";
import type { IncidentDetail, IncidentRow, LlmStatus, QueryInfo, TenantInfo, Topology } from "./api";
import Graph from "./Graph";
import IncidentPanel from "./IncidentPanel";
import AskPanel from "./AskPanel";
import ComponentDrawer from "./ComponentDrawer";
import ConnectPanel from "./ConnectPanel";

function usePoll(fn: () => void, ms: number, deps: unknown[]) {
  useEffect(() => {
    fn();
    const t = setInterval(fn, ms);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

const params = new URLSearchParams(window.location.search);

export default function App() {
  const [tenants, setTenants] = useState<string[]>([]);
  const [tenant, setTenant] = useState(params.get("tenant") ?? "astroshop");
  const [topology, setTopology] = useState<Topology | null>(null);
  const [incidents, setIncidents] = useState<IncidentRow[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [faults, setFaults] = useState<{ flag: string; component: string }[]>([]);
  const [fault, setFault] = useState("paymentFailure");
  const [llm, setLlm] = useState<LlmStatus[]>([]);
  const [queries, setQueries] = useState<Record<string, QueryInfo>>({});
  const [stats, setStats] = useState<Record<string, number>>({});
  const [tab, setTab] = useState<"incident" | "ask">("incident");
  const [component, setComponent] = useState<string | null>(null);
  const [simulating, setSimulating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<TenantInfo | null>(null);

  useEffect(() => {
    api.tenants().then(setTenants);
    api.queries().then(setQueries);
  }, []);
  usePoll(() => { api.llm().then(setLlm).catch(() => {}); }, 15000, []);

  useEffect(() => {
    setSelectedId(params.get("tenant") === tenant || !params.get("tenant") ? params.get("incident") : null);
    setDetail(null); setComponent(params.get("component"));
    setTab(params.get("tab") === "ask" ? "ask" : "incident");
    api.topology(tenant).then(setTopology).catch((e) => setError(String(e)));
    api.stats(tenant).then(setStats).catch(() => {});
    api.tenantInfo(tenant).then(setInfo).catch(() => setInfo(null));
    api.faults(tenant).then((f) => {
      setFaults(f);
      setFault((cur) => (f.some((x) => x.flag === cur) ? cur : f[0]?.flag ?? ""));
    }).catch(() => {});
  }, [tenant]);

  usePoll(() => {
    api.incidents(tenant).then(setIncidents).catch(() => {});
    api.topology(tenant).then((t) => setTopology((old) => (old ? { ...old, nodes: t.nodes } : t))).catch(() => {});
  }, 4000, [tenant]);

  const loadDetail = useCallback(() => {
    if (!selectedId) return;
    api.incident(tenant, selectedId).then(setDetail).catch(() => {});
  }, [tenant, selectedId]);
  usePoll(loadDetail, 2500, [loadDetail]);

  async function simulate() {
    setSimulating(true); setError(null);
    try {
      const r = await api.simulate(tenant, fault);
      if (r.incident) { setSelectedId(r.incident.id); setTab("incident"); }
      api.incidents(tenant).then(setIncidents);
    } catch (e) {
      setError(String(e));
    } finally {
      setSimulating(false);
    }
  }

  const open = incidents.filter((i) => i.status !== "resolved");
  const past = incidents.filter((i) => i.status === "resolved");

  return (
    <div className="app">
      <header>
        <div className="brand">
          <svg width="26" height="26" viewBox="0 0 32 32"><circle cx="16" cy="16" r="13" fill="none" stroke="#ff5c5c" strokeWidth="3" /><circle cx="16" cy="16" r="5.5" fill="#ff5c5c" /></svg>
          <div>
            <h1>Blast Radius</h1>
            <span className="muted small">AI on-call agents on a FalkorDB graph</span>
          </div>
        </div>
        <div className="header-right">
          <div className="stats">
            {["Component", "Incident", "PullRequest", "AgentRun", "Step"].map((k) => (
              <span key={k}><b>{stats[k] ?? 0}</b> {k === "PullRequest" ? "PRs" : k + "s"}</span>
            ))}
          </div>
          <div className="llm-pills" title="LLM fallback chain, in order">
            {llm.map((p) => (
              <span key={p.provider} className={`pill ${p.ready ? "ok" : "off"}`}>{p.provider}</span>
            ))}
          </div>
          <label className="tenant">
            tenant
            <select value={tenant} onChange={(e) => setTenant(e.target.value)}>
              {tenants.map((t) => <option key={t}>{t}</option>)}
            </select>
          </label>
        </div>
      </header>

      <main>
        <aside className="left">
          <section className="card inject">
            <h4>Inject a fault</h4>
            <p className="muted small">Fails a component: alerts fire on it and on what depends on it, and the agents take over.
              {" "}<span className="muted">(Real alerts: see the webhook below.)</span></p>
            <div className="row">
              <select value={fault} onChange={(e) => setFault(e.target.value)}>
                {faults.map((f) => <option key={f.flag} value={f.flag}>{f.flag}</option>)}
              </select>
              <button onClick={simulate} disabled={simulating}>{simulating ? <span className="spinner" /> : "Inject"}</button>
            </div>
          </section>
          {error && <div className="error-box">{error}</div>}
          {info && (
            <div className="source small">
              {info.url ? <>Source: <a href={info.url} target="_blank" rel="noreferrer">{info.source}</a>
                {" "}· topology from {info.topology_from} · users enter at <b>{info.entry}</b>
                {!info.real_names && " · pseudonymized people"}</>
                : <>Source: demo company on the OpenTelemetry Demo topology (synthetic people and history)</>}
            </div>
          )}
          <h4 className="list-title">Open incidents</h4>
          {open.length === 0 && <p className="muted small">None. Inject a fault to start.</p>}
          {open.map((i) => <IncidentItem key={i.id} i={i} active={i.id === selectedId} onClick={() => { setSelectedId(i.id); setTab("incident"); }} />)}
          <h4 className="list-title">Incident memory</h4>
          {past.map((i) => <IncidentItem key={i.id} i={i} active={i.id === selectedId} onClick={() => { setSelectedId(i.id); setTab("incident"); }} />)}
          <ConnectPanel tenant={tenant} onConnected={(t) => { api.tenants().then(setTenants); setTenant(t); }} />
        </aside>

        <section className="center">
          <Graph key={tenant} topology={topology} root={info?.entry ?? null} evidence={selectedId && detail?.incident.id === selectedId ? detail.evidence : null}
                 selected={component} onSelect={setComponent} />
          {component && <ComponentDrawer tenant={tenant} name={component} onClose={() => setComponent(null)} />}
        </section>

        <aside className="right">
          <div className="tabs">
            <button className={tab === "incident" ? "active" : ""} onClick={() => setTab("incident")}>Investigation</button>
            <button className={tab === "ask" ? "active" : ""} onClick={() => setTab("ask")}>Ask the company brain</button>
          </div>
          <div className="tab-body">
            {tab === "incident" && (detail && selectedId ? (
              <IncidentPanel detail={detail} queries={queries}
                onResolve={async (fix, kind) => { await api.resolve(tenant, detail.incident.id, fix, kind); loadDetail(); }}
                onResume={async (runId) => { await api.resume(tenant, runId); loadDetail(); }} />
            ) : (
              <div className="empty">
                <p>Select an incident, or inject a fault to watch the agents investigate.</p>
                <p className="muted small">Click any node in the graph to see its owner, on-call, experts, decisions and memory.</p>
              </div>
            ))}
            {tab === "ask" && <AskPanel tenant={tenant} queries={queries} />}
          </div>
        </aside>
      </main>
    </div>
  );
}

function IncidentItem({ i, active, onClick }: { i: IncidentRow; active: boolean; onClick: () => void }) {
  return (
    <button className={`inc-item ${active ? "active" : ""}`} onClick={onClick}>
      <div className="row-between">
        <span className={`sev ${i.severity}`}>{i.severity}</span>
        <span className={`status s-${i.status}`}>{i.status}</span>
      </div>
      <div className="inc-item-title">{i.id} · {i.title}</div>
      <div className="muted small">{i.root_cause ? `root cause: ${i.root_cause}` : "investigating…"} · {ago(i.opened_at)}</div>
    </button>
  );
}
