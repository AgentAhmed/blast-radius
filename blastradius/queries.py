"""Core graph queries. These are the agent's tools; every answer it gives is built from them.

Conventions
- Dependency edges point from the dependent to its dependency:
  (checkout)-[:CALLS]->(payment), (cart)-[:READS_WRITES]->(valkey-cart),
  (checkout)-[:PUBLISHES]->(orders), (accounting)-[:CONSUMES]->(orders)
- Walking dependency edges FORWARD answers "what does X rely on?"  (root cause)
- Walking them BACKWARD answers "who relies on X?"                 (blast radius)
- Times are epoch seconds.
"""
import time

from blastradius import db

DEP = "CALLS|READS_WRITES|PUBLISHES|CONSUMES"
MAX_HOPS = 6

# Q1 — What breaks if X goes down?
BLAST_RADIUS = f"""
MATCH (target:Component {{name: $name}})
MATCH p = (dep:Component)-[:{DEP}*1..{MAX_HOPS}]->(target)
WITH target, dep, min(length(p)) AS hops
OPTIONAL MATCH (dep)-[:SERVES]->(f:UserFlow)
RETURN dep.name AS component, dep.tier AS tier, hops,
       collect(DISTINCT f.name) AS flows
ORDER BY hops, component
"""

# Q1b — User flows hit if X goes down (X's own flows + flows of everything that depends on it)
FLOWS_AT_RISK = f"""
MATCH (target:Component {{name: $name}})
OPTIONAL MATCH (dep:Component)-[:{DEP}*1..{MAX_HOPS}]->(target)
WITH target, collect(DISTINCT dep) + [target] AS affected
UNWIND affected AS c
MATCH (c)-[:SERVES]->(f:UserFlow)
RETURN f.name AS flow, collect(DISTINCT c.name) AS via
ORDER BY flow
"""

# Q2 — Which component is the most likely root cause of a set of alerting components?
# A candidate must be a (transitive) dependency of the alerting components, or one of them.
# Ranking: covers the most alerting components -> is itself alerting -> has no alerting
# dependencies of its own (otherwise it is probably a victim too) -> changed recently.
ROOT_CAUSE_CANDIDATES = f"""
UNWIND $alerting AS alerting_name
MATCH (a:Component {{name: alerting_name}})-[:{DEP}*0..{MAX_HOPS}]->(c:Component)
WITH c, count(DISTINCT a) AS covers
OPTIONAL MATCH (c)-[:{DEP}]->(d:Component)
WHERE d.name IN $alerting
WITH c, covers, count(DISTINCT d) AS alerting_deps
OPTIONAL MATCH (c)<-[:MODIFIES]-(pr:PullRequest)
WHERE pr.merged_at >= $since
WITH c, covers, alerting_deps, count(DISTINCT pr) AS recent_changes
RETURN c.name AS component, covers, c.name IN $alerting AS is_alerting,
       alerting_deps, recent_changes
ORDER BY covers DESC, is_alerting DESC, alerting_deps ASC, recent_changes DESC, component
LIMIT $limit
"""

# Q3 — What changed recently in X or anything X depends on (within N hops)?
RECENT_CHANGES = f"""
MATCH p = (x:Component {{name: $name}})-[:{DEP}*0..2]->(c:Component)
WITH c, min(length(p)) AS hops
MATCH (c)<-[:MODIFIES]-(pr:PullRequest)<-[:AUTHORED]-(author:Person)
WHERE pr.merged_at >= $since
OPTIONAL MATCH (rel:Release)-[:INCLUDES]->(pr)
RETURN c.name AS component, hops, pr.number AS pr, pr.title AS title,
       pr.merged_at AS merged_at, author.handle AS author,
       rel.version AS release, rel.deployed_at AS deployed_at
ORDER BY merged_at DESC
"""

# Q4 — Who do I page for X right now?
WHO_TO_PAGE = """
MATCH (c:Component {name: $name})<-[:OWNS]-(t:Team)
OPTIONAL MATCH (t)<-[oc:ON_CALL_FOR]-(p:Person)
WHERE oc.from <= $now AND oc.to > $now
OPTIONAL MATCH (lead:Person {role: 'lead'})-[:MEMBER_OF]->(t)
RETURN t.name AS team, p.handle AS on_call, p.name AS on_call_name,
       oc.to AS on_call_until, lead.handle AS team_lead
"""

# Q5 — Who actually knows X? People ranked by changes to X, recent work counting double.
EXPERTS = """
MATCH (c:Component {name: $name})<-[:MODIFIES]-(pr:PullRequest)<-[:AUTHORED]-(p:Person)
OPTIONAL MATCH (p)-[:MEMBER_OF]->(t:Team)
WITH p, t, count(pr) AS changes,
     sum(CASE WHEN pr.merged_at >= $recent_since THEN 2 ELSE 1 END) AS score,
     max(pr.merged_at) AS last_change
RETURN p.handle AS person, p.name AS name, t.name AS team, changes, score, last_change
ORDER BY score DESC, last_change DESC
LIMIT $limit
"""

HOUR = 3600
DAY = 24 * HOUR


def _run(tenant, query, params):
    return db.rows(db.graph(tenant).ro_query(query, params))


def blast_radius(name: str, tenant: str | None = None) -> dict:
    return {
        "component": name,
        "dependents": _run(tenant, BLAST_RADIUS, {"name": name}),
        "flows_at_risk": _run(tenant, FLOWS_AT_RISK, {"name": name}),
    }


def root_cause_candidates(alerting: list[str], tenant: str | None = None,
                          window_hours: int = 24, now: int | None = None, limit: int = 5) -> list[dict]:
    since = (now or int(time.time())) - window_hours * HOUR
    return _run(tenant, ROOT_CAUSE_CANDIDATES, {"alerting": alerting, "since": since, "limit": limit})


def recent_changes(name: str, tenant: str | None = None,
                   window_hours: int = 24, now: int | None = None) -> list[dict]:
    since = (now or int(time.time())) - window_hours * HOUR
    return _run(tenant, RECENT_CHANGES, {"name": name, "since": since})


def who_to_page(name: str, tenant: str | None = None, now: int | None = None) -> list[dict]:
    return _run(tenant, WHO_TO_PAGE, {"name": name, "now": now or int(time.time())})


def experts(name: str, tenant: str | None = None, now: int | None = None, limit: int = 5) -> list[dict]:
    recent_since = (now or int(time.time())) - 30 * DAY
    return _run(tenant, EXPERTS, {"name": name, "recent_since": recent_since, "limit": limit})
