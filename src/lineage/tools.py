"""Query tools over the lineage graph. Importable + CLI.

Every function returns evidence-backed plain data (dicts), safe for an LLM
to consume. No hidden state; pass the graph explicitly.
"""
from __future__ import annotations

import difflib
import json
import re
import sys
from collections import Counter, deque
from pathlib import Path

from . import paths
from .model import EDGE_TYPES, REACH_EDGES, TAINT_EDGES, validate

INT_ARGS = {"limit", "depth", "max_depth"}


def load(path: str | Path | None = None) -> dict:
    path = Path(path) if path else paths.default_graph()
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _index(g: dict):
    return {n["id"]: n for n in g["nodes"]}


def _adj(g: dict):
    out: dict[str, list[dict]] = {}
    for e in g["edges"]:
        out.setdefault(e["src"], []).append(e)
        out.setdefault(e["dst"], []).append(e)
    return out


def validate_schema(g: dict) -> list[str]:
    """Referential integrity + required fields + unique ids. Returns errors."""
    return validate(g)


def graph_summary(g: dict) -> dict:
    counts: dict[str, int] = {}
    for n in g["nodes"]:
        counts[n["type"]] = counts.get(n["type"], 0) + 1
    ecounts: dict[str, int] = {}
    for e in g["edges"]:
        ecounts[e["type"]] = ecounts.get(e["type"], 0) + 1
    return {"meta": g["meta"], "node_counts": counts, "edge_counts": ecounts}


def list_nodes(g: dict, type: str | None = None, scenario: str | None = None,
               name_contains: str | None = None, limit: int = 100,
               module: str | None = None) -> list[dict]:
    out = []
    for n in g["nodes"]:
        if type and n["type"] != type:
            continue
        if scenario and scenario not in n["scenarios"]:
            continue
        if module and n["props"].get("module") != module:
            continue
        if name_contains and name_contains.lower() not in n["id"].lower() \
                and name_contains.lower() not in n["label"].lower():
            continue
        out.append(n)
        if len(out) >= limit:
            break
    return out


def list_edges(g: dict, type: str | None = None, src: str | None = None,
               dst: str | None = None, limit: int = 200) -> list[dict]:
    out = []
    for e in g["edges"]:
        if type and e["type"] != type:
            continue
        if src and e["src"] != src:
            continue
        if dst and e["dst"] != dst:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def get_node(g: dict, id: str) -> dict:
    idx = _index(g)
    if id not in idx:
        close = [i for i in idx if id.lower() in i.lower()][:5]
        raise KeyError(f"node not found: {id}" + (f" (did you mean: {close})" if close else ""))
    n = dict(idx[id])
    n["edges_out"] = [e for e in g["edges"] if e["src"] == id]
    n["edges_in"] = [e for e in g["edges"] if e["dst"] == id]
    n["neighbors"] = sorted({e["dst"] for e in n["edges_out"]} |
                            {e["src"] for e in n["edges_in"]})
    return n


def trace(g: dict, id: str, direction: str = "both", depth: int = 2,
          edge_types: set[str] | None = None) -> dict:
    """BFS from a node; returns the node + every traversed edge with direction."""
    idx = _index(g)
    if id not in idx:
        raise KeyError(f"node not found: {id}")
    types = edge_types or EDGE_TYPES
    seen_nodes = {id}
    seen_edges: dict[tuple, dict] = {}
    frontier = [(id, 0)]
    while frontier:
        cur, d = frontier.pop(0)
        if d >= depth:
            continue
        for e in g["edges"]:
            if e["type"] not in types:
                continue
            direction_out = e["src"] == cur and direction in ("both", "out")
            direction_in = e["dst"] == cur and direction in ("both", "in")
            if not (direction_out or direction_in):
                continue
            key = (e["id"], "out" if direction_out else "in")
            if key in seen_edges:
                continue
            ee = dict(e)
            ee["direction"] = "out" if direction_out else "in"
            seen_edges[key] = ee
            other = e["dst"] if direction_out else e["src"]
            if other not in seen_nodes:
                seen_nodes.add(other)
                frontier.append((other, d + 1))
    return {"node": idx[id], "edges": list(seen_edges.values()),
            "reached_nodes": sorted(seen_nodes)}


