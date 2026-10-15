"""HTTP API + background worker + (built) web UI, in one process.

    uvicorn blastradius.api:app --port 8007
"""
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from blastradius import config, db, incidents, queries
from blastradius.agents import incident_agents, runtime, store, worker
from blastradius.agents.runtime import humanize_times
from blastradius.data import topology
from blastradius.llm.__main__ import _ollama_running

log = logging.getLogger("blastradius.api")
UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


def tenants() -> list[str]:
    names = [g.decode() if isinstance(g, bytes) else g for g in db.client().list_graphs()]
    return sorted(n.removeprefix("org_") for n in names if n.startswith("org_") and not n.startswith("org_test"))


DEMO_TENANTS = {"astroshop": 42, "nebula": 7}   # tenant -> data seed


@asynccontextmanager
async def lifespan(_app):
    if os.getenv("SEED_ON_START") == "1" and not tenants():
        from blastradius.ingest import ingest
        for name, seed in DEMO_TENANTS.items():
            log.warning("seeding demo tenant %s", name)
            ingest(name, reset=True, seed=seed)
    stop = threading.Event()
    if os.getenv("RUN_WORKER", "1") == "1":
        def loop():
            worker_id = f"api-{os.getpid()}"
            while not stop.is_set():
                for t in tenants():
                    try:
                        worker.process_once(t, worker_id)
                    except Exception:
                        log.exception("worker error (%s)", t)
                stop.wait(2)
        threading.Thread(target=loop, name="worker", daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="Blast Radius", lifespan=lifespan)


def _tenant(t: str | None) -> str:
    t = t or config.DEFAULT_TENANT
    if t not in tenants():
        raise HTTPException(404, f"Unknown tenant '{t}'")
    return t


# --- System ---------------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True, "tenants": tenants()}


@app.get("/api/tenants")
def list_tenants():
    return tenants()


@app.get("/api/llm/status")
def llm_status():
    gw = runtime.gateway()
    out = []
    for s in gw.status():
        p = next(p for p in gw.providers if p.name == s["provider"])
        ready = _ollama_running(p.base_url) if p.local else p.configured
        out.append({**s, "ready": ready})
    return out


# --- Graph ----------------------------------------------------------------------------------

TOPOLOGY_NODES = """
MATCH (c:Component)
OPTIONAL MATCH (t:Team)-[:OWNS]->(c)
OPTIONAL MATCH (a:Alert {status: 'firing'})-[:FIRED_ON]->(c)
RETURN c.name AS id, [l IN labels(c) WHERE l <> 'Component'][0] AS kind, c.tier AS tier,
       c.criticality AS criticality, t.name AS team, count(a) AS firing
"""
TOPOLOGY_EDGES = "MATCH (a:Component)-[d:DEPENDS_ON]->(b:Component) RETURN a.name AS source, b.name AS target, d.via AS via"
STATS = "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC"


@app.get("/api/topology")
def get_topology(tenant: str | None = None):
    g = db.graph(_tenant(tenant))
    return {"nodes": db.rows(g.ro_query(TOPOLOGY_NODES)), "edges": db.rows(g.ro_query(TOPOLOGY_EDGES))}


@app.get("/api/stats")
def stats(tenant: str | None = None):
    rows = db.rows(db.graph(_tenant(tenant)).ro_query(STATS))
    return {r["label"]: r["n"] for r in rows}


@app.get("/api/components/{name}")
def component(name: str, tenant: str | None = None):
    t = _tenant(tenant)
    overview = queries.component_overview(name, t)
    if not overview:
        raise HTTPException(404, "Unknown component")
    return humanize_times({
        "overview": overview, "on_call": queries.who_to_page(name, t), "experts": queries.experts(name, t),
        "decisions": queries.decisions_for(name, t), "fixes": queries.past_fixes(name, t),
        "recent_changes": queries.recent_changes(name, t, window_hours=72),
    })


# Which Cypher each tool runs, so the UI can show it next to each step.
TOOL_QUERIES = {
    "root_cause_candidates": "ROOT_CAUSE_CANDIDATES", "blast_radius": "BLAST_RADIUS",
    "recent_changes": "RECENT_CHANGES", "who_to_page": "WHO_TO_PAGE", "experts": "EXPERTS",
    "similar_incidents": "SIMILAR_INCIDENTS", "past_fixes": "PAST_FIXES", "criticality": "CRITICALITY",
    "path_to_user": "PATH_TO_USER", "decisions_for": "DECISIONS_FOR", "search_docs": "SEARCH_DOCS",
    "bus_factor": "BUS_FACTOR", "component_overview": "COMPONENT_OVERVIEW", "person_profile": "PERSON_PROFILE",
}


