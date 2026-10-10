"""ERPNext adapter: parse the ERPNext app repository into data-level lineage.

Contributes to the combined graph:
- doctype nodes (529 real + external stubs for frappe-core Link targets)
- field nodes materialized for PII / Link / Table / fetch_from / Attach / Password
- edges: contains, links_to, contains_child, fetch_from, stored_in, permits
- declarative layer: report nodes (reads_from for SQL reports) and
  print_format nodes (displays edges)

Lazy-imported by build.build_graph; asserts nothing, records everything in stats.
"""
from __future__ import annotations

import bisect
import json
import re
from collections import Counter
from pathlib import Path

from .core import Builder, slug

OPTS_PII = {"Email", "Phone", "Mobile", "WhatsApp"}
CONTACT_RE = re.compile(
    r"^(email_id|email|phone|mobile_no|mobile|whatsapp_no|fax|contact_no)$", re.I)
NAME_RE = re.compile(
    r"^(first_name|middle_name|last_name|full_name|customer_name|supplier_name|"
    r"lead_name|contact_person|person_name|party_name)$", re.I)
GOV_ID_RE = re.compile(
    r"^(aadhaar_no|pan_no|passport_no|national_id|ssn|tax_id|gstin|dob|"
    r"date_of_birth|birth_date)$", re.I)
LOC_RE = re.compile(
    r"^(address_line1|address_line2|city|state|pincode|postal_code)$", re.I)
LINK_TYPES = {"Link", "Table", "Table MultiSelect"}
TABLE_TYPES = {"Table", "Table MultiSelect"}
PERM_FLAGS = ("read", "write", "create", "delete", "submit", "cancel", "amend",
              "email", "export", "import", "print", "report", "share", "select")
SQL_TABLE_RE = re.compile(r"tab([A-Z][A-Za-z0-9 ]*?)`")
SQL_FIELD_RE = re.compile(r"`tab([A-Z][A-Za-z0-9 ]*?)`\.`([a-z][a-z0-9_]*)`")
SCRIPT_FIELD_RE = re.compile(r'["\']fieldname["\']\s*:\s*["\']([a-z][a-z0-9_]*)["\']')
NON_FIELD_ATTRS = {"meta", "docstatus", "flags", "get_formatted",
                   "get_formatted_value", "__islocal", "get_value"}

# ---- controller-flow scan (L2, regex-level, everything marked inferred)
GET_RE = re.compile(r"frappe\.(?:db\.)?get_(doc|all|list|value)\(\s*[\"']([^\"']+)[\"']")
GET_LIST_RE = re.compile(r"frappe\.(?:db\.)?get_(?:all|list)\(\s*[\"']([^\"']+)[\"']")
SET_RE = re.compile(r"frappe\.db\.set_value\(\s*[\"']([^\"']+)[\"']")
DBSET_RE = re.compile(r"\.db_set\(\s*[\"']([a-z][a-z0-9_]*)[\"']")
NEWDOC_RE = re.compile(r"frappe\.new_doc\(\s*[\"']([^\"']+)[\"']")
SQL_TAB_CODE_RE = re.compile(r"tab([A-Z][A-Za-z0-9 ]*?)(?:`|\")")
FIELD_STR_RE = re.compile(r"[\"']([a-z][a-z0-9_]*)[\"']")
SENDMAIL_RE = re.compile(r"frappe\.sendmail\(")
WHITELIST_RE = re.compile(r"@frappe\.whitelist")
FIELDS_LIST_RE = re.compile(r"fields\s*=\s*\[([^\]]*)\]")


def classify_field(fn: str, fieldtype: str, options) -> tuple[str, list[str]]:
    cats: list[str] = []
    sens = "none"
    fn = fn or ""
    if (isinstance(options, str) and options in OPTS_PII) or CONTACT_RE.match(fn):
        cats.append("contact")
        sens = "likely"
    if NAME_RE.match(fn) or GOV_ID_RE.match(fn):
        cats.append("identity")
        sens = "likely"
    if LOC_RE.match(fn):
        cats.append("location")
        if sens == "none":
            sens = "possible"
    if fieldtype == "Password":
        cats.append("credentials")
        sens = "likely"
    if fieldtype in ("Attach", "Attach Image"):
        cats.append("files")
        sens = "likely"
    return sens, cats