def find_path(g: dict, src: str, dst: str, max_depth: int = 6) -> list[str]:
    idx = _index(g)
    if src not in idx or dst not in idx:
        raise KeyError(f"unknown node(s): {src if src not in idx else ''} "
                       f"{dst if dst not in idx else ''}".strip())
    adj = _adj(g)
    prev = {src: None}
    q = deque([(src, 0)])
    while q:
        cur, d = q.popleft()
        if cur == dst:
            path = [cur]
            while prev[path[-1]]:
                path.append(prev[path[-1]])
            return list(reversed(path))
        if d >= max_depth:
            continue
        for e in adj.get(cur, []):
            other = e["dst"] if e["src"] == cur else e["src"]
            if other not in prev:
                prev[other] = cur
                q.append((other, d + 1))
    return []


def search_pii(g: dict) -> list[dict]:
    """Sensitive stores, secrets, and PII-bearing env vars — with evidence."""
    hits = []
    for n in g["nodes"]:
        reasons = []
        if n["type"] == "volume" and n["sensitivity"] != "none":
            reasons.append(f"volume sensitivity={n['sensitivity']} "
                           f"categories={n['props'].get('categories', [])}")
        if n["type"] == "secret":
            reasons.append(f"secret kind={n['props'].get('kind')} weak={n['props'].get('weak')}")
        if n["type"] == "env_var" and n["props"].get("categories"):
            reasons.append(f"env categories={n['props']['categories']}")
        if n["type"] == "field" and n["sensitivity"] != "none":
            reasons.append(f"field categories={n['props'].get('categories', [])} "
                           f"sensitivity={n['sensitivity']}")
        if reasons:
            hits.append({"id": n["id"], "type": n["type"], "label": n["label"],
                         "sensitivity": n["sensitivity"], "reasons": reasons,
                         "evidence": n["evidence"][:3]})
    return sorted(hits, key=lambda h: h["id"])


def find_exits(g: dict) -> list[dict]:
    """Everything that crosses the system boundary outward, with evidence."""
    exits = []
    for n in g["nodes"]:
        if n["type"] == "port" and n["props"].get("published"):
            exits.append({"node": n["id"], "kind": "published_port",
                          "why": f"host port {n['props'].get('host_port')} -> "
                                 f"container {n['props'].get('container_port')}",
                          "evidence": n["evidence"]})
        elif n["type"] == "volume" and n["props"].get("kind") == "bind":
            exits.append({"node": n["id"], "kind": "host_filesystem",
                          "why": f"bind mount to host path {n['props'].get('device')}",
                          "evidence": n["evidence"]})
        elif n["type"] == "job" and any(
                e["type"] == "backs_up" and e["src"] == n["id"] for e in g["edges"]):
            exits.append({"node": n["id"], "kind": "backup_job",
                          "why": n["props"].get("command", ""),
                          "evidence": n["evidence"]})
        elif n["type"] == "secret" and n["props"].get("kind") in ("docker_secret", "file"):
            exits.append({"node": n["id"], "kind": "secret_file",
                          "why": f"secret from file {n['props'].get('file')}",
                          "evidence": n["evidence"]})
    for e in g["edges"]:
        if e["type"] == "mounts" and "/var/run/docker.sock" in json.dumps(e["props"]):
            exits.append({"node": e["src"], "kind": "host_control_plane",
                          "why": "docker.sock mounted (ro) — controls all containers",
                          "evidence": e["evidence"]})
    return exits


def taint(g: dict, start: str) -> list[dict]:
    """Undirected BFS over data-flow edges: who can carry/hold this data."""
    idx = _index(g)
    if start not in idx:
        raise KeyError(f"node not found: {start}")
    adj = _adj(g)
    seen = {start}
    q = deque([start])
    while q:
        cur = q.popleft()
        for e in adj.get(cur, []):
            if e["type"] not in TAINT_EDGES:
                continue
            other = e["dst"] if e["src"] == cur else e["src"]
            if other not in seen:
                seen.add(other)
                q.append(other)
    return [idx[i] for i in sorted(seen)]


def reachable_from(g: dict, id: str, direction: str = "in") -> list[dict]:
    """Blast radius: who reaches this node (in) or what it reaches (out)."""
    idx = _index(g)
    if id not in idx:
        raise KeyError(f"node not found: {id}")
    adj = _adj(g)
    seen = {id}
    q = deque([id])
    while q:
        cur = q.popleft()
        for e in adj.get(cur, []):
            if e["type"] not in REACH_EDGES:
                continue
            if direction == "in" and e["dst"] == cur:
                other = e["src"]
            elif direction == "out" and e["src"] == cur:
                other = e["dst"]
            else:
                continue
            if other not in seen:
                seen.add(other)
                q.append(other)
    return [idx[i] for i in sorted(seen)]