@app.get("/api/queries")
def query_catalog():
    return {tool: {"name": const, "cypher": getattr(queries, const).strip()} for tool, const in TOOL_QUERIES.items()}


# --- Incidents ------------------------------------------------------------------------------

@app.get("/api/faults")
def faults(tenant: str | None = None):
    """Demo fault flags where they apply, plus a generic 'fail:<component>' for every component."""
    t = _tenant(tenant)
    names = queries.component_names(t)
    flags = [{"flag": f, "component": c} for f, c in topology.FAULT_FLAGS.items() if c in names]
    return flags + [{"flag": f"{incidents.FAIL_PREFIX}{n}", "component": n} for n in names]


@app.get("/api/tenant")
def tenant_info(tenant: str | None = None):
    rows = db.rows(db.graph(_tenant(tenant)).ro_query(
        "MATCH (m:Meta {key: 'dataset'}) RETURN m.source AS source, m.url AS url, m.topology_from AS topology_from, "
        "m.entry AS entry, m.real_names AS real_names"))
    return (rows[0] if rows and rows[0]["source"] else {"source": "demo", "url": None, "topology_from": "demo",
                                                         "entry": queries.entry_point(tenant), "real_names": False})


# --- Connectors -------------------------------------------------------------------------------

@app.post("/api/alerts")
def receive_alerts(payload: dict | list = Body(...), tenant: str | None = None):
    """Alert webhook: Prometheus Alertmanager format or generic {component, signal, severity, value}."""
    out = incidents.ingest_alerts(payload, _tenant(tenant))
    if out["unmatched"] and not out["accepted"]:
        raise HTTPException(422, out)
    return out


class ConnectBody(BaseModel):
    repo: str
    max_prs: int | None = None
    real_names: bool = False


_jobs: dict[str, dict] = {}


@app.post("/api/connect/github")
def connect_github(body: ConnectBody):
    from blastradius.connectors import github
    try:
        owner, name = github.parse_repo(body.repo)
    except github.ConnectorError as e:
        raise HTTPException(400, str(e))
    job_id = uuid.uuid4().hex[:8]
    job = _jobs[job_id] = {"id": job_id, "repo": f"{owner}/{name}", "status": "running", "step": "Starting",
                           "result": None, "error": None}

    def run():
        try:
            job["result"] = github.connect(body.repo, min(body.max_prs or 0, 300) or None, body.real_names,
                                           progress=lambda m: job.update(step=m))
            job.update(status="done", step="Done")
        except Exception as e:   # surface any failure to the UI
            job.update(status="failed", error=str(e))

    threading.Thread(target=run, name=f"connect-{job_id}", daemon=True).start()
    return job


@app.get("/api/connect/jobs/{job_id}")
def connect_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Unknown job")
    return _jobs[job_id]


class SimulateBody(BaseModel):
    flag: str


@app.post("/api/simulate")
def simulate(body: SimulateBody, tenant: str | None = None):
    t = _tenant(tenant)
    try:
        sim = incidents.simulate_fault(body.flag, t)
    except ValueError as e:
        raise HTTPException(400, str(e))
    inc = incident_agents.triage(t)       # the worker picks up the investigation task
    return {"alerts": sim["alerts"], "incident": inc}


@app.get("/api/incidents")
def list_incidents(tenant: str | None = None):
    return incidents.list_incidents(_tenant(tenant))


RUNS_OF_INCIDENT = """
MATCH (r:AgentRun)-[:INVESTIGATES]->(:Incident {id: $id})
RETURN r.id AS id ORDER BY r.started_at
"""


@app.get("/api/incidents/{incident_id}")
def incident(incident_id: str, tenant: str | None = None):
    t = _tenant(tenant)
    detail = incidents.get_incident(incident_id, t)
    if not detail:
        raise HTTPException(404, "Unknown incident")
    run_ids = [r["id"] for r in db.rows(db.graph(t).ro_query(RUNS_OF_INCIDENT, {"id": incident_id}))]
    detail["runs"] = [_run_view(t, rid) for rid in run_ids]
    detail["evidence"] = evidence_graph(t, detail)
    return detail


