"""Indexes for the tenant graph. Lookups by these keys happen on every agent step."""

INDEXES = [
    "CREATE INDEX FOR (c:Component) ON (c.name)",
    "CREATE INDEX FOR (t:Team) ON (t.name)",
    "CREATE INDEX FOR (p:Person) ON (p.handle)",
    "CREATE INDEX FOR (pr:PullRequest) ON (pr.number)",
    "CREATE INDEX FOR (pr:PullRequest) ON (pr.merged_at)",
    "CREATE INDEX FOR (r:Release) ON (r.id)",
    "CREATE INDEX FOR (i:Incident) ON (i.id)",
    "CREATE INDEX FOR (d:Document) ON (d.id)",
    "CREATE INDEX FOR (d:Decision) ON (d.id)",
    "CREATE INDEX FOR (f:UserFlow) ON (f.name)",
]


def create_indexes(g) -> None:
    for stmt in INDEXES:
        try:
            g.query(stmt)
        except Exception as e:  # index already exists
            if "already" not in str(e).lower():
                raise
