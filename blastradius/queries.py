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

# Q6 — Have we seen this before? Vector similarity of the incident text, boosted by graph structure:
# same root-cause component and overlap with the components involved now.
SIMILAR_INCIDENTS = """
CALL db.idx.vector.queryNodes('Incident', 'embedding', $k, vecf32($vec)) YIELD node, score
WITH node AS i, 1 - score AS text_sim
WHERE i.id <> $exclude AND i.status = 'resolved'
OPTIONAL MATCH (i)-[:ROOT_CAUSE]->(rc:Component)
OPTIONAL MATCH (i)-[:IMPACTED]->(imp:Component)
WITH i, text_sim, rc, collect(DISTINCT imp.name) AS impacted
WITH i, text_sim, rc, impacted,
     CASE WHEN rc.name IN $components THEN 1.0 ELSE 0.0 END AS same_root,
     size([c IN impacted WHERE c IN $components]) AS overlap
OPTIONAL MATCH (i)-[:RESOLVED_BY]->(a:Action)
WITH i, rc, impacted, text_sim, same_root, overlap, collect(a.detail) AS fixes
RETURN i.id AS incident, i.title AS title, i.severity AS severity, i.opened_at AS opened_at,
       rc.name AS root_cause, impacted, fixes,
       round(text_sim * 1000) / 1000.0 AS text_similarity, same_root = 1.0 AS same_root_cause, overlap,
       round((0.6 * text_sim + 0.3 * same_root + 0.1 * toFloat(overlap) / $n_components) * 1000) / 1000.0 AS score
ORDER BY score DESC
LIMIT $limit
"""

# Q7 — What fixed problems in X before? Past incident fixes + runbooks + lessons learned.
PAST_FIXES = """
MATCH (c:Component {name: $name})
OPTIONAL MATCH (rb:Runbook)-[:FOR]->(c)
WITH c, collect(DISTINCT {id: rb.id, title: rb.title, steps: rb.steps}) AS runbooks
OPTIONAL MATCH (l:Lesson)-[:APPLIES_TO]->(c)
WITH c, runbooks, collect(DISTINCT {id: l.id, text: l.text, learned_from: l.incident}) AS lessons
OPTIONAL MATCH (i:Incident)-[:ROOT_CAUSE]->(c)
OPTIONAL MATCH (i)-[:RESOLVED_BY]->(a:Action)
RETURN runbooks, lessons,
       collect(DISTINCT {incident: i.id, title: i.title, kind: a.kind, fix: a.detail}) AS past_fixes
"""

# Q8 — Which components are most critical? PageRank over DEPENDS_ON: rank flows from dependents to
# their dependencies, so things many paths rely on score highest.
CRITICALITY = """
CALL algo.pageRank('Component', 'DEPENDS_ON') YIELD node, score
RETURN node.name AS component, round(score * 10000) / 10000.0 AS pagerank
ORDER BY pagerank DESC
LIMIT $limit
"""

# Q9 — How does a failure in X reach users? Shortest dependency path from the user entry point.
PATH_TO_USER = """
MATCH (src:Component {name: $entry}), (dst:Component {name: $name})
CALL algo.SPpaths({sourceNode: src, targetNode: dst, relTypes: ['DEPENDS_ON'], maxLen: 8, pathCount: 3})
YIELD path, pathWeight
RETURN [n IN nodes(path) | n.name] AS path, pathWeight AS hops
ORDER BY hops
LIMIT 1
"""

# Q10 — Why is X built this way? Design decisions affecting X, who made them, and the source document.
DECISIONS_FOR = """
MATCH (c:Component {name: $name})<-[:AFFECTS]-(d:Decision)
OPTIONAL MATCH (p:Person)-[:MADE]->(d)
OPTIONAL MATCH (d)-[:RECORDED_IN]->(doc:Document)
RETURN d.id AS decision, d.title AS title, d.status AS status, d.date AS date,
       d.rationale AS rationale, p.handle AS made_by, doc.id AS source
ORDER BY date DESC
"""

# Semantic search over company documents (postmortems, ADRs), with what each document is about.
SEARCH_DOCS = """
CALL db.idx.vector.queryNodes('Document', 'embedding', $k, vecf32($vec)) YIELD node, score
WITH node AS d, 1 - score AS similarity
OPTIONAL MATCH (d)-[:ABOUT]->(c:Component)
RETURN d.id AS document, d.kind AS kind, d.title AS title, d.body AS body,
       round(similarity * 1000) / 1000.0 AS similarity, collect(c.name) AS about
ORDER BY similarity DESC
"""

# Knowledge risk: components that only a few people have ever changed.
BUS_FACTOR = """
MATCH (c:Service)
OPTIONAL MATCH (c)<-[:MODIFIES]-(:PullRequest)<-[:AUTHORED]-(p:Person)
WITH c, count(DISTINCT p) AS contributors, collect(DISTINCT p.handle) AS people
WHERE 0 < contributors AND contributors <= $max_people
OPTIONAL MATCH (t:Team)-[:OWNS]->(c)
RETURN c.name AS component, contributors, people, t.name AS owner_team, c.criticality AS criticality
ORDER BY criticality DESC
"""

