"""Derived graph facts and long-term memory writes.

- DEPENDS_ON: one edge type summarising CALLS / READS_WRITES / PUBLISHES / CONSUMES, so graph
  algorithms (PageRank, shortest path, communities) can run over all dependencies at once.
- criticality: PageRank score stored on each Component.
- embeddings: vectors on Incident and Document nodes for semantic recall.
- SIMILAR_TO: links between incidents, written when an incident is remembered.
"""
from blastradius import db, embeddings

DERIVE_DEPENDS_ON = """
MATCH (a:Component)-[r:CALLS|READS_WRITES|PUBLISHES|CONSUMES]->(b:Component)
WITH a, b, collect(DISTINCT type(r)) AS via
MERGE (a)-[d:DEPENDS_ON]->(b)
SET d.via = via
"""

STORE_CRITICALITY = """
CALL algo.pageRank('Component', 'DEPENDS_ON') YIELD node, score
SET node.criticality = round(score * 10000) / 10000.0
"""

INCIDENTS_TO_EMBED = """
MATCH (i:Incident) WHERE i.embedding IS NULL
OPTIONAL MATCH (i)-[:ROOT_CAUSE|IMPACTED]->(c:Component)
RETURN i.id AS id, i.title AS title, i.summary AS summary, collect(DISTINCT c.name) AS components
"""

DOCS_TO_EMBED = "MATCH (d:Document) WHERE d.embedding IS NULL RETURN d.id AS id, d.title AS title, d.body AS body"

SET_INCIDENT_VECTORS = "UNWIND $rows AS r MATCH (i:Incident {id: r.id}) SET i.embedding = vecf32(r.vec)"
SET_DOC_VECTORS = "UNWIND $rows AS r MATCH (d:Document {id: r.id}) SET d.embedding = vecf32(r.vec)"

LINK_SIMILAR = """
MATCH (i:Incident {id: $id})
CALL db.idx.vector.queryNodes('Incident', 'embedding', $k, i.embedding) YIELD node, score
WITH i, node AS other, 1 - score AS sim
WHERE other <> i AND sim >= $min_sim
MERGE (i)-[s:SIMILAR_TO]->(other)
SET s.score = round(sim * 1000) / 1000.0
RETURN other.id AS id, s.score AS score
"""


def derive(g) -> None:
    g.query(DERIVE_DEPENDS_ON)
    g.query(STORE_CRITICALITY)


def embed_missing(g) -> int:
    incidents = db.rows(g.query(INCIDENTS_TO_EMBED))
    if incidents:
        vecs = embeddings.embed([embeddings.incident_text(r["title"], r["summary"], r["components"]) for r in incidents])
        g.query(SET_INCIDENT_VECTORS, {"rows": [{"id": r["id"], "vec": v} for r, v in zip(incidents, vecs)]})
    docs = db.rows(g.query(DOCS_TO_EMBED))
    if docs:
        vecs = embeddings.embed([f"{d['title']}. {d['body']}" for d in docs])
        g.query(SET_DOC_VECTORS, {"rows": [{"id": d["id"], "vec": v} for d, v in zip(docs, vecs)]})
    return len(incidents) + len(docs)


def link_similar(g, incident_id: str, k: int = 4, min_sim: float = 0.75) -> list[dict]:
    return db.rows(g.query(LINK_SIMILAR, {"id": incident_id, "k": k, "min_sim": min_sim}))
