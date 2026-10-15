"""Alerts, incidents and the fault simulator.

The simulator mimics what monitoring would report when one of the OTel Demo's fault flags is turned on:
the failing component and the components that depend on it start alerting. The true cause is stored on a
separate Scenario node that no agent tool ever reads, so it can be used to score the agents honestly.
"""
import re
import time
import uuid
from datetime import datetime

from blastradius import db
from blastradius.data import topology

ALERT_HOPS = 3          # dependents up to this distance start alerting
NO_ALERTS = {"load-generator"}  # synthetic traffic, not monitored

# Dependents of the failing component, with distance, via the derived DEPENDS_ON edge.
AFFECTED = """
MATCH (root:Component {name: $root})
OPTIONAL MATCH p = (dep:Component)-[:DEPENDS_ON*1..3]->(root)
WITH root, dep, min(length(p)) AS hops
RETURN root.name AS root, [l IN labels(root) WHERE l <> 'Component'][0] AS root_kind,
       collect({name: dep.name, hops: hops, consumer: size([(dep)-[:CONSUMES]->(root) | 1]) > 0}) AS dependents
"""

CREATE_ALERTS = """
UNWIND $alerts AS a
MATCH (c:Component {name: a.component})
CREATE (al:Alert {id: a.id, signal: a.signal, severity: a.severity, value: a.value,
                  fired_at: a.fired_at, status: 'firing'})
CREATE (al)-[:FIRED_ON]->(c)
"""

NEXT_INCIDENT_NUMBER = """
MATCH (i:Incident) RETURN coalesce(max(toInteger(substring(i.id, 4))), 100) + 1 AS n
"""

UNGROUPED_ALERTS = """
MATCH (a:Alert {status: 'firing'})-[:FIRED_ON]->(c:Component)
WHERE NOT (a)<-[:TRIGGERED_BY]-(:Incident)
RETURN a.id AS id, a.signal AS signal, a.severity AS severity, a.value AS value,
       a.fired_at AS fired_at, c.name AS component
ORDER BY fired_at
"""

FLOWS_OF = """
UNWIND $names AS n
MATCH (c:Component {name: n})-[:SERVES]->(f:UserFlow)
RETURN collect(DISTINCT f.name) AS flows
"""

CREATE_INCIDENT = """
CREATE (i:Incident {id: $id, title: $title, severity: $severity, status: 'investigating',
                    opened_at: $now, summary: $summary})
WITH i
UNWIND $alert_ids AS aid
MATCH (a:Alert {id: aid})-[:FIRED_ON]->(c:Component)
MERGE (i)-[:TRIGGERED_BY]->(a)
MERGE (i)-[:IMPACTED]->(c)
"""

INCIDENT_DETAIL = """
MATCH (i:Incident {id: $id})
OPTIONAL MATCH (i)-[:TRIGGERED_BY]->(a:Alert)-[:FIRED_ON]->(c:Component)
WITH i, collect({id: a.id, component: c.name, signal: a.signal, severity: a.severity, value: a.value,
                 fired_at: a.fired_at}) AS alerts
OPTIONAL MATCH (i)-[:IMPACTED]->(imp:Component)
WITH i, alerts, collect(DISTINCT imp.name) AS impacted
OPTIONAL MATCH (i)-[:ROOT_CAUSE]->(rc:Component)
OPTIONAL MATCH (i)-[:HAS_FINDING]->(f:Finding)
WITH i, alerts, impacted, rc, f ORDER BY f.created_at DESC
WITH i, alerts, impacted, rc, collect(f)[0] AS f
OPTIONAL MATCH (i)-[pg:PAGED]->(p:Person)
OPTIONAL MATCH (i)-[:RESOLVED_BY]->(act:Action)
OPTIONAL MATCH (i)-[:HAS_ACTION]->(other:Action)
RETURN i {.id, .title, .severity, .status, .opened_at, .resolved_at, .summary} AS incident,
       alerts, impacted, rc.name AS root_cause,
       f {.id, .root_cause, .confidence, .summary, .recommended_action, .evidence} AS finding,
       collect(DISTINCT p {.handle, .name, reason: pg.reason}) AS paged,
       collect(DISTINCT act {.id, .kind, .detail}) AS resolution,
       collect(DISTINCT other {.id, .kind, .status, .url, .detail}) AS actions
"""

