"""Split the big lineage graph into small, simple category views.

Six views, each answering ONE question with few nodes:

    deployment      frappe_docker runtime (services, volumes, ports, secrets)
    customer-flow   one customer record end-to-end (PII -> transforms -> storage -> exits)
    pii-flow        all PII DocTypes with sources, storage and email exits (aggregated)
    access          roles x PII DocTypes permissions
    exits           everything that crosses the boundary (ports, docker.sock, secrets, emails)
    structure       DocType relationship map + reports/print formats (advanced)

Heavy detail is aggregated: code files collapse into one `transform:<doctype>`
node per doctype; db-data + sites collapse into one `storage:hub` node.

Usage:
    lineage views [--graph data/graph_demo.json] [--outdir views] [--no-mermaid]
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import paths, render, tools, viewer as view

ORDER = ["deployment", "customer-flow", "pii-flow", "access", "exits", "structure"]
PRESETS = {"deployment": "detail", "customer-flow": "detail", "pii-flow": "simple",
           "access": "detail", "exits": "simple", "structure": "detail"}
LR_DEFAULT = {"pii-flow", "customer-flow"}
DEPLOY_TYPES = {"service", "image", "build_stage", "volume", "network", "port",
                "secret", "job", "file", "artifact", "owner"}


def _doc_of_field(fid: str) -> str:
    return "doctype:" + fid.split(":", 1)[1].split(".", 1)[0]


def _ev0(e: dict):
    return e["evidence"][0] if e.get("evidence") else None


def _mk_edge(type_: str, src: str, dst: str, label: str, ev, count=None) -> dict:
    return {"id": f"{type_}:{src}->{dst}", "type": type_, "src": src, "dst": dst,
            "label": label, "confidence": "inferred", "boundary": False,
            "evidence": [ev] if ev else [], "files": [], "scenarios": ["repo"],
            "props": {"count": count} if count else {}}


def _dedupe(edges: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for e in edges:
        cur = out.get(e["id"])
        if cur is None:
            out[e["id"]] = dict(e)
            continue
        n = (cur["props"].get("count") or 1) + (e["props"].get("count") or 1)
        cur["props"]["count"] = n
        if e["evidence"] and len(cur["evidence"]) < 4:
            cur["evidence"].append(e["evidence"][0])
    return list(out.values())


def _hub() -> dict:
    return {"id": "storage:hub", "type": "volume",
            "name": "Storage (MariaDB + files)", "label": "Storage (MariaDB + files)",
            "tier": 4, "group": "storage", "sensitivity": "likely",
            "boundary": False, "confidence": "inferred",
            "evidence": [{"file": "graph.json", "line": 1,
                          "snippet": "aggregate of volume:db-data + volume:sites"}],
            "files": [], "scenarios": ["repo"],
            "props": {"kind": "hub", "categories": ["personal", "files", "backups"]}}


def _transform_flow(graph: dict, scope: set[str]) -> tuple[list[dict], list[dict]]:
    """Collapse code-file reads/writes into one transform node per doctype."""
    tinfo: dict[str, dict] = {}
    edges: list[dict] = []
    emitter_files = {e["src"] for e in graph["edges"] if e["type"] == "emits_email"}
    for e in graph["edges"]:
        if e["type"] not in ("reads_from", "writes_to") or not e["src"].startswith("code:"):
            continue
        dst = e["dst"]
        if dst.startswith("doctype:"):
            s = dst.split(":", 1)[1]
        elif dst.startswith("field:"):
            s = dst.split(":", 1)[1].split(".", 1)[0]
        else:
            continue
        if s not in scope:
            continue
        tid = f"transform:{s}"
        rec = tinfo.setdefault(tid, {"files": set(), "calls": 0, "ev": None})
        rec["files"].add(e["src"])
        rec["calls"] += e["props"].get("count", 1)
        if rec["ev"] is None:
            rec["ev"] = _ev0(e)
        edges.append(_mk_edge(e["type"], tid, dst, e["label"], _ev0(e),
                              e["props"].get("count")))
    nodes = []
    for tid, rec in sorted(tinfo.items()):
        s = tid.split(":", 1)[1]
        nodes.append({"id": tid, "type": "code", "name": f"code: {s}.py",
                      "label": f"code: {s}.py", "tier": 2, "group": "compute",
                      "sensitivity": "none", "boundary": False,
                      "confidence": "inferred",
                      "evidence": [rec["ev"]] if rec["ev"] else [],
                      "files": [], "scenarios": ["repo"],
                      "props": {"files": len(rec["files"]), "calls": rec["calls"]}})
        if any(f in emitter_files for f in rec["files"]):
            edges.append(_mk_edge("emits_email", tid, "sink:email",
                                  f"email x{len(rec['files'])}", rec["ev"]))
    return nodes, _dedupe(edges)


def _cat_deployment(graph: dict) -> dict:
    ids = {n["id"] for n in graph["nodes"] if n["type"] in DEPLOY_TYPES}
    return {"title": "Deployment (frappe_docker)",
            "desc": "Runtime topology: services, volumes, ports, secrets, jobs. "
                    "Where data physically runs and exits.",
            "nodes": [n for n in graph["nodes"] if n["id"] in ids],
            "edges": [e for e in graph["edges"] if e["src"] in ids and e["dst"] in ids]}


def _cat_customer_flow(graph: dict) -> dict:
    ids = {"doctype:customer", "storage:hub"}
    ids |= {n["id"] for n in graph["nodes"] if n["type"] == "field"
            and n["id"].startswith("field:customer.") and n["sensitivity"] != "none"}
    add: list[dict] = []
    for e in graph["edges"]:
        t = e["type"]
        if t == "contains" and e["src"] == "doctype:customer" and e["dst"] in ids:
            add.append(e)
        elif t == "contains_child" and e["src"] == "doctype:customer":
            ids.add(e["dst"])
            add.append(e)
        elif t == "fetch_from" and e["src"] in ids:
            ids.add(e["dst"])
            add.append(e)
        elif (t == "permits" and e["dst"] == "doctype:customer"
              and any(e["props"].get(k) for k in ("export", "email", "print"))):
            ids.add(e["src"])
            add.append(e)
        elif (t == "reads_from" and e["dst"] == "doctype:customer"
              and e["src"].startswith("report:")):
            ids.add(e["src"])
            add.append(e)
        elif t == "displays" and e["dst"] in ids:
            ids.add(e["src"])
            add.append(e)
        elif t == "stored_in" and e["src"] in ids:
            add.append(_mk_edge("stored_in", e["src"], "storage:hub", "storage",
                                _ev0(e)))
        elif (t in ("reads_from", "writes_to") and e["src"] == "source:api"
              and e["dst"] == "doctype:customer"):
            ids.add("source:api")
            add.append(e)
        elif (t in ("reads_from", "writes_to") and e["src"] == "source:import"
              and e["dst"] == "doctype:customer"):
            ids.add("source:import")
            add.append(e)
    tnodes, tedges = _transform_flow(graph, {"customer"})
    ids |= {n["id"] for n in tnodes}
    if any(e["dst"] == "sink:email" for e in tedges):
        ids.add("sink:email")
    tedges = [e for e in tedges if e["dst"] in ids]
    nodes = [n for n in graph["nodes"] if n["id"] in ids] + tnodes
    if "storage:hub" in ids:
        nodes.append(_hub())
    return {"title": "Customer — one record's journey",
            "desc": "PII fields, where values come from (fetch_from), the code that "
                    "reads/writes them, storage, reports, and who can email/export.",
            "nodes": nodes, "edges": _dedupe(add + tedges)}


def _cat_pii_flow(graph: dict) -> dict:
    """One self-describing box per PII DocType. The uniform 'everything rests
    in MariaDB' fact is stated in the caption instead of drawn 88 times."""
    pii_by_doc: dict[str, list[str]] = {}
    for n in graph["nodes"]:
        if n["type"] == "field" and n["sensitivity"] != "none":
            doc = _doc_of_field(n["id"])
            pii_by_doc.setdefault(doc, []).append(n["id"].split(".", 1)[1])
    scope = set(pii_by_doc)
    nodes: list[dict] = []
    for n in graph["nodes"]:
        if n["id"] not in scope:
            continue
        fns = sorted(pii_by_doc[n["id"]])
        nodes.append(dict(n, props={**n["props"], "pii_count": len(fns),
                                    "pii_fields": fns[:12],
                                    "viz_size": 11 + min(2 * len(fns), 13)}))
    edges: list[dict] = []
    hubs: set[str] = set()
    omitted_fetch = 0
    for e in graph["edges"]:
        t, src = e["type"], e["src"]
        if src in ("source:api", "source:import") and e["dst"] in scope:
            hubs.add(src)
            edges.append(e)
        elif t == "fetch_from" and src.startswith("field:"):
            a = _doc_of_field(src)
            b_doc = (_doc_of_field(e["dst"]) if e["dst"].startswith("field:")
                     else e["dst"])
            if a in scope and b_doc in scope:
                if a != b_doc:
                    edges.append(_mk_edge("fetch_from", a, b_doc, e["label"],
                                          _ev0(e)))
            elif a in scope:
                omitted_fetch += 1
    code_targets: dict[str, set[str]] = {}
    for e in graph["edges"]:
        if (e["type"] in ("reads_from", "writes_to") and e["src"].startswith("code:")
                and e["dst"].startswith("doctype:")):
            code_targets.setdefault(e["src"], set()).add(e["dst"])
    for e in graph["edges"]:
        if e["type"] == "emits_email" and e["src"] in code_targets:
            for dst in code_targets[e["src"]]:
                if dst in scope:
                    hubs.add("sink:email")
                    edges.append(_mk_edge("emits_email", dst, "sink:email",
                                          "email", _ev0(e)))
    for hid in sorted(hubs):
        nodes.append(next(x for x in graph["nodes"] if x["id"] == hid))
    return {"title": "PII flow (all DocTypes)",
            "desc": "One box per PII DocType: the number is its PII field count; "
                    "boxes grow with it — hover a box to see the actual fields. "
                    "Arrows: enter (API / Import) → relate (fetch_from) → leave "
                    "(EMAIL). Every row rests in the MariaDB volume db-data; "
                    "attachments live in sites. "
                    f"{omitted_fetch} fetch_from arrows leaving the PII scope "
                    "(e.g. to Contact) are omitted.",
            "nodes": nodes, "edges": _dedupe(edges)}