def scenario_report(g: dict, name: str) -> dict:
    nodes = [n for n in g["nodes"] if name in n["scenarios"]]
    edges = [e for e in g["edges"] if name in e["scenarios"]]
    return {"scenario": name, "node_count": len(nodes), "edge_count": len(edges),
            "services": sorted(n["name"] for n in nodes if n["type"] == "service"),
            "volumes": sorted(n["name"] for n in nodes if n["type"] == "volume"),
            "published_ports": sorted(n["id"] for n in nodes
                                      if n["type"] == "port" and n["props"].get("published"))}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")


def _doctype_nodes(g: dict) -> list[dict]:
    return [n for n in g["nodes"] if n["type"] == "doctype"]


def _doctype_lookup(g: dict) -> dict[str, dict]:
    """Map slug / display name / label (case-insensitive) -> doctype node."""
    lookup: dict[str, dict] = {}
    for n in _doctype_nodes(g):
        slug = n["id"].split(":", 1)[1]
        keys = {slug, slug.replace("-", " "), (n.get("name") or "").lower(),
                (n.get("label") or "").lower(), _slugify(n.get("name") or ""),
                _slugify(n.get("label") or "")}
        for key in keys:
            if key:
                lookup.setdefault(key, n)
    return lookup


def _resolve_doctype(g: dict, doctype: str) -> dict:
    q = str(doctype or "").strip().lower()
    if q.startswith("doctype:"):
        q = q[len("doctype:"):]
    if not q:
        raise KeyError("doctype not found: (empty)")
    lookup = _doctype_lookup(g)
    hit = lookup.get(q) or lookup.get(_slugify(q))
    if hit:
        return hit
    keys = sorted(lookup)
    close = difflib.get_close_matches(q, keys, n=5, cutoff=0.6)
    close += [k for k in keys if q in k and k not in close]
    suggestions: list[str] = []
    for k in dict.fromkeys(close):
        cand = lookup[k]["id"]
        if cand not in suggestions:
            suggestions.append(cand)
    raise KeyError(f"doctype not found: {doctype}" +
                   (f" (did you mean: {suggestions[:5]})" if suggestions else ""))


def doctype_report(g: dict, doctype: str) -> dict:
    """One-shot report on a DocType (slug or display name, case-insensitive)."""
    n = _resolve_doctype(g, doctype)
    idx = _index(g)
    slug = n["id"].split(":", 1)[1]
    prefix = f"field:{slug}."
    props = n["props"] or {}

    def display(node_id: str) -> str:
        target = idx.get(node_id)
        return (target.get("label") or target.get("name")) if target else node_id

    def owner_doctype(field_id: str) -> str | None:
        if not field_id.startswith("field:"):
            return None
        return "doctype:" + field_id[len("field:"):].rsplit(".", 1)[0]

    field_ids = {e["dst"] for e in g["edges"]
                 if e["type"] == "contains" and e["src"] == n["id"]}
    field_nodes = [idx[f] for f in sorted(field_ids) if f in idx]

    def bare_field_name(field: dict) -> str:
        name = field.get("name") or field["id"].split(":", 1)[-1]
        return name[len(slug) + 1:] if name.startswith(slug + ".") else name

    pii = [{"field": bare_field_name(f), "categories": f["props"].get("categories", [])}
           for f in field_nodes if f["sensitivity"] != "none"]

    child_tables, seen_children = [], set()
    for e in g["edges"]:
        if e["type"] == "contains_child" and e["src"] == n["id"] and e["dst"] not in seen_children:
            seen_children.add(e["dst"])
            child_tables.append({"doctype": display(e["dst"]), "id": e["dst"],
                                 "via": e["props"].get("via_field") or e.get("label") or ""})

    links_out_edges = [e for e in g["edges"]
                       if e["type"] == "links_to" and e["src"].startswith(prefix)]
    out_counts = Counter(e["dst"] for e in links_out_edges)
    links_in_edges = [e for e in g["edges"]
                      if e["type"] == "links_to" and e["dst"] == n["id"]]
    in_counts = Counter(owner_doctype(e["src"]) or e["src"] for e in links_in_edges)

    perm_edges = [e for e in g["edges"]
                  if e["type"] == "permits" and e["dst"] == n["id"]]
    roles = []
    for e in sorted(perm_edges, key=lambda e: e["src"]):
        flags = sorted(k for k, v in (e["props"] or {}).items() if v is True)
        roles.append([display(e["src"]), flags])
    has_export = any((e["props"] or {}).get("export") for e in perm_edges)

    report_ids = {e["src"] for e in g["edges"]
                  if e["type"] == "reads_from" and e["src"].startswith("report:")
                  and (e["dst"] == n["id"] or e["dst"].startswith(prefix))}
    pf_ids = {e["src"] for e in g["edges"]
              if e["type"] == "displays"
              and (e["dst"] == n["id"] or e["dst"].startswith(prefix))}
    stored = any(e["type"] == "stored_in" and e["src"] == n["id"] for e in g["edges"])

    evidence = [{"file": ev.get("file"), "line": ev.get("line"),
                 "snippet": ev.get("snippet", "")}
                for ev in (n.get("evidence") or [])[:3]]
    return {
        "id": n["id"], "slug": slug, "name": n.get("label") or n["name"],
        "module": props.get("module"),
        "flags": {"submittable": bool(props.get("is_submittable")),
                  "track_changes": bool(props.get("track_changes")),
                  "allow_import": bool(props.get("allow_import")),
                  "istable": bool(props.get("istable")),
                  "issingle": bool(props.get("issingle"))},
        "fields": {"total_materialized": len(field_nodes), "pii": pii,
                   "child_tables": child_tables},
        "links_out": {"total": len(links_out_edges),
                      "top_targets": [[display(d), c]
                                      for d, c in out_counts.most_common(10)]},
        "links_in": {"total": len(links_in_edges),
                     "top_sources": [[display(o) if o in idx else o, c]
                                     for o, c in in_counts.most_common(10)]},
        "permissions": {"roles": roles[:12], "has_export_grants": has_export},
        "reports_reading_count": len(report_ids),
        "print_formats_count": len(pf_ids),
        "stored_in": stored,
        "evidence": evidence,
    }