class ResolveBody(BaseModel):
    fix: str
    fix_kind: str = "fix"
    root_cause: str | None = None


@app.post("/api/incidents/{incident_id}/resolve")
def resolve(incident_id: str, body: ResolveBody, tenant: str | None = None):
    t = _tenant(tenant)
    try:
        task = incident_agents.resolve(t, incident_id, body.fix, body.fix_kind, body.root_cause)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"task": task}


# --- Runs -----------------------------------------------------------------------------------

def _run_view(tenant, run_id):
    run = store.load_run(tenant, run_id)
    touched: dict[int, list] = {}
    for row in store.touched_by_step(tenant, run_id):
        touched.setdefault(row["step"], []).append({"label": row["label"], "key": row["key"]})
    steps = []
    for s in run["steps"]:
        steps.append({k: s.get(k) for k in ("n", "kind", "tool", "args", "output", "message", "provider",
                                            "model", "tokens", "latency_ms", "at")} | {"touched": touched.get(s["n"], [])})
    return {k: run.get(k) for k in ("id", "agent", "goal", "status", "started_at", "updated_at", "resume_at",
                                    "error", "provider", "result", "incident", "session")} | {"steps": steps}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, tenant: str | None = None):
    t = _tenant(tenant)
    if not store.load_run(t, run_id):
        raise HTTPException(404, "Unknown run")
    return _run_view(t, run_id)


@app.post("/api/runs/{run_id}/resume")
def resume(run_id: str, tenant: str | None = None):
    t = _tenant(tenant)
    run = store.load_run(t, run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    return runtime.execute(t, incident_agents.AGENTS[run["agent"]], run_id)


class AskBody(BaseModel):
    question: str
    session: str | None = None


@app.post("/api/ask")
def ask(body: AskBody, tenant: str | None = None):
    t = _tenant(tenant)
    out = incident_agents.ask(t, body.question, body.session)
    return _run_view(t, out["run_id"])


# --- Evidence graph -------------------------------------------------------------------------

PATH_BETWEEN = """
MATCH (a:Component {name: $a}), (b:Component {name: $b})
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['DEPENDS_ON'], maxLen: 6, pathCount: 1})
YIELD path
RETURN [n IN nodes(path) | n.name] AS path
"""

CONTEXT = """
MATCH (i:Incident {id: $id})-[:HAS_FINDING]->(f:Finding)-[:SUPPORTED_BY]->(n)
WHERE n:PullRequest OR n:Incident OR n:Person OR n:Runbook OR n:Lesson OR n:Decision
OPTIONAL MATCH (n)-[r:MODIFIES|ROOT_CAUSE|FOR|APPLIES_TO|AFFECTS]->(c:Component)
OPTIONAL MATCH (p:Person)-[:AUTHORED]->(n)
RETURN DISTINCT labels(n)[0] AS label, coalesce(n.id, n.handle, toString(n.number)) AS key,
       coalesce(n.title, n.name, n.text) AS title, type(r) AS rel, c.name AS component, p.handle AS author
"""


def evidence_graph(tenant: str, detail: dict) -> dict:
    """The subgraph that explains an incident: alerting components, the dependency paths from them to the
    root cause, and the changes, people and past incidents the finding is supported by."""
    g = db.graph(tenant)
    finding = detail.get("finding") or {}
    root = detail.get("root_cause") or finding.get("root_cause")
    alerting = sorted({a["component"] for a in detail["alerts"]})
    paths = []
    if root:
        for a in alerting:
            if a == root:
                continue
            rows = db.rows(g.ro_query(PATH_BETWEEN, {"a": a, "b": root}))
            if rows:
                paths.append(rows[0]["path"])
    context = db.rows(g.ro_query(CONTEXT, {"id": detail["incident"]["id"]}))
    return {"root_cause": root, "alerting": alerting, "paths": paths, "context": context,
            "paged": [p["handle"] for p in detail.get("paged", [])]}


# --- Web UI (production build) ----------------------------------------------------------------

if UI_DIST.exists():
    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        file = UI_DIST / path
        return FileResponse(file if path and file.is_file() else UI_DIST / "index.html")