def _cat_access(graph: dict) -> dict:
    pii_docs = {_doc_of_field(f["id"]) for f in graph["nodes"]
                if f["type"] == "field" and f["sensitivity"] != "none"}
    ids = {n["id"] for n in graph["nodes"] if n["type"] == "role"} | pii_docs
    return {"title": "Access — roles x PII DocTypes",
            "desc": "Which roles hold read/write/export/email permissions on DocTypes "
                    "that contain PII fields.",
            "nodes": [n for n in graph["nodes"] if n["id"] in ids],
            "edges": [e for e in graph["edges"] if e["src"] in ids and e["dst"] in ids]}


def _cat_exits(graph: dict) -> dict:
    ids: set[str] = set()
    for n in graph["nodes"]:
        t, p = n["type"], n["props"]
        if t == "port" and p.get("published"):
            ids.add(n["id"])
        elif t in ("secret", "job"):
            ids.add(n["id"])
        elif t == "volume" and p.get("kind") == "bind":
            ids.add(n["id"])
    for e in graph["edges"]:
        if e["type"] in ("publishes", "uses_secret") and e["dst"] in ids:
            ids.add(e["src"])
        elif e["type"] == "mounts" and e["dst"] in ids:
            ids.add(e["src"])
        elif e["type"] == "executes" and e["src"] in ids:
            ids.add(e["dst"])
        elif e["type"] == "backs_up" and e["src"] in ids:
            ids.add(e["dst"])
        elif e["type"] == "emits_email":
            ids.add(e["src"])
            ids.add("sink:email")
    return {"title": "Exits — boundary crossings",
            "desc": "Every way data crosses the system boundary: published ports, "
                    "host mounts incl. docker.sock, secrets, backups, and the code "
                    "sites that send email.",
            "nodes": [n for n in graph["nodes"] if n["id"] in ids],
            "edges": [e for e in graph["edges"] if e["src"] in ids and e["dst"] in ids]}


