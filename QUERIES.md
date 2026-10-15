# Queries and graph algorithms

Everything the agents know comes from these queries. The LLM never writes Cypher: it chooses one of these tools
and its arguments, and we run the fixed, parameterised query.

Sources: [blastradius/queries.py](blastradius/queries.py) (agent tools),
[blastradius/memory.py](blastradius/memory.py) (derived facts and memory),
[blastradius/agents/store.py](blastradius/agents/store.py) (checkpoints and handoffs),
[blastradius/incidents.py](blastradius/incidents.py) (alerts and incidents).

**Conventions**
- Dependency edges point from the dependent to its dependency: `(checkout)-[:CALLS]->(payment)`.
  Walk **forward** for root cause and **backward** for blast radius.
- `DEP` = `CALLS|READS_WRITES|PUBLISHES|CONSUMES`. `DEPENDS_ON` is a derived single edge type over all four,
  used by the graph algorithms.
- Times are epoch seconds. Tool results are converted to "2h ago" before the LLM sees them.

| # | Question | Technique | Used by |
|---|---|---|---|
| Q1 | What breaks if X goes down? | variable-length path, backwards | investigator, brain, MCP |
| Q2 | Root cause of these alerts? | multi-hop common-dependency ranking | investigator, MCP |
| Q3 | What changed near X? | 0..2-hop traversal + time filter | investigator, brain, MCP |
| Q4 | Who do I page now? | time-scoped edge | investigator actions, brain, MCP |
| Q5 | Who knows X? | aggregation over paths | brain, MCP |
| Q6 | Seen this before? | **vector index** + structural boost | investigator, brain, MCP |
| Q7 | What fixed it before? | runbooks, lessons, past fixes | investigator, brain, MCP |
| Q8 | Most critical components? | **PageRank** (`algo.pageRank`) | investigator, brain, UI node size |
| Q9 | How does it reach users? | **shortest path** (`algo.SPpaths`) | investigator, brain, evidence UI |
| Q10 | Why is it built this way? | decision → source document | brain, MCP |
| Q11 | Search company documents | **vector index** on documents | brain, MCP |
| Q12 | Knowledge risk (bus factor) | aggregation | brain |
| Q13 | Service clusters | **label propagation** (`algo.labelPropagation`) | analysis |
| M1–M4 | Memory: derive, embed, link similar | write queries + vector search | ingest, scribe |
| A1–A4 | Agent checkpoints and handoffs | atomic claim, step chain | runtime, worker |

---

## Q1 · Blast radius
```cypher
MATCH (target:Component {name: $name})
MATCH p = (dep:Component)-[:CALLS|READS_WRITES|PUBLISHES|CONSUMES*1..6]->(target)
WITH target, dep, min(length(p)) AS hops
OPTIONAL MATCH (dep)-[:SERVES]->(f:UserFlow)
RETURN dep.name AS component, dep.tier AS tier, hops, collect(DISTINCT f.name) AS flows
ORDER BY hops, component
```
The companion query `FLOWS_AT_RISK` maps X and everything that depends on it to the user flows they serve.

## Q2 · Root-cause candidates
A candidate is anything reachable *forward* from the alerting components (0..6 hops, so an alerting component can be
its own candidate). They are ranked by:
1. `covers`: how many alerting components depend on it
2. `is_alerting`: it shows symptoms itself
3. fewest `alerting_deps`: if its own dependencies alert, it is probably a victim too
4. `recent_changes`: PRs merged within the time window
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
Validated against faults whose true cause is known in [tests/test_queries.py](tests/test_queries.py). For example,
alerts on `accounting` and `fraud-detection` only point to the shared Kafka topic `orders`, which never alerts itself.

## Q3 · Recent changes in X and its dependencies
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

## Q4 · Who to page right now
```cypher
MATCH (c:Component {name: $name})<-[:OWNS]-(t:Team)
OPTIONAL MATCH (t)<-[oc:ON_CALL_FOR]-(p:Person)
WHERE oc.from <= $now AND oc.to > $now
OPTIONAL MATCH (lead:Person {role: 'lead'})-[:MEMBER_OF]->(t)
RETURN t.name AS team, p.handle AS on_call, p.name AS on_call_name,
       oc.to AS on_call_until, lead.handle AS team_lead
```

## Q5 · Who actually knows X
Changes from the last 30 days count double.
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

## Q6 · Similar past incidents (vector + structure)
The score is 60% text similarity (local `bge-small` embeddings in a FalkorDB vector index), 30% same root-cause
component, and 10% overlap with the components involved now. FalkorDB returns cosine *distance*, so similarity is
`1 - score`.
```cypher
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
RETURN i.id AS incident, i.title AS title, rc.name AS root_cause, impacted, fixes,
       round((0.6 * text_sim + 0.3 * same_root + 0.1 * toFloat(overlap) / $n_components) * 1000) / 1000.0 AS score
ORDER BY score DESC
LIMIT $limit
```

## Q7 · Past fixes, runbooks and lessons for X
```cypher
MATCH (c:Component {name: $name})
OPTIONAL MATCH (rb:Runbook)-[:FOR]->(c)
WITH c, collect(DISTINCT {id: rb.id, title: rb.title, steps: rb.steps}) AS runbooks
OPTIONAL MATCH (l:Lesson)-[:APPLIES_TO]->(c)
WITH c, runbooks, collect(DISTINCT {id: l.id, text: l.text, learned_from: l.incident}) AS lessons
OPTIONAL MATCH (i:Incident)-[:ROOT_CAUSE]->(c)
OPTIONAL MATCH (i)-[:RESOLVED_BY]->(a:Action)
RETURN runbooks, lessons, collect(DISTINCT {incident: i.id, title: i.title, kind: a.kind, fix: a.detail}) AS past_fixes
```

