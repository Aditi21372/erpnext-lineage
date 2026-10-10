# Lineage Graph Schema (v3.1) — frappe_docker + erpnext

One artifact: `data/graph.json`. Everything else (tools, agent, render) is a
lens over it. Two repos merged: deployment facts come from `frappe_docker/`
(files referenced unprefixed), data facts from `erpnext/` (files prefixed
`erpnext/`). `meta.repos`, `meta.git_commits`, `meta.erpnext` (stats) record
provenance.

## IDs
`<type>:<name>` — e.g. `service:backend`, `volume:sites`, `doctype:customer`,
`field:customer.email_id`, `role:sales-user`, `report:delivered-items-to-be-billed`,
`secret:MYSQL_ROOT_PASSWORD`, `job:datecron`, `artifact:github.com/frappe/erpnext`.

## Node types (21) and required fields
Every node: `id`, `type`, `name`, `label`, `tier`, `group`, `sensitivity`,
`boundary`, `confidence`, `evidence` [{repo, file, line, snippet}], `files`,
`scenarios`, `props`.

| type | extra props | notes |
|---|---|---|
| service | image, command, restart | compose service |
| image | repo, tags | container image |
| build_stage | dockerfile, stage, base | Containerfile stage |
| volume | kind (named/bind), device, categories | data store; sites/db-data ≈ personal |
| network | external, driver | reachability domain |
| port | host_port, container_port, published | boundary if published |
| env_var | origin, categories | config |
| secret | kind, weak, masked | never store real values |
| file | runtime | runtime artifacts (configs, rendered nginx) |
| job | schedule, command, user | scheduled execution |
| artifact | kind (git_repo/base_image/release) | external source |
| owner | role | maintainers |
| doctype | module, istable, issingle, is_submittable, track_changes, allow_import, external | 529 real / 26 stubs |
| field | fieldtype, options, reqd, unique, fetch_from, globally_searchable, categories | materialized for PII/Link/Table/fetch/Attach/Password |
| role | | from permissions[] |
| report | ref_doctype, report_type, query_type (sql/script), reads_known | declarative reads |
| print_format | doc_type, disabled | display/egress layer |
| code | module | controller/report/api file with a detected flow |
| api | module | `@frappe.whitelist` endpoint (one node per decorator line) |
| source | boundary, endpoints/doctypes | external entry points: API, Data Import |
| sink | boundary, send_sites | egress sink (EMAIL) |

`sensitivity`: none | possible | likely; field categories: contact, identity,
location, credentials, files.
`tier`: 0 external/host, 1 ingress, 2 compute, 3 data services, 4 storage/files.

## Edge types (28)
Every edge: `id`, `type`, `src`, `dst`, `label`, `confidence`, `evidence`, `files`,
`scenarios`, `boundary`, `props`.

| type | meaning | key props |
|---|---|---|
| uses_image, built_from, builds_from, clones | build lineage | |
| depends_on, connects_to, proxies_to | runtime calls | via, port, condition, kind |
| mounts, reads_from, writes_to | storage flow | path, mode, bind (mounts path is part of the edge id) |
| joins_network, exposes, publishes | networking/boundary | |
| configures, uses_secret, writes_config | config | |
| executes, backs_up | scheduled ops | |
| owns | ownership | |
| contains | doctype→field; artifact→doctype | |
| links_to | field→doctype | kind: link/table |
| contains_child | doctype→child doctype | via_field |
| fetch_from | field→doctype | source_field |
| stored_in | doctype→volume:db-data; Attach→sites | inferred |
| permits | role→doctype | read/write/email/export/print… |
| displays | print_format→doctype | |
| emits_email | code→sink:email | count (sendmail call sites) |
| wires | code→doctype via hooks doc_events | event (part of the edge id: via=event) |

## Confidence
`validated` — explicit in config · `parsed` — direct JSON/YAML value ·
`inferred` — derived (stored_in, connects_to, data-flow rules) — always labeled.

## Rules & limits
- Raw secret values never stored; evidence snippets for secrets redacted.
- External DocType stubs flagged `external: true`, evidenced from the referring field.
- 6 unresolvable fetch_from chains are classified in `meta.erpnext`:
  `fetch_from_intra_document` (same-document fetch, e.g. from a Currency field),
  `fetch_from_dynamic_link` (source is a Dynamic Link), `fetch_from_dangling`
  (source field missing in the ERPNext JSON) — their sum equals
  `fetch_from_unresolved`; none are silently dropped.
- hooks doc_events wiring: one `wires` edge per (code file, doctype, event);
  `wires_unresolved` = `wires_file_missing` + `wires_no_code_node`.
- 15 non-DocType JSONs skipped at canonical depth (`skipped_non_doctype`);
  chart-of-accounts templates (209 deeper JSONs) are never globbed.
- Static only. Runtime logs are out of scope.