def _cat_structure(graph: dict) -> dict:
    dt_ids = {n["id"] for n in graph["nodes"] if n["type"] == "doctype"}
    ids = dt_ids | {n["id"] for n in graph["nodes"]
                    if n["type"] in ("report", "print_format")}
    rel: dict[tuple, dict] = {}
    for e in graph["edges"]:
        t = e["type"]
        if t == "contains_child":
            a, b, kind = e["src"], e["dst"], "child"
        elif t == "links_to" and e["src"].startswith("field:"):
            a, b, kind = _doc_of_field(e["src"]), e["dst"], "link"
        elif t == "fetch_from" and e["src"].startswith("field:"):
            a, b, kind = _doc_of_field(e["src"]), e["dst"], "fetch"
        else:
            continue
        if a == b or a not in dt_ids or b not in dt_ids:
            continue
        rec = rel.setdefault((a, b), {"kinds": set(), "count": 0, "ev": _ev0(e)})
        rec["kinds"].add(kind)
        rec["count"] += 1
    edges = [_mk_edge("links_to", a, b,
                      "+".join(sorted(r["kinds"])) + (f" x{r['count']}" if r["count"] > 1 else ""),
                      r["ev"], r["count"]) for (a, b), r in sorted(rel.items())]
    for e in graph["edges"]:
        if e["type"] == "reads_from" and e["src"].startswith("report:") and e["dst"] in dt_ids:
            edges.append(e)
        elif e["type"] == "displays" and e["src"].startswith("print_format:") and e["dst"] in dt_ids:
            edges.append(e)
    return {"title": "Structure — DocType relationships",
            "desc": "DocType-to-DocType relationships (link/child/fetch, parallel "
                    "edges merged with counts) plus reports and print formats. "
                    "The advanced view.",
            "nodes": [n for n in graph["nodes"] if n["id"] in ids],
            "edges": edges}


