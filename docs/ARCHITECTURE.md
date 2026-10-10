# Architecture

One artifact, many lenses. The graph is built once from two read-only
repositories and everything else (tools, agent, views, renderers) only reads
it — no hidden state, no second source of truth.

```
  frappe_docker checkout ─┐                erpnext checkout ─┐
  (pinned commit)         │                (pinned commit)   │
                          ▼                                 ▼
                 docker_parser.py                    frappe_parser.py
                 compose / Dockerfile /              DocType JSON / Link /
                 bake / env / scenarios              fetch_from / roles /
                          │                          reports / code scan
                          └────────────┬────────────┘
                                       ▼
                                  build.py            merge + scenario tags + meta
                                       ▼
                            data/graph.json          5,554 nodes / 13,819 edges
                    (every node/edge: evidence file:line, confidence,
                     sensitivity, boundary, scenarios)
                                       ▼
        ┌──────────────┬───────────────┼────────────────┬──────────────┐
        ▼              ▼               ▼                ▼              ▼
     tools.py       agent.py        views.py        render.py      viewer.py
     BFS/path/      grounded        six small       Mermaid/       Sigma.js
     taint/PII      tool loop       category views  DOT export     (WebGL) page
                        │                                          + chat.py
                        ▼                                          /api/ask server
                 chat UI sidebar  ──── highlights the answer's nodes
```

## Layers and what is repo-agnostic

| Layer | Modules | Reusability |
|---|---|---|
| Graph model | `core.py` (Builder), `model.py` (types + validation), SCHEMA.md | **Repo-agnostic.** An adapter calls `node()/edge()/attach()`; evidence, confidence merging, dedup are provided. |
| Adapters | `docker_parser.py`, `frappe_parser.py` | **Repo-specific by design.** Each encodes one system's concepts (compose services vs DocTypes). |
| Assembly | `build.py`, `paths.py`, `cli.py` | Generic orchestration; add an adapter call to include another source. |
| Lenses | `tools.py`, `agent.py`, `chat.py`, `views.py`, `render.py`, `viewer.py` | **Mostly repo-agnostic.** Traversal, taint, rendering (Sigma.js WebGL + d3-force), the chat server and the tool loop work on any graph that satisfies SCHEMA.md; `search_pii`/`find_exits` and the agent's system prompt currently encode frappe/ERPNext semantics. |

So: the *pattern* is repo-agnostic, the *instantiation* is repo-specific.
Adding a repository means writing one adapter that emits the documented node
and edge types — the lenses keep working.

## Adapter contract

An adapter is a module with `build(root) -> {"nodes": dict, "edges": dict,
"stats": dict}` that fills a `core.Builder`. Rules that keep the artifact
trustworthy (enforced by `lineage test` and `lineage audit`):

1. Every node and edge carries at least one evidence entry with
   `file`, `line` (> 0) and a snippet that is literally present on that line.
   Secrets are masked (`a***z` / `***`), never stored.
2. Derived facts use `confidence="inferred"`; anything read literally from a
   file is `parsed`; config-declared facts may be `validated`.
3. Nothing is silently dropped: unresolved references are classified and
   counted in `meta` stats (e.g. `fetch_from_dangling`), so "unknown" is
   visible, not guessed.
4. External targets (frappe-core DocTypes referenced by ERPNext) become
   flagged stubs (`external: true`) instead of disappearing.

## Determinism

`lineage build` is deterministic: the two input repos are pinned by commit,
iteration is over sorted paths, and evidence eviction uses a total order
(lexicographic tie-break), so two runs produce byte-identical JSON except
`meta.generated_at`. The regression test for this runs the eviction under
two different `PYTHONHASHSEED` values.

## Verification story

- `tests/test_spec.py` — 92 tests: golden counts, acceptance queries,
  vulnerability regressions (secret redaction, schema validator catching
  ghost edges, evidence locality per field), chat round-trip over a live
  HTTP server, and hash-seed determinism.
- `lineage audit` — adversarial pass over the generated artifact: schema
  integrity, unique ids, every evidence entry checked line-by-line against
  the actual repos, plus a leak scan. Exit code != 0 on any finding.
- CI runs both against freshly fetched pinned sources.
