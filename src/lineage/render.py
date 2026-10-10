"""Render the lineage graph to Mermaid or Graphviz DOT.

Full graph:
    lineage render --format mermaid --out data/graph.mmd
Focused subgraph (recommended for the merged ERPNext graph):
    lineage render --focus doctype:customer --depth 2 --out data/customer.dot --format dot
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import paths, tools


def _sanitize(id_: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", id_)


def _label(n: dict) -> str:
    base = f'{n["type"]}:{n["name"]}'
    samples = (n.get("props") or {}).get("sample_values")
    if samples:
        base += f" | e.g. {samples[0]}"
    return base.replace('"', "'")[:120]


def _subgraph(g: dict, focus: str, depth: int):
    idx = {n["id"]: n for n in g["nodes"]}
    if focus not in idx:
        raise KeyError(f"node not found: {focus}")
    adj: dict[str, list[dict]] = {}
    for e in g["edges"]:
        adj.setdefault(e["src"], []).append(e)
        adj.setdefault(e["dst"], []).append(e)
    seen = {focus}
    frontier = [(focus, 0)]
    used_edges: dict[str, dict] = {}
    while frontier:
        cur, d = frontier.pop(0)
        if d >= depth:
            continue
        for e in adj.get(cur, []):
            used_edges[e["id"]] = e
            other = e["dst"] if e["src"] == cur else e["src"]
            if other not in seen:
                seen.add(other)
                frontier.append((other, d + 1))
    return [idx[i] for i in sorted(seen)], list(used_edges.values())


def _select(g: dict, focus: str | None, depth: int, max_nodes: int):
    if focus:
        return _subgraph(g, focus, depth)
    nodes = g["nodes"][:max_nodes]
    ids = {n["id"] for n in nodes}
    edges = [e for e in g["edges"] if e["src"] in ids and e["dst"] in ids]
    return nodes, edges


def to_mermaid(g: dict, focus: str | None = None, depth: int = 2,
               max_nodes: int = 5000) -> str:
    nodes, edges = _select(g, focus, depth, max_nodes)
    lines = ["flowchart LR"]
    for n in nodes:
        lines.append(f'  {_sanitize(n["id"])}["{_label(n)}"]')
    for e in edges:
        lines.append(f'  {_sanitize(e["src"])} -->|{e["type"]}| {_sanitize(e["dst"])}')
    return "\n".join(lines)


def to_dot(g: dict, focus: str | None = None, depth: int = 2,
           max_nodes: int = 5000) -> str:
    nodes, edges = _select(g, focus, depth, max_nodes)
    lines = ["digraph lineage {", '  rankdir="LR";']
    for n in nodes:
        lines.append(f'  "{n["id"]}" [label="{_label(n)}"];')
    for e in edges:
        lines.append(f'  "{e["src"]}" -> "{e["dst"]}" [label="{e["type"]}"];')
    lines.append("}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    fmt = "mermaid"
    out = None
    focus = None
    depth = 2
    graph_path = None
    for i, a in enumerate(argv):
        if a == "--format" and i + 1 < len(argv):
            fmt = argv[i + 1]
        elif a == "--out" and i + 1 < len(argv):
            out = Path(argv[i + 1])
        elif a == "--focus" and i + 1 < len(argv):
            focus = argv[i + 1]
        elif a == "--depth" and i + 1 < len(argv):
            depth = int(argv[i + 1])
        elif a == "--graph" and i + 1 < len(argv):
            graph_path = argv[i + 1]
    g = tools.load(graph_path)
    renderer = to_mermaid if fmt == "mermaid" else to_dot
    text = renderer(g, focus=focus, depth=depth)
    paths.ensure_data_dir()
    default = paths.DATA_DIR / ("graph.mmd" if fmt == "mermaid" else "graph.dot")
    target = out or default
    target.write_text(text, encoding="utf-8")
    print(f"{target} written ({len(text.splitlines())} lines, focus={focus})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