LIST_INCIDENTS = """
MATCH (i:Incident)
OPTIONAL MATCH (i)-[:ROOT_CAUSE]->(rc:Component)
OPTIONAL MATCH (i)-[:HAS_FINDING]->(f:Finding)
WITH i, rc, collect(f.root_cause)[0] AS found
RETURN i.id AS id, i.title AS title, i.severity AS severity, i.status AS status,
       i.opened_at AS opened_at, i.resolved_at AS resolved_at,
       coalesce(rc.name, found) AS root_cause
ORDER BY opened_at DESC
LIMIT $limit
"""


FAIL_PREFIX = "fail:"   # generic fault for any tenant: "fail:<component>"


def fault_root(flag: str, tenant: str | None = None) -> str:
    if flag in topology.FAULT_FLAGS:
        return topology.FAULT_FLAGS[flag]
    if flag.startswith(FAIL_PREFIX):
        name = flag[len(FAIL_PREFIX):]
        if db.rows(db.graph(tenant).ro_query("MATCH (c:Component {name: $n}) RETURN c.name AS n", {"n": name})):
            return name
    raise ValueError(f"Unknown fault '{flag}'. Use one of {', '.join(topology.FAULT_FLAGS)} "
                     f"or '{FAIL_PREFIX}<component>'")


def simulate_fault(flag: str, tenant: str | None = None, now: int | None = None) -> dict:
    """Inject a fault (a demo flag or 'fail:<component>'): create the alerts monitoring would fire."""
    root = fault_root(flag, tenant)
    g = db.graph(tenant)
    now = now or int(time.time())
    info = db.rows(g.query(AFFECTED, {"root": root}))[0]

    alerts = []
    if info["root_kind"] == "Service":
        alerts.append(_alert(root, "error_rate", "critical", 0.42, now))
    for d in sorted(info["dependents"], key=lambda d: d["hops"] or 0):
        if not d["name"] or d["name"] in NO_ALERTS or d["hops"] > ALERT_HOPS:
            continue
        if d["consumer"]:      # consumers of a broken topic fall behind rather than erroring
            signal, sev, value = "consumer_lag", "warning", 1800
        else:
            signal = "error_rate" if d["hops"] <= 2 else "latency_p99"
            sev = "critical" if d["hops"] == 1 else "warning"
            value = round(0.3 / d["hops"], 3) if signal == "error_rate" else 2400
        alerts.append(_alert(d["name"], signal, sev, value, now + 20 * d["hops"]))
    batch = uuid.uuid4().hex[:6]   # unique even when several faults are injected in the same second
    for i, a in enumerate(alerts):
        a["id"] = f"AL-{now}-{batch}-{i}"

    g.query(CREATE_ALERTS, {"alerts": alerts})
    g.query("CREATE (:Scenario {flag: $flag, root: $root, started_at: $now})",
            {"flag": flag, "root": root, "now": now})
    return {"flag": flag, "alerts": alerts}


# --- Real alerts: Prometheus Alertmanager webhook or generic JSON ------------------------------

COMPONENT_LABELS = ("component", "service", "service_name", "service.name", "app", "app_kubernetes_io_name",
                    "app.kubernetes.io/name", "k8s_deployment_name", "deployment", "container", "job", "pod")

RESOLVE_ALERTS = """
UNWIND $rows AS r
MATCH (a:Alert {status: 'firing', signal: r.signal})-[:FIRED_ON]->(:Component {name: r.component})
SET a.status = 'resolved', a.resolved_at = $now
RETURN count(a) AS n
"""


def match_component(value: str, names: list[str]) -> str | None:
    """Map a label value to a component: exact, then without common affixes, then pod/replica-set names."""
    v = value.strip().lower().split("/")[-1]
    if v in names:
        return v
    for affix in ("-service", "service", "-svc", "-server", "-api", "-app"):
        if v.endswith(affix) and v[: -len(affix)] in names:
            return v[: -len(affix)]
    prefixed = [n for n in names if v.startswith(n + "-")]          # cart-7d9f8-abcde -> cart
    return max(prefixed, key=len) if prefixed else None


def _signal(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()).strip("_") or "alert"


