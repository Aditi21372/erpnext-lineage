#!/usr/bin/env bash
# First-run bootstrap for the Docker image:
#   1. clone the pinned sources from GitHub (read-only inputs — never vendored)
#   2. build the evidence-backed lineage graph from them
#   3. generate fictional demo data (tooltips/labels) and the six HTML views
#   4. serve the chat UI + views on 0.0.0.0:$PORT
# All state lives in named volumes, so restarts are instant. REBUILD=1 forces
# steps 1-3 again. No hardcoded paths: everything is relative to /app.
set -euo pipefail
cd /app

step=0
say(){ step=$((step + 1)); echo; echo "[$step] $*"; }

if [ "${REBUILD:-}" = "1" ]; then
  say "REBUILD=1 → refreshing pinned sources and artifacts"
  rm -rf sources
  bash ./scripts/fetch_sources.sh sources
  rm -f data/graph.json data/graph_demo.json data/dummy_data.json views/index.html
fi

if [ ! -f data/graph.json ]; then
  say "cloning pinned sources from GitHub (frappe/frappe_docker + frappe/erpnext)"
  bash ./scripts/fetch_sources.sh sources
  say "building the lineage graph (~5 seconds, deterministic)"
  lineage build
fi

if [ ! -f data/dummy_data.json ]; then
  say "generating fictional demo data"
  lineage dummy generate --out data/dummy_data.json
fi
if [ ! -f data/graph_demo.json ]; then
  lineage dummy apply --data data/dummy_data.json --out data/graph_demo.json
fi

if [ ! -f views/index.html ]; then
  say "rendering the six HTML views"
  lineage views || echo "views rendering failed (non-fatal)"
fi

echo
if [ -n "${OPENAI_API_KEY:-}" ]; then
  echo "LLM: ON — model '${OPENAI_MODEL:-<unset>}' via '${OPENAI_BASE_URL:-<default endpoint>}'"
else
  echo "LLM: OFF — the chat answers via the deterministic router."
  echo "       To enable the LLM: add OPENAI_BASE_URL + OPENAI_API_KEY + OPENAI_MODEL"
  echo "       to .env, then run:  docker compose up -d   (recreates the container;"
  echo "       your data volume is kept — no rebuild needed)."
fi
echo
echo "==> Ready:  http://localhost:${PORT:-8765}          (chat + graph)"
echo "==> Views:  http://localhost:${PORT:-8765}/views/   (six focused pages)"
echo
exec lineage chat --host 0.0.0.0 --port "${PORT:-8765}" --no-open
