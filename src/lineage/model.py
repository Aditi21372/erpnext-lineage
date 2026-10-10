"""Graph model: node/edge type registries and schema validation.

The artifact contract (SCHEMA.md) implemented in code. Every parser output,
tool and viewer relies on these invariants; `validate()` is the gate used by
the test suite and the adversarial audit.
"""
from __future__ import annotations

NODE_TYPES = {"service", "image", "build_stage", "volume", "network", "port",
              "env_var", "secret", "file", "job", "artifact", "owner",
              "doctype", "field", "role", "report", "print_format",
              "code", "api", "source", "sink"}
EDGE_TYPES = {"uses_image", "built_from", "builds_from", "clones", "depends_on",
              "connects_to", "mounts", "reads_from", "writes_to", "joins_network",
              "exposes", "publishes", "proxies_to", "configures", "uses_secret",
              "writes_config", "executes", "owns", "backs_up",
              "contains", "links_to", "contains_child", "fetch_from", "stored_in",
              "permits", "displays", "emits_email", "wires"}
NODE_REQUIRED = ("id", "type", "name", "label", "tier", "group", "sensitivity",
                 "boundary", "confidence", "evidence", "files", "scenarios", "props")
EDGE_REQUIRED = ("id", "type", "src", "dst", "label", "confidence", "evidence",
                 "files", "scenarios", "boundary", "props")
ID_PREFIX = {"env_var": "env"}

# Data-flow traversal sets used by tools (taint / blast radius).
TAINT_EDGES = {"mounts", "reads_from", "writes_to", "connects_to", "proxies_to",
               "publishes", "executes", "backs_up",
               "contains", "links_to", "contains_child", "fetch_from", "stored_in",
               "emits_email"}
REACH_EDGES = {"connects_to", "depends_on", "proxies_to", "mounts", "publishes",
               "uses_secret", "links_to", "permits", "contains", "contains_child",
               "reads_from", "writes_to", "emits_email"}


def validate(g: dict) -> list[str]:
    """Referential integrity + required fields + unique ids. Returns errors."""
    errors: list[str] = []
    ids = set()
    for n in g.get("nodes", []):
        for key in NODE_REQUIRED:
            if key not in n:
                errors.append(f"node {n.get('id', '?')} missing field {key}")
        if n.get("type") not in NODE_TYPES:
            errors.append(f"node {n.get('id')} bad type {n.get('type')}")
        prefix = n.get("id", "").split(":", 1)[0]
        if prefix != ID_PREFIX.get(n.get("type"), n.get("type")):
            errors.append(f"node id/type mismatch: {n.get('id')} vs {n.get('type')}")
        if not isinstance(n.get("evidence"), list):
            errors.append(f"node {n.get('id')} evidence not a list")
        if n.get("id") in ids:
            errors.append(f"duplicate node id: {n.get('id')}")
        ids.add(n.get("id"))
    edge_ids = set()
    for e in g.get("edges", []):
        for key in EDGE_REQUIRED:
            if key not in e:
                errors.append(f"edge {e.get('id', '?')} missing field {key}")
        if e.get("type") not in EDGE_TYPES:
            errors.append(f"edge {e.get('id')} bad type {e.get('type')}")
        if e.get("src") not in ids:
            errors.append(f"edge {e.get('id')} src missing node {e.get('src')}")
        if e.get("dst") not in ids:
            errors.append(f"edge {e.get('id')} dst missing node {e.get('dst')}")
        if not isinstance(e.get("evidence"), list):
            errors.append(f"edge {e.get('id')} evidence not a list")
        if e.get("id") in edge_ids:
            errors.append(f"duplicate edge id: {e.get('id')}")
        edge_ids.add(e.get("id"))
    return errors
