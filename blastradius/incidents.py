"""Alerts, incidents and the fault simulator.

The simulator mimics what monitoring would report when one of the OTel Demo's fault flags is turned on:
the failing component and the components that depend on it start alerting. The true cause is stored on a
separate Scenario node that no agent tool ever reads, so it can be used to score the agents honestly.
"""
import time

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


def simulate_fault(flag: str, tenant: str | None = None, now: int | None = None) -> dict:
    """Turn on a demo fault flag: create the alerts monitoring would fire. Returns the alerts."""
    if flag not in topology.FAULT_FLAGS:
        raise ValueError(f"Unknown fault flag '{flag}'. Known: {', '.join(topology.FAULT_FLAGS)}")
    g = db.graph(tenant)
    now = now or int(time.time())
    root = topology.FAULT_FLAGS[flag]
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
    for i, a in enumerate(alerts):
        a["id"] = f"AL-{now}-{i}"

    g.query(CREATE_ALERTS, {"alerts": alerts})
    g.query("CREATE (:Scenario {flag: $flag, root: $root, started_at: $now})",
            {"flag": flag, "root": root, "now": now})
    return {"flag": flag, "alerts": alerts}


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
