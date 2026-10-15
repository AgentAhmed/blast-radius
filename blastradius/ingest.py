"""Load topology + synthetic company data into a tenant graph.

Usage:
    python -m blastradius.ingest --tenant astroshop --reset
"""
import argparse
import time

from blastradius import db, schema
from blastradius.data import synthetic, topology


def load_topology(g) -> None:
    g.query(
        "UNWIND $rows AS r "
        "MERGE (s:Component:Service {name: r.name}) "
        "SET s.language = r.language, s.tier = r.tier",
        {"rows": [{"name": n, "language": lang, "tier": tier} for n, _, lang, tier in topology.SERVICES]},
    )
    g.query(
        "UNWIND $rows AS r MERGE (d:Component:Datastore {name: r.name}) SET d.kind = r.kind, d.tier = 'core'",
        {"rows": [{"name": n, "kind": k} for n, k in topology.DATASTORES]},
    )
    g.query(
        "UNWIND $names AS n MERGE (t:Component:Topic {name: n}) SET t.kind = 'kafka', t.tier = 'core'",
        {"names": topology.TOPICS},
    )
    g.query(
        "UNWIND $rows AS r "
        "MATCH (a:Component {name: r.src}), (b:Component {name: r.dst}) "
        "MERGE (a)-[c:CALLS]->(b) SET c.protocol = r.protocol",
        {"rows": [{"src": a, "dst": b, "protocol": p} for a, b, p in topology.CALLS]},
    )
    for rel, pairs in (("READS_WRITES", topology.READS_WRITES),
                       ("PUBLISHES", topology.PUBLISHES),
                       ("CONSUMES", topology.CONSUMES)):
        g.query(
            "UNWIND $rows AS r "
            "MATCH (a:Component {name: r.src}), (b:Component {name: r.dst}) "
            f"MERGE (a)-[:{rel}]->(b)",
            {"rows": [{"src": a, "dst": b} for a, b in pairs]},
        )
    g.query(
        "UNWIND $rows AS r "
        "MERGE (f:UserFlow {name: r.flow}) "
        "WITH f, r MATCH (c:Component {name: r.comp}) MERGE (c)-[:SERVES]->(f)",
        {"rows": [{"flow": f, "comp": c} for f, comps in topology.USER_FLOWS.items() for c in comps]},
    )


