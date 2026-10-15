# One image: built web UI + API + background worker.
FROM node:20-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH"
COPY blastradius ./blastradius
COPY --from=ui /ui/dist ./ui/dist
# Download the local embedding model at build time so the container starts fast.
RUN python -c "from blastradius.embeddings import embed_one; embed_one('warm up')"
EXPOSE 8007
CMD ["sh", "-c", "uvicorn blastradius.api:app --host 0.0.0.0 --port ${PORT:-8007}"]