BUILDERS = {
    "deployment": _cat_deployment,
    "customer-flow": _cat_customer_flow,
    "pii-flow": _cat_pii_flow,
    "access": _cat_access,
    "exits": _cat_exits,
    "structure": _cat_structure,
}


def build_categories(graph: dict) -> dict[str, dict]:
    out = {}
    for name in ORDER:
        c = BUILDERS[name](graph)
        c["preset"] = PRESETS[name]
        out[name] = c
    return out


INDEX_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Lineage views</title>
<style>
 body{margin:0;font:14px/1.5 system-ui,Segoe UI,sans-serif;background:#0f1419;color:#dde3ea}
 header{padding:22px 28px 6px}
 h1{margin:0 0 4px;font-size:20px}
 .meta{color:#8b98a8;font-size:12px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px;padding:18px 28px 30px}
 a.card{display:block;background:#161c24;border:1px solid #273140;border-radius:10px;
        padding:16px 18px;text-decoration:none;color:#dde3ea}
 a.card:hover{border-color:#3d5670;background:#1a222c}
 .card h2{margin:0 0 6px;font-size:15px;color:#7fb3e8}
 .card p{margin:0 0 10px;color:#aab7c4;font-size:13px}
 .counts{color:#8b98a8;font-size:12px}
 .preset{background:#22303f;border-radius:5px;padding:1px 7px;font-size:11px;color:#9fd0ff}
 code{background:#0b0f14;padding:1px 5px;border-radius:4px;color:#9fd0ff}
</style></head><body>
<header><h1>Lineage views — ERPNext + frappe_docker</h1>
<div class="meta">source: %%SOURCE%% · generated %%WHEN%% · %%TOTAL_NODES%% nodes / %%TOTAL_EDGES%% edges total
· one small view at a time · each page is standalone</div></header>
<div class="grid">%%CARDS%%</div>
</body></html>"""


def _card(name: str, c: dict) -> str:
    return (f'<a class="card" href="{name}.html"><h2>{c["title"]}</h2>'
            f'<p>{c["desc"]}</p>'
            f'<span class="counts">{len(c["nodes"])} nodes · '
            f'{len(c["edges"])} edges · <span class="preset">{c["preset"]}</span> · '
            f'<code>{name}</code></span></a>')


def main(argv: list[str]) -> int:
    graph_path, outdir, mermaid = None, str(paths.VIEWS_DIR), True
    for i, a in enumerate(argv):
        if a == "--graph" and i + 1 < len(argv):
            graph_path = argv[i + 1]
        elif a == "--outdir" and i + 1 < len(argv):
            outdir = argv[i + 1]
        elif a == "--no-mermaid":
            mermaid = False
    if graph_path is None:
        graph_path = str(paths.default_graph(prefer_demo=True))
    graph = tools.load(graph_path)
    cats = build_categories(graph)
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    vendor_out = out / "vendor"
    vendor_out.mkdir(exist_ok=True)
    for name in view.VENDOR_FILES:
        src = view.VENDOR_DIR / name
        if src.exists():
            shutil.copy(src, vendor_out / name)

    for name in ORDER:
        c = cats[name]
        sub = {"meta": graph["meta"], "nodes": c["nodes"], "edges": c["edges"]}
        page = view.build_html(sub, f'{c["title"]} — lineage',
                               vendor_tag=view.vendor_tags("vendor/"),
                               default_hidden=set(),
                               default_preset=c["preset"],
                               caption=c["desc"],
                               default_lr=name in LR_DEFAULT)
        (out / f"{name}.html").write_text(page, encoding="utf-8")
        if mermaid:
            (out / f"{name}.mmd").write_text(
                render.to_mermaid(sub, max_nodes=20000), encoding="utf-8")

    cards = "\n".join(_card(n, cats[n]) for n in ORDER)
    source = Path(graph_path).resolve()
    try:
        source_display = str(source.relative_to(paths.PROJECT_ROOT))
    except ValueError:
        source_display = str(source)
    index = (INDEX_TEMPLATE
             .replace("%%SOURCE%%", source_display)
             .replace("%%WHEN%%", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
             .replace("%%TOTAL_NODES%%", str(graph["meta"]["node_count"]))
             .replace("%%TOTAL_EDGES%%", str(graph["meta"]["edge_count"]))
             .replace("%%CARDS%%", cards))
    (out / "index.html").write_text(index, encoding="utf-8")

    for name in ORDER:
        c = cats[name]
        print(f"  {name:15s} {len(c['nodes']):5d} nodes  {len(c['edges']):5d} edges"
              f"  [{c['preset']}]")
    print(f"written to {out}/ — open {out / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
