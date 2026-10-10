"""Build the combined lineage graph from the two source repositories.

Pipeline:  frappe_docker adapter  ─┐
           erpnext adapter       ─┴─> merge -> scenarios -> meta -> graph dict

The result is the single artifact every lens (tools/agent/views/render)
reads. `lineage build` writes it to data/graph.json.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import docker_parser
from .core import Builder


def _git_commit(repo: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip() \
            or "unknown"
    except Exception:  # noqa: BLE001 - meta must never break the build
        return "unknown"


def build_graph(repo: Path, erp_root: Path | None = None) -> dict:
    """Parse `repo` (frappe_docker) and, if present, the sibling `erp_root`
    (erpnext) into one evidence-backed graph dict."""
    repo = Path(repo)
    b = Builder(repo)
    scenarios = docker_parser.load_scenarios(repo)

    compose_files = ["pwd.yml", "compose.yaml", "tests/compose.ci.yaml"]
    compose_files += sorted(str(p.relative_to(repo)).replace("\\", "/")
                            for p in (repo / "overrides").glob("*.yaml"))
    parsed_docs: dict[str, dict] = {}
    for rel in compose_files:
        if not (repo / rel).exists():
            continue
        doc = docker_parser.load_yaml(repo / rel)
        if doc:
            parsed_docs[rel] = doc
    all_service_names: set[str] = set()
    for doc in parsed_docs.values():
        all_service_names |= set((doc.get("services") or {}).keys())
    for rel, doc in parsed_docs.items():
        docker_parser.parse_compose(b, rel, all_service_names)
        docker_parser.derive_connections(b, rel, doc)

    for rel in ("images/bench/Dockerfile", "images/layered/Containerfile",
                "images/custom/Containerfile", "images/production/Containerfile"):
        if (repo / rel).exists():
            docker_parser.parse_containerfile(b, rel)

    docker_parser.parse_bake(b, "docker-bake.hcl")
    docker_parser.parse_example_env(b, "example.env")
    docker_parser.parse_owners(b, "MAINTAINERS.md")
    docker_parser.add_internal_exposes(b)
    docker_parser.link_runtime_files(b)

    # merge the ERPNext data-level graph (sibling repo), if present
    merged = None
    erp_root = Path(erp_root) if erp_root else (repo.parent / "erpnext")
    if (erp_root / "erpnext").is_dir():
        from . import frappe_parser
        merged = frappe_parser.build(erp_root)
        _merge_builder(b, merged)

    # scenario tagging
    for n in b.nodes.values():
        tags = ["repo"]
        files = set(n["files"])
        for sname, sfiles in scenarios.items():
            if files & set(sfiles):
                tags.append(sname)
        n["scenarios"] = sorted(set(tags))
    for e in b.edges.values():
        tags = ["repo"]
        files = set(e["files"])
        for sname, sfiles in scenarios.items():
            if files & set(sfiles):
                tags.append(sname)
        e["scenarios"] = sorted(set(tags))

    if merged:
        for n in b.nodes.values():
            if any(f.startswith("erpnext/") for f in n["files"]):
                n["scenarios"] = sorted(set(n["scenarios"]) | {"erpnext"})
        for e in b.edges.values():
            if any(f.startswith("erpnext/") for f in e["files"]):
                e["scenarios"] = sorted(set(e["scenarios"]) | {"erpnext"})

    nodes = sorted(b.nodes.values(), key=lambda n: n["id"])
    edges = sorted(b.edges.values(), key=lambda e: e["id"])

    git_commit = _git_commit(repo)
    meta = {
        "repo": repo.name,
        "git_commit": git_commit,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "node_count": len(nodes),
        "edge_count": len(edges),
    }
    if merged:
        meta["repos"] = [repo.name, "erpnext"]
        meta["git_commits"] = {repo.name: git_commit, "erpnext": _git_commit(erp_root)}
        meta["erpnext"] = merged["stats"]
    return {"meta": meta, "nodes": nodes, "edges": edges}


def _merge_builder(main: Builder, g2: dict):
    """Merge a second repo's builder output into the main builder."""
    for id_, n2 in g2["nodes"].items():
        if id_ not in main.nodes:
            main.nodes[id_] = n2
            continue
        n = main.nodes[id_]
        n["files"] = sorted(set(n["files"]) | set(n2["files"]))
        seen = {(ev["file"], ev["line"]) for ev in n["evidence"]}
        for ev in n2["evidence"]:
            if (ev["file"], ev["line"]) not in seen and len(n["evidence"]) < 12:
                n["evidence"].append(ev)
                seen.add((ev["file"], ev["line"]))
        n["boundary"] = n["boundary"] or n2["boundary"]
        rank = {"none": 0, "possible": 1, "likely": 2}
        if rank.get(n2["sensitivity"], 0) > rank.get(n["sensitivity"], 0):
            n["sensitivity"] = n2["sensitivity"]
        for k, v in n2["props"].items():
            n["props"].setdefault(k, v)
    for key, e2 in g2["edges"].items():
        if key not in main.edges:
            main.edges[key] = e2