def parse_alerts(payload, names: list[str]) -> tuple[list[dict], list[dict], list[str]]:
    """Alertmanager or generic JSON -> (firing alerts, resolved alerts, unmatched label values)."""
    items = payload if isinstance(payload, list) else payload.get("alerts", [payload])
    firing, resolved, unmatched = [], [], []
    for item in items:
        labels = item.get("labels") or {}
        annotations = item.get("annotations") or {}
        raw = item.get("component") or next((labels[k] for k in COMPONENT_LABELS if labels.get(k)), None)
        component = match_component(str(raw), names) if raw else None
        if not component:
            unmatched.append(str(raw or labels or item))
            continue
        alert = {
            "component": component,
            "signal": _signal(item.get("signal") or labels.get("alertname") or "alert"),
            "severity": str(item.get("severity") or labels.get("severity") or "warning").lower(),
            "value": item.get("value") or annotations.get("value") or annotations.get("summary"),
            "fired_at": _ts(item.get("startsAt")) or int(time.time()),
        }
        (resolved if str(item.get("status", "firing")).lower() == "resolved" else firing).append(alert)
    return firing, resolved, unmatched


def _ts(value) -> int | None:
    if not value or str(value).startswith("0001"):
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def ingest_alerts(payload, tenant: str | None = None) -> dict:
    """Store real alerts from a webhook. Firing alerts are triaged by the worker like simulated ones."""
    g = db.graph(tenant)
    names = [r["n"] for r in db.rows(g.ro_query("MATCH (c:Component) RETURN c.name AS n"))]
    firing, resolved, unmatched = parse_alerts(payload, names)
    batch = uuid.uuid4().hex[:6]
    for i, a in enumerate(firing):
        a["id"] = f"AL-{a['fired_at']}-{batch}-{i}"
        a["value"] = None if a["value"] is None else str(a["value"])[:200]
    if firing:
        g.query(CREATE_ALERTS, {"alerts": firing})
    closed = 0
    if resolved:
        closed = db.rows(g.query(RESOLVE_ALERTS, {"rows": resolved, "now": int(time.time())}))[0]["n"]
    return {"accepted": [{k: a[k] for k in ("id", "component", "signal", "severity")} for a in firing],
            "resolved": closed, "unmatched": unmatched, "known_components": names if unmatched else None}


def _alert(component, signal, severity, value, fired_at):
    return {"component": component, "signal": signal, "severity": severity, "value": value, "fired_at": fired_at}


def ungrouped_alerts(tenant: str | None = None) -> list[dict]:
    return db.rows(db.graph(tenant).ro_query(UNGROUPED_ALERTS))


def open_incident(alerts: list[dict], tenant: str | None = None, now: int | None = None) -> dict:
    """Group alerts into a new incident. Severity comes from which user flows are hit."""
    g = db.graph(tenant)
    now = now or int(time.time())
    number = db.rows(g.query(NEXT_INCIDENT_NUMBER))[0]["n"]
    names = sorted({a["component"] for a in alerts})
    flows = db.rows(g.ro_query(FLOWS_OF, {"names": names}))[0]["flows"]
    if "checkout" in flows:
        severity = "SEV1"
    elif len(names) >= 3 or "order-processing" in flows:
        severity = "SEV2"
    else:
        severity = "SEV3"
    first = alerts[0]
    title = f"{first['signal'].replace('_', ' ')} on {first['component']}"
    if len(names) > 1:
        title += f" and {len(names) - 1} more"
    summary = f"{len(alerts)} alerts on {', '.join(names)}. User flows at risk: {', '.join(flows) or 'none'}."
    incident_id = f"INC-{number}"
    g.query(CREATE_INCIDENT, {"id": incident_id, "title": title, "severity": severity, "now": now,
                              "summary": summary, "alert_ids": [a["id"] for a in alerts]})
    return {"id": incident_id, "title": title, "severity": severity, "components": names, "flows": flows,
            "summary": summary}


def get_incident(incident_id: str, tenant: str | None = None) -> dict | None:
    rows = db.rows(db.graph(tenant).ro_query(INCIDENT_DETAIL, {"id": incident_id}))
    if not rows or not rows[0]["incident"]:
        return None
    r = rows[0]
    r["alerts"] = [a for a in r["alerts"] if a.get("id")]
    r["paged"] = [p for p in r["paged"] if p.get("handle")]
    r["resolution"] = [a for a in r["resolution"] if a.get("id")]
    r["actions"] = [a for a in r["actions"] if a.get("id")]
    return r


def list_incidents(tenant: str | None = None, limit: int = 30) -> list[dict]:
    return db.rows(db.graph(tenant).ro_query(LIST_INCIDENTS, {"limit": limit}))
