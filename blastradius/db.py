"""FalkorDB connection. One graph per tenant: org_<tenant>."""
from functools import lru_cache

from falkordb import FalkorDB, Graph

from blastradius import config


@lru_cache(maxsize=1)
def client() -> FalkorDB:
    return FalkorDB(
        host=config.FALKORDB_HOST,
        port=config.FALKORDB_PORT,
        username=config.FALKORDB_USERNAME,
        password=config.FALKORDB_PASSWORD,
    )


def graph_name(tenant: str) -> str:
    return f"org_{tenant}"


def graph(tenant: str | None = None) -> Graph:
    return client().select_graph(graph_name(tenant or config.DEFAULT_TENANT))


def rows(result) -> list[dict]:
    """Turn a FalkorDB result into a list of dicts keyed by column name."""
    cols = [h[1] if isinstance(h, (list, tuple)) else h for h in result.header]
    cols = [c.decode() if isinstance(c, bytes) else c for c in cols]
    return [dict(zip(cols, r)) for r in result.result_set]
