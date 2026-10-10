# Roadmap

Ordered by "what a reviewer would ask for next", not by effort.

## P1 — agent quality (the current weakest link)

- **Eval harness**: a golden set of ~30 questions with expected node/edge ids
  and required evidence; score the LLM loop on answer correctness, evidence
  citation rate, and refusal behaviour on unanswerable questions. Run the
  same set through the deterministic router as a baseline.
- **Budgeting**: token accounting, tool-result compaction (aggregate instead
  of truncate), per-question cost/latency reporting.
- **Robustness**: retry with backoff on 429/5xx, streaming, timeout budget,
  and a transcript export for failure triage.

## P2 — flow layer precision

- Replace regex flow scanning with an AST pass (tree-sitter or Python `ast`)
  for `erpnext/**/*.py`: typed reads/writes of `doc.<field>`, dynamic doctype
  names, `frappe.db.sql` string construction, and hook call chains.
- Column-level lineage for the 170 script reports (currently `reads_known=false`
  — honest, but coarse).

## P3 — runtime truth (complement, not replacement)

- **CDC tap** (MariaDB binlog -> Debezium/ClickHouse) to observe actual writes
  the static scan cannot see (raw SQL, server scripts, custom apps).
- Join runtime events to the static graph by `(doctype, docname)`, upgrading
  `inferred` edges to `observed` where they agree.
- Track `Version` doctype rows for field-level change lineage on documents.

## P4 — coverage

- frappe core + HRMS adapters (same contract, new module).
- Custom fields / Server Scripts: they live in the site database, so a
  DB-dump adapter that reads `tabCustom Field` / `tabServer Script` closes
  the biggest "static repo parsing cannot see this" gap.
- Multi-site / multi-app workspace graphs.

## P5 — scale & UX

- Graph store behind the same tool API (Neo4j or DuckDB materialized edges)
  when the artifact outgrows in-memory JSON, keeping SCHEMA.md stable.
- Time-scrubbed views ("as-of" lineage) once runtime events exist.
