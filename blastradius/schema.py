"""Indexes for the tenant graph. Lookups by these keys happen on every agent step."""
from blastradius.embeddings import DIM

INDEXES = [
    "CREATE INDEX FOR (c:Component) ON (c.name)",
    "CREATE INDEX FOR (t:Team) ON (t.name)",
    "CREATE INDEX FOR (p:Person) ON (p.handle)",
    "CREATE INDEX FOR (pr:PullRequest) ON (pr.number)",
    "CREATE INDEX FOR (pr:PullRequest) ON (pr.merged_at)",
    "CREATE INDEX FOR (r:Release) ON (r.id)",
    "CREATE INDEX FOR (i:Incident) ON (i.id)",
    "CREATE INDEX FOR (i:Incident) ON (i.status)",
    "CREATE INDEX FOR (a:Alert) ON (a.id)",
    "CREATE INDEX FOR (a:Alert) ON (a.status)",
    "CREATE INDEX FOR (d:Document) ON (d.id)",
    "CREATE INDEX FOR (d:Decision) ON (d.id)",
    "CREATE INDEX FOR (f:UserFlow) ON (f.name)",
    "CREATE INDEX FOR (t:Task) ON (t.status)",
    "CREATE INDEX FOR (t:Task) ON (t.id)",
    "CREATE INDEX FOR (r:AgentRun) ON (r.id)",
    "CREATE INDEX FOR (r:AgentRun) ON (r.status)",
    "CREATE INDEX FOR (s:Step) ON (s.id)",
    "CREATE INDEX FOR (a:Action) ON (a.id)",
    "CREATE INDEX FOR (l:Lesson) ON (l.id)",
    # Vector indexes: semantic recall of past incidents and documents
    f"CREATE VECTOR INDEX FOR (i:Incident) ON (i.embedding) OPTIONS {{dimension:{DIM}, similarityFunction:'cosine'}}",
    f"CREATE VECTOR INDEX FOR (d:Document) ON (d.embedding) OPTIONS {{dimension:{DIM}, similarityFunction:'cosine'}}",
    # Full-text index: keyword search over company documents
    "CALL db.idx.fulltext.createNodeIndex('Document', 'title', 'body')",
]


def create_indexes(g) -> None:
    for stmt in INDEXES:
        try:
            g.query(stmt)
        except Exception as e:  # index already exists
            if "already" not in str(e).lower():
                raise