def pii_report(g: dict) -> dict:
    """Compliance-style PII summary across the whole graph."""
    fields = [n for n in g["nodes"] if n["type"] == "field"]
    pii = [n for n in fields if n["sensitivity"] != "none"]
    by_cat = Counter(c for n in pii for c in (n["props"].get("categories") or []))
    by_doc: Counter = Counter()
    for n in pii:
        owner = n["id"].split(":", 1)[1].rsplit(".", 1)[0]
        by_doc["doctype:" + owner] += 1
    attach = [n for n in fields
              if (n["props"].get("fieldtype") or "") in ("Attach", "Attach Image")]
    passwords = [n for n in fields if n["props"].get("fieldtype") == "Password"]
    searchable = [n for n in fields if n["props"].get("globally_searchable")]
    searchable_pii = [n for n in searchable if n["sensitivity"] != "none"]
    emits = sum(1 for e in g["edges"] if e["type"] == "emits_email")
    stored = sum(1 for e in g["edges"]
                 if e["type"] == "stored_in" and e["dst"] == "volume:db-data")
    return {
        "total_pii_fields": len(pii),
        "by_category": dict(sorted(by_cat.items())),
        "top_doctypes": [[d, c] for d, c in by_doc.most_common(15)],
        "attachments": len(attach),
        "password_fields": len(passwords),
        "globally_searchable": len(searchable_pii),
        "globally_searchable_all": len(searchable),
        "emits_email_edges": emits,
        "stored_in_db_data_doctypes": stored,
    }


TOOLS = {
    "graph_summary": graph_summary,
    "list_nodes": list_nodes,
    "list_edges": list_edges,
    "get_node": get_node,
    "trace": trace,
    "find_path": find_path,
    "search_pii": search_pii,
    "find_exits": find_exits,
    "taint": taint,
    "reachable_from": reachable_from,
    "scenario_report": scenario_report,
    "doctype_report": doctype_report,
    "pii_report": pii_report,
}


def main(argv: list[str]):
    g = load()
    cmd = argv[0] if argv else "summary"
    args = [a for a in argv[1:]]
    if cmd == "summary":
        print(json.dumps(graph_summary(g), indent=2))
    elif cmd == "validate":
        errs = validate_schema(g)
        print(f"{len(errs)} schema errors" + ("\n" + "\n".join(errs[:20]) if errs else ""))
    else:
        fn = TOOLS.get(cmd)
        if not fn:
            print(f"unknown command. available: summary, validate, {', '.join(TOOLS)}")
            return 1
        kwargs = {}
        for a in args:
            if "=" in a:
                k, v = a.split("=", 1)
                if k in INT_ARGS and v.isdigit():
                    kwargs[k] = int(v)
                else:
                    kwargs[k] = v
        print(json.dumps(fn(g, **kwargs), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