# Clusters of components that depend on each other (community detection).
CLUSTERS = """
CALL algo.labelPropagation({nodeLabels: ['Component'], relationshipTypes: ['DEPENDS_ON']})
YIELD node, communityId
RETURN communityId AS cluster, collect(node.name) AS components
ORDER BY size(components) DESC
"""

# Everything an agent needs to know about one component.
COMPONENT_OVERVIEW = f"""
MATCH (c:Component {{name: $name}})
OPTIONAL MATCH (t:Team)-[:OWNS]->(c)
OPTIONAL MATCH (c)-[:DEPENDS_ON]->(dep:Component)
WITH c, t, collect(DISTINCT dep.name) AS depends_on
OPTIONAL MATCH (user:Component)-[:DEPENDS_ON]->(c)
WITH c, t, depends_on, collect(DISTINCT user.name) AS used_by
OPTIONAL MATCH (c)-[:SERVES]->(f:UserFlow)
WITH c, t, depends_on, used_by, collect(DISTINCT f.name) AS flows
OPTIONAL MATCH (i:Incident)-[:ROOT_CAUSE|IMPACTED]->(c)
RETURN c.name AS component, [l IN labels(c) WHERE l <> 'Component'][0] AS kind, c.tier AS tier,
       c.language AS language, c.criticality AS criticality, t.name AS owner_team,
       depends_on, used_by, flows, count(DISTINCT i) AS past_incidents
"""

# What a person owns and knows.
PERSON_PROFILE = """
MATCH (p:Person {handle: $handle})
OPTIONAL MATCH (p)-[:MEMBER_OF]->(t:Team)
OPTIONAL MATCH (t)-[:OWNS]->(owned:Component)
WITH p, t, collect(DISTINCT owned.name) AS team_owns
OPTIONAL MATCH (p)-[:AUTHORED]->(pr:PullRequest)-[:MODIFIES]->(c:Component)
WITH p, t, team_owns, c, count(pr) AS changes
ORDER BY changes DESC
WITH p, t, team_owns, collect({component: c.name, changes: changes})[..6] AS worked_on
OPTIONAL MATCH (i:Incident)-[:HANDLED_BY]->(p)
RETURN p.handle AS person, p.name AS name, p.role AS role, t.name AS team, team_owns, worked_on,
       collect(i.id) AS incidents_handled
"""

COMPONENT_NAMES = "MATCH (c:Component) RETURN c.name AS name ORDER BY name"

HOUR = 3600
DAY = 24 * HOUR
USER_ENTRY = "frontend-proxy"


def _run(tenant, query, params=None):
    return db.rows(db.graph(tenant).ro_query(query, params or {}))


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


def similar_incidents(text: str, components: list[str], tenant: str | None = None,
                      exclude: str = "", limit: int = 3) -> list[dict]:
    from blastradius.embeddings import embed_one
    return _run(tenant, SIMILAR_INCIDENTS, {
        "vec": embed_one(text), "k": limit + 5, "exclude": exclude, "components": components,
        "n_components": float(max(len(components), 1)), "limit": limit,
    })


def past_fixes(name: str, tenant: str | None = None) -> dict:
    rows = _run(tenant, PAST_FIXES, {"name": name})
    if not rows:
        return {"runbooks": [], "lessons": [], "past_fixes": []}
    row = rows[0]
    return {
        "runbooks": [r for r in row["runbooks"] if r.get("id")],
        "lessons": [lesson for lesson in row["lessons"] if lesson.get("id")],
        "past_fixes": [f for f in row["past_fixes"] if f.get("incident")],
    }


def criticality(tenant: str | None = None, limit: int = 10) -> list[dict]:
    return _run(tenant, CRITICALITY, {"limit": limit})


def path_to_user(name: str, tenant: str | None = None, entry: str = USER_ENTRY) -> dict:
    rows = _run(tenant, PATH_TO_USER, {"entry": entry, "name": name})
    return rows[0] if rows else {"path": [], "hops": None}


def decisions_for(name: str, tenant: str | None = None) -> list[dict]:
    return _run(tenant, DECISIONS_FOR, {"name": name})


def search_docs(text: str, tenant: str | None = None, limit: int = 3) -> list[dict]:
    from blastradius.embeddings import embed_one
    return _run(tenant, SEARCH_DOCS, {"vec": embed_one(text), "k": limit})


def bus_factor(tenant: str | None = None, max_people: int = 1) -> list[dict]:
    return _run(tenant, BUS_FACTOR, {"max_people": max_people})


def clusters(tenant: str | None = None) -> list[dict]:
    return _run(tenant, CLUSTERS)


def component_overview(name: str, tenant: str | None = None) -> dict | None:
    rows = _run(tenant, COMPONENT_OVERVIEW, {"name": name})
    return rows[0] if rows else None


def person_profile(handle: str, tenant: str | None = None) -> dict | None:
    rows = _run(tenant, PERSON_PROFILE, {"handle": handle})
    return rows[0] if rows and rows[0]["person"] else None


def component_names(tenant: str | None = None) -> list[str]:
    return [r["name"] for r in _run(tenant, COMPONENT_NAMES)]