def _ensure_stub(b: Builder, target_slug: str, target_name: str, ev: dict,
                 rel: str, stubs: set) -> str:
    tid = f"doctype:{target_slug}"
    if tid not in b.nodes:
        b.node(tid, "doctype", name=target_name, group="external", external=True)
        b.attach(tid, ev, rel, boundary=True)
        stubs.add(target_slug)
    return tid


def _scoped_options_ev(b: Builder, rel: str, fn: str, options: str) -> dict:
    """Evidence line for `fn`'s own options value.

    Builder.ev() returns the first line matching the needle, which is the
    wrong field when several fields in one JSON share the same target
    (e.g. job-card.time_logs and job-card.employee -> Job Card Time Log).
    """
    lines = b.lines(rel)
    fn_needle = f'"fieldname": "{fn}"'
    start = None
    for i, ln in enumerate(lines):
        if fn_needle in ln:
            start = i
            break
    if start is not None:
        opt_needle = f'"options": "{options}"'
        for j in range(start + 1, min(start + 60, len(lines))):
            if '"fieldname"' in lines[j]:
                break
            if opt_needle in lines[j]:
                return {"file": rel, "line": j + 1, "snippet": lines[j].strip()[:160]}
    return b.ev(rel, f'"options": "{options}"')


def _scan_code_flow(b: Builder, erp_root: Path, docs: dict, stats) -> None:
    """L2: scan controllers/reports/api for reads/writes/emails (all inferred)."""
    name_to_slug = {d["name"]: s for s, (d, _) in docs.items()}
    sink_ev = None
    api_ev = None
    api_edges: dict[tuple, dict] = {}

    for p in sorted(erp_root.glob("erpnext/**/*.py")):
        rel = p.relative_to(erp_root).as_posix()
        if (p.name.endswith("_test.py") or "/tests/" in rel
                or p.name == "__init__.py"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "frappe." not in text:
            continue
        starts = [0] + [m.end() for m in re.finditer("\n", text)]
        state = {"first": None}

        def ev_at(pos: int) -> dict:
            ln = bisect.bisect_right(starts, pos)
            end = starts[ln] if ln < len(starts) else len(text)
            ev = {"file": rel, "line": ln,
                  "snippet": text[starts[ln - 1]:end].strip()[:160]}
            if state["first"] is None:
                state["first"] = ev
            return ev

        own_doc = None
        if "/doctype/" in rel:
            parts = rel.split("/")
            i = parts.index("doctype")
            if i + 1 < len(parts) and parts[i + 1] in docs:
                own_doc = parts[i + 1]

        reads: dict[str, dict] = {}
        writes: dict[str, dict] = {}
        field_reads: dict[str, dict] = {}
        field_writes: dict[str, dict] = {}
        email_ev = None
        emails = 0

        for m in GET_RE.finditer(text):
            s = name_to_slug.get(m.group(2))
            if s:
                rec = reads.setdefault(s, {"count": 0, "ev": ev_at(m.start())})
                rec["count"] += 1
            else:
                stats["unmatched_doctype_refs"] += 1
        for m in SET_RE.finditer(text):
            s = name_to_slug.get(m.group(1))
            if s:
                writes.setdefault(s, {"count": 0,
                                      "ev": ev_at(m.start())})["count"] += 1
            else:
                stats["unmatched_doctype_refs"] += 1
        for m in NEWDOC_RE.finditer(text):
            s = name_to_slug.get(m.group(1))
            if s:
                writes.setdefault(s, {"count": 0,
                                      "ev": ev_at(m.start())})["count"] += 1
            else:
                stats["unmatched_doctype_refs"] += 1
        if own_doc:
            for m in DBSET_RE.finditer(text):
                key = f"{own_doc}.{m.group(1)}"
                if f"field:{key}" in b.nodes:
                    field_writes.setdefault(key, ev_at(m.start()))
        for gm in GET_LIST_RE.finditer(text):
            s = name_to_slug.get(gm.group(1))
            if not s:
                continue
            window = text[gm.end(): gm.end() + 400]
            fl = FIELDS_LIST_RE.search(window)
            if fl:
                base = gm.end() + fl.start()
                for fm in FIELD_STR_RE.finditer(fl.group(1)):
                    fn = fm.group(1)
                    if f"field:{s}.{fn}" in b.nodes:
                        field_reads.setdefault(f"{s}.{fn}",
                                               ev_at(base + fm.start()))
        for m in SQL_TAB_CODE_RE.finditer(text):
            s = name_to_slug.get(m.group(1).strip())
            if s:
                rec = reads.setdefault(s, {"count": 0, "ev": ev_at(m.start())})
                rec["count"] += 1
        for m in SENDMAIL_RE.finditer(text):
            emails += 1
            if email_ev is None:
                email_ev = ev_at(m.start())
                if sink_ev is None:
                    sink_ev = email_ev

        api_ids = []
        for wm in WHITELIST_RE.finditer(text):
            body = text[wm.end(): wm.end() + 2500]
            fmm = re.search(r"def\s+([a-z_][a-z0-9_]*)\s*\(", body)
            func = fmm.group(1) if fmm else "endpoint"
            ln = bisect.bisect_right(starts, wm.start())
            aid = f"api:{rel}:{ln}"
            b.node(aid, "api", name=f"{p.stem}.{func}", group="external",
                   module=rel.split("/")[1] if "/" in rel else None,
                   boundary=True)
            b.attach(aid, ev_at(wm.start()), rel, boundary=True)
            api_ids.append(aid)
            stats["api_endpoints"] += 1
            if api_ev is None:
                api_ev = ev_at(wm.start())
            for am in GET_RE.finditer(body):
                s = name_to_slug.get(am.group(2))
                if s:
                    b.edge("reads_from", aid, f"doctype:{s}",
                           ev_at(wm.end() + am.start()), confidence="inferred",
                           label=f"api get_{am.group(1)}")
                    rec = api_edges.setdefault(("reads_from", s),
                                               {"count": 0, "ev": api_ev})
                    rec["count"] += 1
            for rx, kind in ((SET_RE, "set_value"), (NEWDOC_RE, "new_doc")):
                for am in rx.finditer(body):
                    s = name_to_slug.get(am.group(1))
                    if s:
                        b.edge("writes_to", aid, f"doctype:{s}",
                               ev_at(wm.end() + am.start()),
                               confidence="inferred", label=f"api {kind}")
                        rec = api_edges.setdefault(("writes_to", s),
                                                   {"count": 0, "ev": api_ev})
                        rec["count"] += 1

        if not (reads or writes or field_reads or field_writes or emails
                or api_ids):
            continue

        cid = f"code:{rel}"
        b.node(cid, "code", name=rel, group="compute",
               module=rel.split("/")[1] if "/" in rel else None)
        b.attach(cid, state["first"], rel, confidence="inferred")
        stats["code_files"] += 1
        for s, rec in reads.items():
            b.edge("reads_from", cid, f"doctype:{s}", rec["ev"],
                   confidence="inferred", label=f"reads x{rec['count']}",
                   count=rec["count"])
            stats["code_read_edges"] += 1
        for s, rec in writes.items():
            b.edge("writes_to", cid, f"doctype:{s}", rec["ev"],
                   confidence="inferred", label=f"writes x{rec['count']}",
                   count=rec["count"])
            stats["code_write_edges"] += 1
        for key, ev in field_reads.items():
            s, fn = key.split(".", 1)
            b.edge("reads_from", cid, f"field:{key}", ev, confidence="inferred",
                   label=f"reads {fn}")
            stats["code_field_edges"] += 1
        for key, ev in field_writes.items():
            s, fn = key.split(".", 1)
            b.edge("writes_to", cid, f"field:{key}", ev, confidence="inferred",
                   label=f"writes {fn}")
            stats["code_field_edges"] += 1
        if emails:
            b.edge("emits_email", cid, "sink:email", email_ev,
                   confidence="inferred", label=f"sends x{emails}", count=emails)
            stats["email_edges"] += 1

    # hubs: API sources, import sources, email sink
    if sink_ev:
        b.node("sink:email", "sink", name="EMAIL (sink)", group="ops",
               boundary=True, send_sites=stats["email_edges"])
        b.attach("sink:email", sink_ev, sink_ev["file"], boundary=True)
    b.node("source:api", "source", name="API (endpoints)", group="external",
           boundary=True, endpoints=stats["api_endpoints"])
    if api_ev:
        b.attach("source:api", api_ev, api_ev["file"], boundary=True)
    for (kind, s_slug), rec in sorted(api_edges.items()):
        b.edge(kind, "source:api", f"doctype:{s_slug}", rec["ev"],
               confidence="inferred",
               label=f"API {kind} x{rec['count']}", count=rec["count"])
        stats["api_edges"] += 1
    import_ev = None
    n_imports = 0
    for s, (d, rel) in sorted(docs.items()):
        if d.get("allow_import"):
            ev = b.ev(rel, f'"name": "{d["name"]}"')
            if import_ev is None:
                import_ev = ev
            b.edge("writes_to", "source:import", f"doctype:{s}", ev,
                   confidence="inferred", label="data import")
            n_imports += 1
    b.node("source:import", "source", name="Data Import (sources)",
           group="external", boundary=True, doctypes=n_imports)
    if import_ev:
        b.attach("source:import", import_ev, import_ev["file"], boundary=True)
    stats["import_edges"] = n_imports

    # hooks.py doc_events wiring: code -> doctype
    hooks_rel = "erpnext/hooks.py"
    if (erp_root / hooks_rel).exists():
        htext = b.text(hooks_rel)
        sec = re.search(r"doc_events\s*=\s*\{(.*?)\n\}", htext, re.S)
        if sec:
            for dm in re.finditer(r'"([A-Z][^"]+)":\s*\{([^}]*)\}', sec.group(1)):
                docname, inner = dm.group(1), dm.group(2)
                s = name_to_slug.get(docname)
                tid = f"doctype:{s}" if s else f"doctype:{slug(docname)}"
                if tid not in b.nodes:
                    # frappe-core target (Communication, User, ...) -> stub
                    b.node(tid, "doctype", name=docname, group="external",
                           external=True)
                    b.attach(tid, b.ev(hooks_rel, f'"{docname}"'), hooks_rel,
                             boundary=True)
                    stats["external_stubs"] += 1
                for em in re.finditer(
                        r'"([a-z_]+)":\s*(?:"([\w.]+)"|\[([^\]]*)\])', inner):
                    event = em.group(1)
                    paths = ([em.group(2)] if em.group(2)
                             else re.findall(r'"([\w.]+)"', em.group(3) or ""))
                    for path in paths:
                        parts = path.split(".")
                        file_rel = None
                        for cut in range(len(parts) - 1, 2, -1):
                            cand = "/".join(parts[:cut]) + ".py"
                            if (erp_root / cand).exists():
                                file_rel = cand
                                break
                        cid = f"code:{file_rel}" if file_rel else None
                        if cid and cid in b.nodes:
                            # the event is part of the edge identity: one file
                            # can handle several events for the same doctype
                            b.edge("wires", cid, tid,
                                   b.ev(hooks_rel, path), confidence="inferred",
                                   label=event, via=event)
                        else:
                            if file_rel is None:
                                stats["wires_file_missing"] += 1
                            else:
                                stats["wires_no_code_node"] += 1
                            stats["wires_unresolved"] += 1
    stats["wire_edges"] = sum(1 for e in b.edges.values() if e["type"] == "wires")


def build(erp_root: Path) -> dict:
    erp_root = Path(erp_root)
    b = Builder(erp_root)
    stats = Counter()
    for k in ("doctypes", "child_tables", "fields_total", "fields_materialized",
              "pii_fields", "external_stubs", "roles", "permit_pairs",
              "fetch_from_total", "fetch_from_resolved", "fetch_from_unresolved",
              "fetch_from_intra_document", "fetch_from_dynamic_link",
              "fetch_from_dangling",
              "skipped_parse_error", "skipped_non_doctype", "slug_collisions",
              "reports", "sql_reports", "sql_table_unresolved", "print_formats",
              "print_format_doctype_unresolved", "skipped_report_json",
              "skipped_print_format_json",
              "fetch_from_field_precise", "print_format_field_displays",
              "script_report_field_reads", "sql_report_field_reads",
              "code_files", "code_read_edges", "code_write_edges",
              "code_field_edges", "api_endpoints", "api_edges", "email_edges",
              "wire_edges", "wires_unresolved", "wires_file_missing",
              "wires_no_code_node", "unmatched_doctype_refs"):
        stats[k] = 0
    docs: dict[str, tuple[dict, str]] = {}

    # pass 1: collect doctype definitions (canonical depth only)
    for p in sorted(erp_root.glob("erpnext/**/doctype/*/*.json")):
        rel = p.relative_to(erp_root).as_posix()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            stats["skipped_parse_error"] += 1
            continue
        if not (isinstance(d, dict) and d.get("name")
                and isinstance(d.get("fields"), list)):
            stats["skipped_non_doctype"] += 1
            continue
        s = slug(d["name"])
        if s in docs:
            stats["slug_collisions"] += 1
            continue
        docs[s] = (d, rel)

    # pass 2: doctype nodes
    for s, (d, rel) in docs.items():
        did = f"doctype:{s}"
        b.node(did, "doctype", name=d["name"], group="data",
               module=d.get("module"), istable=bool(d.get("istable")),
               issingle=bool(d.get("issingle")),
               is_submittable=bool(d.get("is_submittable")),
               track_changes=bool(d.get("track_changes")),
               allow_import=bool(d.get("allow_import")),
               title_field=d.get("title_field"))
        b.attach(did, b.ev(rel, f'"name": "{d["name"]}"'), rel)
        stats["doctypes"] += 1
        if d.get("istable"):
            stats["child_tables"] += 1

    # pass 3: fields, links, children, fetch_from, stored_in
    stubs: set[str] = set()
    fetch_specs: list[tuple] = []
    for s, (d, rel) in docs.items():
        did = f"doctype:{s}"
        by_fn = {f.get("fieldname"): f for f in d["fields"] if f.get("fieldname")}
        for f in d["fields"]:
            fn = f.get("fieldname")
            stats["fields_total"] += 1
            if not fn:
                continue
            t = f.get("fieldtype", "")
            o = f.get("options")
            sens, cats = classify_field(fn, t, o)
            is_link = t in LINK_TYPES
            is_dyn = t == "Dynamic Link"
            is_fetch = bool(f.get("fetch_from"))
            if not (is_link or is_dyn or is_fetch or cats):
                continue
            fid = f"field:{s}.{fn}"
            f_ev = b.ev(rel, f'"fieldname": "{fn}"')
            b.node(fid, "field", name=f"{s}.{fn}", group="data",
                   fieldtype=t, options=o if isinstance(o, str) else None,
                   label=f.get("label"), reqd=bool(f.get("reqd")),
                   unique=bool(f.get("unique")), fetch_from=f.get("fetch_from"),
                   globally_searchable=bool(f.get("in_global_search")),
                   categories=cats)
            b.attach(fid, f_ev, rel, sensitivity=sens)
            b.edge("contains", did, fid, f_ev, label=fn)
            stats["fields_materialized"] += 1
            if cats:
                stats["pii_fields"] += 1
            if is_dyn:
                continue  # options is a fieldname, not a doctype target
            if is_link and isinstance(o, str):
                # note: options can be "Dynamic Link" — a real frappe-core
                # child doctype — so it becomes an external stub like any other.
                ts = slug(o)
                t_ev = _scoped_options_ev(b, rel, fn, o)
                if ts in docs:
                    tid = f"doctype:{ts}"
                else:
                    tid = _ensure_stub(b, ts, o, t_ev, rel, stubs)
                kind = "table" if t in TABLE_TYPES else "link"
                b.edge("links_to", fid, tid, t_ev, label=o, kind=kind)
                if t in TABLE_TYPES:
                    b.edge("contains_child", did, tid, t_ev, label=fn, via=fn,
                           via_field=fn)
            if is_fetch:
                stats["fetch_from_total"] += 1
                src_fn = str(f["fetch_from"]).split(".")[0]
                sf = by_fn.get(src_fn)
                if (sf and isinstance(sf.get("options"), str)
                        and sf.get("fieldtype") in LINK_TYPES):
                    ts = slug(sf["options"])
                    if ts in docs:
                        tid = f"doctype:{ts}"
                    else:
                        sf_ev = _scoped_options_ev(b, rel, src_fn, sf["options"])
                        tid = _ensure_stub(b, ts, sf["options"], sf_ev, rel, stubs)
                    source_field = (str(f["fetch_from"]).split(".", 1)[1]
                                    if "." in str(f["fetch_from"]) else None)
                    # defer: fields of the target doctype may not exist yet
                    fetch_specs.append((fid, ts, source_field, f_ev,
                                        f["fetch_from"], tid))
                    stats["fetch_from_resolved"] += 1
                else:
                    # classify why it cannot become an edge (never silently drop)
                    if sf is None:
                        stats["fetch_from_dangling"] += 1
                    elif sf.get("fieldtype") == "Dynamic Link":
                        stats["fetch_from_dynamic_link"] += 1
                    else:
                        stats["fetch_from_intra_document"] += 1
                    stats["fetch_from_unresolved"] += 1
            if t in ("Attach", "Attach Image"):
                b.edge("stored_in", fid, "volume:sites", f_ev,
                       confidence="inferred", label="file in sites volume")
        b.edge("stored_in", did, "volume:db-data",
               b.ev(rel, f'"name": "{d["name"]}"'), confidence="inferred",
               label="MariaDB table")
    # resolve deferred fetch_from edges now that all fields exist
    for fid, ts, source_field, f_ev, ff_label, fallback_tid in fetch_specs:
        target_field = f"field:{ts}.{source_field}" if source_field else None
        if target_field and target_field in b.nodes:
            b.edge("fetch_from", fid, target_field, f_ev, label=ff_label,
                   source_field=source_field, resolution="field")
            stats["fetch_from_field_precise"] += 1
        else:
            b.edge("fetch_from", fid, fallback_tid, f_ev, label=ff_label,
                   source_field=source_field, resolution="doctype")
    stats["external_stubs"] = len(stubs)

    # pass 4: roles + permissions
    role_ids: set[str] = set()
    for s, (d, rel) in docs.items():
        did = f"doctype:{s}"
        merged: dict[str, dict] = {}
        for pr in d.get("permissions", []) or []:
            r = pr.get("role")
            if not r:
                continue
            flags = merged.setdefault(str(r), {})
            for k in PERM_FLAGS:
                if pr.get(k):
                    flags[k] = True
        for r, flags in merged.items():
            rid = f"role:{slug(r)}"
            r_ev = b.ev(rel, f'"role": "{r}"')
            b.node(rid, "role", name=r, group="ops")
            b.attach(rid, r_ev, rel)
            b.edge("permits", rid, did, r_ev, label=r, **flags)
            role_ids.add(rid)
            stats["permit_pairs"] += 1
    stats["roles"] = len(role_ids)

    # pass 5: artifact provenance + contains
    # evidence files must follow the "erpnext/" prefix convention (repo-root
    # files like pyproject.toml are ambiguous in the merged graph)
    ev_file, ev = "erpnext/hooks.py", None
    for cand, needle in (("erpnext/hooks.py", "erpnext"),
                         ("erpnext/modules.txt", "Accounts")):
        if (erp_root / cand).exists():
            candidate = b.ev(cand, needle)
            if candidate["line"] > 0:
                ev_file, ev = cand, candidate
                break
    aid = "artifact:github.com/frappe/erpnext"
    b.node(aid, "artifact", name="github.com/frappe/erpnext", group="external",
           kind="git_repo", boundary=True)
    b.attach(aid, ev, ev_file, boundary=True)
    for s in docs:
        b.edge("contains", aid, f"doctype:{s}", ev, confidence="inferred",
               label="ships doctype")

    # pass 6 (declarative layer): reports + print formats
    report_ids: set[str] = set()
    for p in sorted(erp_root.glob("erpnext/**/report/*/*.json")):
        rel = p.relative_to(erp_root).as_posix()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            stats["skipped_report_json"] += 1
            continue
        if not (isinstance(d, dict) and (d.get("report_name") or d.get("name"))):
            stats["skipped_report_json"] += 1
            continue
        name = d.get("report_name") or d["name"]
        rid = f"report:{slug(name)}"
        if rid in report_ids:
            continue
        query = d.get("query")
        needle = (f'"report_name": "{name}"' if d.get("report_name")
                  else f'"name": "{name}"')
        r_ev = b.ev(rel, needle)
        q_ev = b.ev(rel, '"query":')
        if q_ev["line"] == 0:
            q_ev = r_ev
        b.node(rid, "report", name=name, group="ops",
               ref_doctype=d.get("ref_doctype"), report_type=d.get("report_type"),
               query_type=("sql" if query else "script"),
               reads_known=bool(query))
        b.attach(rid, r_ev, rel)
        report_ids.add(rid)
        stats["reports"] += 1
        if query:
            stats["sql_reports"] += 1
            for tm in dict.fromkeys(SQL_TABLE_RE.findall(query)):
                ts = slug(tm.strip())
                if ts in docs:
                    b.edge("reads_from", rid, f"doctype:{ts}", q_ev,
                           confidence="parsed", label=f"SQL: tab{tm.strip()}")
                else:
                    stats["sql_table_unresolved"] += 1
            for fm in SQL_FIELD_RE.finditer(query):
                ts = slug(fm.group(1).strip())
                fid = f"field:{ts}.{fm.group(2)}"
                if fid in b.nodes:
                    b.edge("reads_from", rid, fid, q_ev, confidence="parsed",
                           label=f"SQL: {fm.group(2)}")
                    stats["sql_report_field_reads"] += 1
        else:
            # script report: column definitions live in the sibling .py
            py_path = p.with_suffix(".py")
            if py_path.exists():
                py_rel = py_path.relative_to(erp_root).as_posix()
                py_lines = py_path.read_text(encoding="utf-8",
                                             errors="ignore").splitlines()
                rdt = (slug(d.get("ref_doctype"))
                       if isinstance(d.get("ref_doctype"), str) else None)
                seen_f: set[str] = set()
                for ln, line in enumerate(py_lines, start=1):
                    for fm in SCRIPT_FIELD_RE.finditer(line):
                        fn = fm.group(1)
                        if fn in seen_f or len(seen_f) >= 60:
                            continue
                        seen_f.add(fn)
                        if rdt and f"field:{rdt}.{fn}" in b.nodes:
                            b.edge("reads_from", rid, f"field:{rdt}.{fn}",
                                   {"file": py_rel, "line": ln,
                                    "snippet": line.strip()[:160]},
                                   confidence="inferred", label=f"column {fn}")
                            stats["script_report_field_reads"] += 1

    pf_ids: set[str] = set()
    for p in sorted(erp_root.glob("erpnext/**/print_format/*/*.json")):
        rel = p.relative_to(erp_root).as_posix()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            stats["skipped_print_format_json"] += 1
            continue
        if not (isinstance(d, dict) and d.get("name")):
            stats["skipped_print_format_json"] += 1
            continue
        name = d["name"]
        pfid = f"print_format:{slug(name)}"
        if pfid in pf_ids:
            continue
        pf_ev = b.ev(rel, f'"name": "{name}"')
        b.node(pfid, "print_format", name=name, group="ops",
               doc_type=d.get("doc_type"), disabled=bool(d.get("disabled")))
        b.attach(pfid, pf_ev, rel)
        pf_ids.add(pfid)
        stats["print_formats"] += 1
        dt = d.get("doc_type")
        if isinstance(dt, str) and slug(dt) in docs:
            b.edge("displays", pfid, f"doctype:{slug(dt)}", pf_ev,
                   confidence="parsed", label=f"renders {dt}")
            html_text = d.get("html")
            if isinstance(html_text, str):
                refs = sorted(set(re.findall(r"doc\.([a-z][a-z0-9_]{2,})", html_text))
                              - NON_FIELD_ATTRS)
                for fn in refs[:50]:
                    fid = f"field:{slug(dt)}.{fn}"
                    if fid in b.nodes:
                        b.edge("displays", pfid, fid, pf_ev,
                               confidence="inferred", label=fn)
                        stats["print_format_field_displays"] += 1
        else:
            stats["print_format_doctype_unresolved"] += 1

    # pass 7 (L2): controller-flow / transformation layer (regex-level, inferred)
    _scan_code_flow(b, erp_root, docs, stats)

    return {"nodes": dict(b.nodes), "edges": dict(b.edges), "stats": dict(stats)}
