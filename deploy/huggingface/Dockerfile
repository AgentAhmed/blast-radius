# Single-container image for Hugging Face Spaces: FalkorDB + API + worker + web UI in one container.
# Build context is the repository root:  docker build -f deploy/huggingface/Dockerfile -t blast-radius-space .
FROM node:20-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM falkordb/falkordb:latest
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    FASTEMBED_CACHE_PATH=/app/models \
    FALKORDB_DATA_PATH=/app/falkordb-data \
    BROWSER=0 \
    SEED_ON_START=1 \
    PORT=7860
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv python install 3.12 && uv sync --frozen --no-dev --no-install-project --python 3.12
ENV PATH="/app/.venv/bin:$PATH"
COPY blastradius ./blastradius
COPY --from=ui /ui/dist ./ui/dist
COPY deploy/huggingface/start.sh /app/start.sh
# Download the embedding model at build time so the Space starts fast.
RUN python -c "from blastradius.embeddings import embed_one; embed_one('warm up')" \
 && mkdir -p /app/falkordb-data && chmod +x /app/start.sh \
 && chown -R 1000:1000 /app
# Spaces run the container as uid 1000.
USER 1000
EXPOSE 7860
ENTRYPOINT []
CMD ["/app/start.sh"]
