# lineage — chat UI + graph views, no host toolchain required.
# Build:  docker compose build
# Run:    docker compose up
# Then:   http://localhost:8765  (chat + /views/ pages)
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# package first (cached layer), then entrypoint
COPY pyproject.toml README.md SCHEMA.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY scripts ./scripts
COPY docker/entrypoint.sh /usr/local/bin/lineage-entrypoint
RUN chmod +x /usr/local/bin/lineage-entrypoint scripts/fetch_sources.sh \
 && mkdir -p /app/data /app/views /app/sources

ENV PYTHONUNBUFFERED=1 \
    PORT=8765

VOLUME ["/app/data", "/app/views", "/app/sources"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=240s \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/meta" >/dev/null || exit 1

ENTRYPOINT ["lineage-entrypoint"]
