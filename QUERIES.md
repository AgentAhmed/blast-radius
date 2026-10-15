# Queries and graph algorithms

Every agent answer is built from these queries. Source: [blastradius/queries.py](blastradius/queries.py).
`DEP` = `CALLS|READS_WRITES|PUBLISHES|CONSUMES`. Dependency edges point dependent → dependency.

## Q1 · Blast radius: what breaks if X goes down?
Walk dependency edges **backwards** from X, up to 6 hops; keep the shortest distance per component.
```cypher
MATCH (target:Component {name: $name})
MATCH p = (dep:Component)-[:CALLS|READS_WRITES|PUBLISHES|CONSUMES*1..6]->(target)
WITH target, dep, min(length(p)) AS hops
OPTIONAL MATCH (dep)-[:SERVES]->(f:UserFlow)
RETURN dep.name AS component, dep.tier AS tier, hops, collect(DISTINCT f.name) AS flows
ORDER BY hops, component
```
Companion query `FLOWS_AT_RISK` maps the affected components to the user flows they serve.

## Q2 · Root-cause candidates for a set of alerting components
A candidate is any component reachable *forward* from the alerting ones (0..6 hops, so an alerting
component can be its own candidate). Ranking:
1. `covers`: how many alerting components depend on it (a shared cause explains more symptoms)
2. `is_alerting`: it shows symptoms itself
3. `alerting_deps`: fewer alerting dependencies, so it is less likely to be a victim
4. `recent_changes`: PRs merged within the window
```cypher
UNWIND $alerting AS alerting_name
MATCH (a:Component {name: alerting_name})-[:CALLS|READS_WRITES|PUBLISHES|CONSUMES*0..6]->(c:Component)
WITH c, count(DISTINCT a) AS covers
OPTIONAL MATCH (c)-[:CALLS|READS_WRITES|PUBLISHES|CONSUMES]->(d:Component)
WHERE d.name IN $alerting
WITH c, covers, count(DISTINCT d) AS alerting_deps
OPTIONAL MATCH (c)<-[:MODIFIES]-(pr:PullRequest)
WHERE pr.merged_at >= $since
WITH c, covers, alerting_deps, count(DISTINCT pr) AS recent_changes
RETURN c.name AS component, covers, c.name IN $alerting AS is_alerting, alerting_deps, recent_changes
ORDER BY covers DESC, is_alerting DESC, alerting_deps ASC, recent_changes DESC, component
LIMIT $limit
```
Validated against known faults in [tests/test_queries.py](tests/test_queries.py). For example, alerts only on
`accounting` and `fraud-detection` point to the shared Kafka topic `orders`.

## Q3 · Recent changes in X and its dependencies (≤ 2 hops)
```cypher
MATCH p = (x:Component {name: $name})-[:CALLS|READS_WRITES|PUBLISHES|CONSUMES*0..2]->(c:Component)
WITH c, min(length(p)) AS hops
MATCH (c)<-[:MODIFIES]-(pr:PullRequest)<-[:AUTHORED]-(author:Person)
WHERE pr.merged_at >= $since
OPTIONAL MATCH (rel:Release)-[:INCLUDES]->(pr)
RETURN c.name AS component, hops, pr.number AS pr, pr.title AS title, pr.merged_at AS merged_at,
       author.handle AS author, rel.version AS release, rel.deployed_at AS deployed_at
ORDER BY merged_at DESC
```

## Q4 · Who to page for X right now
Owner team → the person whose `ON_CALL_FOR` window contains `$now`, plus the team lead as an escalation contact.
```cypher
MATCH (c:Component {name: $name})<-[:OWNS]-(t:Team)
OPTIONAL MATCH (t)<-[oc:ON_CALL_FOR]-(p:Person)
WHERE oc.from <= $now AND oc.to > $now
OPTIONAL MATCH (lead:Person {role: 'lead'})-[:MEMBER_OF]->(t)
RETURN t.name AS team, p.handle AS on_call, p.name AS on_call_name,
       oc.to AS on_call_until, lead.handle AS team_lead
```

## Q5 · Who actually knows X
People ranked by changes to X; changes in the last 30 days count double.
```cypher
MATCH (c:Component {name: $name})<-[:MODIFIES]-(pr:PullRequest)<-[:AUTHORED]-(p:Person)
OPTIONAL MATCH (p)-[:MEMBER_OF]->(t:Team)
WITH p, t, count(pr) AS changes,
     sum(CASE WHEN pr.merged_at >= $recent_since THEN 2 ELSE 1 END) AS score,
     max(pr.merged_at) AS last_change
RETURN p.handle AS person, p.name AS name, t.name AS team, changes, score, last_change
ORDER BY score DESC, last_change DESC
LIMIT $limit
```

*Coming next: PageRank criticality, shortest path to user, incident similarity (vector index), decision tracing,
agent-run provenance.*
