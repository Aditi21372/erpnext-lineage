# Lineage pipeline — run `make` for the full flow.
# Works without installing: PYTHONPATH=src + `python -m lineage`.
# Prefers an existing .venv, falls back to python3.
export PYTHONPATH := src
PY ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
LINEAGE := $(PY) -m lineage

.PHONY: all sources install build test audit chat views page render demo query clean

all: build test audit views   ## full pipeline

sources:  ## clone the pinned source repos into sources/ (read-only inputs)
	./scripts/fetch_sources.sh sources

install:  ## editable install with the `lineage` entry point
	$(PY) -m pip install -e .

build:  ## regenerate data/graph.json (~4s, deterministic)
	$(LINEAGE) build

test:  ## spec + vulnerability suite (needs `make build` for one CLI test)
	$(LINEAGE) test

audit:  ## adversarial audit: every evidence line vs the real files
	$(LINEAGE) audit

chat:  ## chat UI on http://127.0.0.1:8765 (graph + grounded agent)
	$(LINEAGE) chat

views:  ## six small HTML pages in views/ (uses the demo graph if present)
	$(LINEAGE) views

render:  ## focused Mermaid export for one node
	$(LINEAGE) render --format mermaid --focus doctype:customer --depth 2 --out data/customer.mmd

demo:  ## generate fictional sample data and apply it for rendering
	$(LINEAGE) dummy generate --out data/dummy_data.json
	$(LINEAGE) dummy apply --data data/dummy_data.json --out data/graph_demo.json

query:  ## example: make query Q=search_pii
	$(LINEAGE) query $(Q)

clean:  ## drop generated artifacts
	rm -rf data views