def load_org(g, data: dict) -> None:
    g.query(
        "UNWIND $rows AS r MERGE (t:Team {name: r.name}) "
        "WITH t, r UNWIND r.owns AS comp MATCH (c:Component {name: comp}) MERGE (t)-[:OWNS]->(c)",
        {"rows": data["teams"]},
    )
    g.query(
        "UNWIND $rows AS r MERGE (p:Person {handle: r.handle}) SET p.name = r.name, p.role = r.role "
        "WITH p, r MATCH (t:Team {name: r.team}) MERGE (p)-[:MEMBER_OF]->(t)",
        {"rows": data["people"]},
    )
    g.query(
        "UNWIND $rows AS r MATCH (p:Person {handle: r.handle}), (t:Team {name: r.team}) "
        "CREATE (p)-[:ON_CALL_FOR {from: r.from, to: r.to}]->(t)",
        {"rows": data["on_call"]},
    )
    g.query(
        "UNWIND $rows AS r "
        "MERGE (pr:PullRequest {number: r.number}) SET pr.title = r.title, pr.merged_at = r.merged_at "
        "WITH pr, r MATCH (p:Person {handle: r.author}), (c:Component {name: r.service}) "
        "MERGE (p)-[:AUTHORED]->(pr) MERGE (pr)-[:MODIFIES]->(c)",
        {"rows": data["prs"]},
    )
    g.query(
        "UNWIND $rows AS r "
        "MERGE (rel:Release {id: r.id}) SET rel.version = r.version, rel.deployed_at = r.deployed_at "
        "WITH rel, r MATCH (c:Component {name: r.service}), (pr:PullRequest {number: r.pr}) "
        "MERGE (rel)-[:DEPLOYS]->(c) MERGE (rel)-[:INCLUDES]->(pr)",
        {"rows": data["releases"]},
    )
    g.query(
        "UNWIND $rows AS r "
        "MERGE (i:Incident {id: r.id}) "
        "SET i.title = r.title, i.severity = r.severity, i.status = r.status, "
        "    i.opened_at = r.opened_at, i.resolved_at = r.resolved_at, i.summary = r.summary "
        "WITH i, r "
        "MATCH (root:Component {name: r.root_cause}), (h:Person {handle: r.handled_by}) "
        "MERGE (i)-[:ROOT_CAUSE]->(root) "
        "MERGE (i)-[:HANDLED_BY]->(h) "
        "MERGE (a:Action {id: r.action.id}) SET a.kind = r.action.kind, a.detail = r.action.detail, a.at = r.action.at "
        "MERGE (i)-[:RESOLVED_BY]->(a) "
        "MERGE (d:Document {id: r.postmortem.id}) "
        "SET d.kind = 'postmortem', d.title = r.postmortem.title, d.body = r.postmortem.body "
        "MERGE (d)-[:POSTMORTEM_OF]->(i) "
        "MERGE (d)-[:ABOUT]->(root) "
        "WITH i, r UNWIND r.impacted AS imp MATCH (c:Component {name: imp}) MERGE (i)-[:IMPACTED]->(c)",
        {"rows": data["incidents"]},
    )
    g.query(
        "UNWIND $rows AS r "
        "MERGE (d:Decision {id: r.id}) "
        "SET d.title = r.title, d.date = r.date, d.status = r.status, d.rationale = r.rationale "
        "MERGE (doc:Document {id: r.id + '-doc'}) "
        "SET doc.kind = 'adr', doc.title = r.id + ': ' + r.title, doc.body = r.rationale "
        "MERGE (d)-[:RECORDED_IN]->(doc) "
        "WITH d, doc, r MATCH (p:Person {handle: r.made_by}) MERGE (p)-[:MADE]->(d) "
        "WITH d, doc, r UNWIND r.affects AS comp MATCH (c:Component {name: comp}) "
        "MERGE (d)-[:AFFECTS]->(c) MERGE (doc)-[:ABOUT]->(c)",
        {"rows": data["decisions"]},
    )
    g.query(
        "UNWIND $rows AS r "
        "MERGE (rb:Runbook {id: r.id}) SET rb.title = r.title, rb.steps = r.steps "
        "WITH rb, r MATCH (c:Component {name: r.service}) MERGE (rb)-[:FOR]->(c)",
        {"rows": data["runbooks"]},
    )


def ingest(tenant: str, reset: bool = False, now: int | None = None, seed: int = 42) -> dict:
    g = db.graph(tenant)
    if reset:
        try:
            g.delete()
        except Exception:
            pass  # graph did not exist yet
        g = db.graph(tenant)
    now = now or int(time.time())
    schema.create_indexes(g)
    load_topology(g)
    load_org(g, synthetic.generate(now, seed))
    g.query("MERGE (m:Meta {key: 'dataset'}) SET m.generated_at = $now, m.seed = $seed",
            {"now": now, "seed": seed})
    counts = db.rows(g.query("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY label"))
    return {r["label"]: r["n"] for r in counts}


def main() -> None:
    ap = argparse.ArgumentParser(description="Load a tenant graph into FalkorDB")
    ap.add_argument("--tenant", default="astroshop")
    ap.add_argument("--reset", action="store_true", help="drop the tenant graph first")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    counts = ingest(args.tenant, args.reset, seed=args.seed)
    print(f"Loaded graph {db.graph_name(args.tenant)}:")
    for label, n in counts.items():
        print(f"  {label:<12} {n}")


if __name__ == "__main__":
    main()