## Q8 · Criticality: PageRank
Rank flows from dependents to their dependencies, so components that many paths rely on score highest. The score
is also stored as `Component.criticality` (and sets node size in the UI).
```cypher
CALL algo.pageRank('Component', 'DEPENDS_ON') YIELD node, score
RETURN node.name AS component, round(score * 10000) / 10000.0 AS pagerank
ORDER BY pagerank DESC LIMIT $limit
```

## Q9 · Shortest path from the user to X
```cypher
MATCH (src:Component {name: $entry}), (dst:Component {name: $name})
CALL algo.SPpaths({sourceNode: src, targetNode: dst, relTypes: ['DEPENDS_ON'], maxLen: 8, pathCount: 3})
YIELD path, pathWeight
RETURN [n IN nodes(path) | n.name] AS path, pathWeight AS hops
ORDER BY hops LIMIT 1
```
The same algorithm draws the red failure paths, from each alerting component to the root cause, in the evidence view
([blastradius/api.py](blastradius/api.py)).

## Q10 · Why is X built this way?
```cypher
MATCH (c:Component {name: $name})<-[:AFFECTS]-(d:Decision)
OPTIONAL MATCH (p:Person)-[:MADE]->(d)
OPTIONAL MATCH (d)-[:RECORDED_IN]->(doc:Document)
RETURN d.id AS decision, d.title AS title, d.status AS status, d.date AS date,
       d.rationale AS rationale, p.handle AS made_by, doc.id AS source
ORDER BY date DESC
```

## Q11 · Semantic document search
```cypher
CALL db.idx.vector.queryNodes('Document', 'embedding', $k, vecf32($vec)) YIELD node, score
WITH node AS d, 1 - score AS similarity
OPTIONAL MATCH (d)-[:ABOUT]->(c:Component)
RETURN d.id AS document, d.kind AS kind, d.title AS title, d.body AS body, similarity, collect(c.name) AS about
ORDER BY similarity DESC
```
A full-text index on `Document(title, body)` is also created, for keyword search.

## Q12 · Bus factor
```cypher
MATCH (c:Service)
OPTIONAL MATCH (c)<-[:MODIFIES]-(:PullRequest)<-[:AUTHORED]-(p:Person)
WITH c, count(DISTINCT p) AS contributors, collect(DISTINCT p.handle) AS people
WHERE 0 < contributors AND contributors <= $max_people
OPTIONAL MATCH (t:Team)-[:OWNS]->(c)
RETURN c.name AS component, contributors, people, t.name AS owner_team, c.criticality AS criticality
ORDER BY criticality DESC
```

## Q13 · Service clusters: label propagation
```cypher
CALL algo.labelPropagation({nodeLabels: ['Component'], relationshipTypes: ['DEPENDS_ON']})
YIELD node, communityId
RETURN communityId AS cluster, collect(node.name) AS components
```

Also used as tools: `COMPONENT_OVERVIEW` (owner, dependencies, dependents, flows, incident count) and
`PERSON_PROFILE` (team, what the person owns, worked on and handled). See [queries.py](blastradius/queries.py).

---

## Memory writes ([memory.py](blastradius/memory.py), [incident_agents.py](blastradius/agents/incident_agents.py))
**M1 · Derive `DEPENDS_ON`**, so algorithms run over all dependency types at once:
```cypher
MATCH (a:Component)-[r:CALLS|READS_WRITES|PUBLISHES|CONSUMES]->(b:Component)
WITH a, b, collect(DISTINCT type(r)) AS via
MERGE (a)-[d:DEPENDS_ON]->(b) SET d.via = via
```
**M2 · Store PageRank:** `CALL algo.pageRank('Component','DEPENDS_ON') YIELD node, score SET node.criticality = ...`

**M3 · Link similar incidents** after an incident is remembered:
```cypher
MATCH (i:Incident {id: $id})
CALL db.idx.vector.queryNodes('Incident', 'embedding', $k, i.embedding) YIELD node, score
WITH i, node AS other, 1 - score AS sim
WHERE other <> i AND sim >= $min_sim
MERGE (i)-[s:SIMILAR_TO]->(other) SET s.score = round(sim * 1000) / 1000.0
```
**M4 · Scribe resolution:** one query sets `ROOT_CAUSE`, the `RESOLVED_BY` action, a postmortem `Document` with its
embedding, a `Lesson` that `APPLIES_TO` the component, and the incident embedding (`RESOLVE` in
incident_agents.py).

## Agent state ([agents/store.py](blastradius/agents/store.py))
**A1 · Atomic task claim.** It is one query, and FalkorDB runs writes to a graph one at a time, so two workers can
never claim the same task:
```cypher
MATCH (t:Task {status: 'open'}) WHERE t.type IN $types
WITH t ORDER BY t.created_at LIMIT 1
SET t.status = 'claimed', t.claimed_by = $worker, t.claimed_at = $now
RETURN t.id AS id, t.type AS type, t.incident AS incident, t.payload AS payload
```
**A2 · Checkpoint a step.** It creates the `Step`, links it to the run and chains it to the previous step with `NEXT`
(`ADD_STEP`).

**A3 · Provenance.** `(Step)-[:TOUCHED]->(entity)` for every entity in a tool's arguments and result, and
`(Finding)-[:SUPPORTED_BY]->(entity)` for every entity the finding cites.

**A4 · Resume.** Paused runs whose time has come:
```cypher
MATCH (r:AgentRun) WHERE r.status = 'paused' AND r.resume_at <= $now
RETURN r.id AS id, r.agent AS agent ORDER BY r.resume_at
```
