"""Local text embeddings (no API, no rate limit). Vectors are stored on nodes in FalkorDB.

Model: BAAI/bge-small-en-v1.5 via fastembed (ONNX, CPU), 384 dimensions.
The model (~130 MB) is downloaded once on first use.
"""
from functools import lru_cache

MODEL = "BAAI/bge-small-en-v1.5"
DIM = 384


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding
    return TextEmbedding(MODEL)


def embed(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in _model().embed(texts)]


def embed_one(text: str) -> list[float]:
    return embed([text])[0]


def incident_text(title: str, summary: str = "", components: list[str] | None = None) -> str:
    """What we embed for an incident: symptoms + components, so similar failures land close."""
    parts = [title, summary]
    if components:
        parts.append("components: " + ", ".join(components))
    return ". ".join(p for p in parts if p)
