"""Graph builder primitives shared by every repository adapter.

`Builder` is the generic accumulation layer: adapters (docker_parser,
frappe_parser) call `node()`, `edge()` and `attach()` while they walk a
repository, and get evidence, confidence merging, sensitivity ranking and
deduplication for free. Nothing in this module knows what a DocType or a
compose service is — that is the adapter's job.
"""
from __future__ import annotations

import re

TIER_FOR_GROUP = {"external": 0, "ingress": 1, "compute": 2, "ops": 2,
                  "config": 2, "data": 3, "build": 0, "network": 2}
# Applied when an adapter asks for redacted evidence snippets.
REDACT_RE = re.compile(
    r"(?i)((?:password|secret|token|api[_-]?key)[A-Za-z0-9_-]*\s*[=:]\s*)(\S+)")


def slug(s: str) -> str:
    """Canonical id fragment: lowercase, dashes, no leading/trailing dash."""
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


class Builder:
    def __init__(self, repo):
        self.repo = repo
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}
        self._lines: dict[str, list[str]] = {}

    # --- text helpers
    def text(self, rel: str) -> str:
        return (self.repo / rel).read_text(encoding="utf-8", errors="replace")

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            self._lines[rel] = self.text(rel).splitlines()
        return self._lines[rel]

    def line_of(self, rel: str, needle: str) -> tuple[int, str]:
        for i, line in enumerate(self.lines(rel), start=1):
            if needle in line:
                return i, line.strip()[:160]
        return 0, ""

    def ev(self, rel: str, needle: str, redact: bool = False) -> dict:
        num, snippet = self.line_of(rel, needle)
        if redact and snippet:
            snippet = REDACT_RE.sub(r"\1***", snippet)
        return {"file": rel, "line": num, "snippet": snippet}

    # --- graph helpers
    def node(self, id_: str, type_: str, name: str | None = None, **props) -> dict:
        sens = props.pop("sensitivity", None)
        bound = props.pop("boundary", None)
        conf = props.pop("confidence", None)
        if id_ in self.nodes:
            n = self.nodes[id_]
            # A volume that is host-bound by a bind override stays kind=bind
            # even though service mounts re-declare it as a named volume.
            if type_ == "volume" and props.get("kind") == "named" \
                    and n["props"].get("kind") == "bind":
                props = {k: v for k, v in props.items() if k != "kind"}
            n["props"].update({k: v for k, v in props.items() if v is not None})
            if bound is not None:
                n["boundary"] = n["boundary"] or bool(bound)
            rank = {"none": 0, "possible": 1, "likely": 2}
            if sens and rank.get(sens, 0) > rank.get(n["sensitivity"], 0):
                n["sensitivity"] = sens
            if conf:
                order = {"inferred": 0, "parsed": 1, "validated": 2}
                if order.get(conf, 0) > order.get(n["confidence"], 0):
                    n["confidence"] = conf
            return n
        _, _, short = id_.partition(":")
        n = {
            "id": id_, "type": type_, "name": name or short,
            "label": name or short,
            "tier": TIER_FOR_GROUP.get(props.get("group", ""), 2),
            "group": props.get("group", "compute"),
            "sensitivity": sens or "none",
            "boundary": bool(bound) if bound is not None else False,
            "confidence": conf or "parsed",
            "evidence": [], "files": [], "scenarios": [],
            "props": props,
        }
        self.nodes[id_] = n
        return n

    def attach(self, id_: str | dict, evidence: dict | None, file_: str,
               sensitivity="none", boundary=None, confidence=None):
        if isinstance(id_, dict):
            id_ = id_["id"]
        n = self.nodes[id_]
        if file_ not in n["files"]:
            n["files"].append(file_)
        if evidence:
            key = (evidence["file"], evidence["line"])
            if key not in {(e["file"], e["line"]) for e in n["evidence"]}:
                if len(n["evidence"]) < 12:
                    n["evidence"].append(evidence)
                else:
                    files = [e["file"] for e in n["evidence"]]
                    if evidence["file"] not in files:
                        # Deterministic eviction: among equally frequent files
                        # pick the lexicographically largest, never a set-order
                        # random one (regenerating the graph must be stable).
                        most = max(sorted(set(files)),
                                   key=lambda f: (files.count(f), f))
                        for i, e in enumerate(n["evidence"]):
                            if e["file"] == most:
                                n["evidence"][i] = evidence
                                break
        rank = {"none": 0, "possible": 1, "likely": 2}
        if rank.get(sensitivity, 0) > rank.get(n["sensitivity"], 0):
            n["sensitivity"] = sensitivity
        if boundary is not None:
            n["boundary"] = n["boundary"] or bool(boundary)
        if confidence:
            order = {"inferred": 0, "parsed": 1, "validated": 2}
            if order.get(confidence, 0) > order.get(n["confidence"], 0):
                n["confidence"] = confidence
        return n

    def edge(self, type_: str, src: str, dst: str, evidence: dict,
             confidence="parsed", boundary=False, label="", **props) -> dict:
        # `path` is part of the identity for mounts: the same service may mount
        # one volume at different container paths in different scenarios
        # (e.g. db-data at /var/lib/mysql vs /var/lib/postgresql/data);
        # merging those would silently overwrite the path attribute.
        key = (type_, src, dst, props.get("via", ""), props.get("path", ""))
        if key in self.edges:
            e = self.edges[key]
            e["props"].update({k: v for k, v in props.items() if v is not None})
            e["boundary"] = e["boundary"] or boundary
            if evidence and len(e["evidence"]) < 6:
                e["evidence"].append(evidence)
            return e
        suffix = props.get("via") or (props.get("path") if type_ == "mounts" else "")
        e = {
            "id": f"{type_}:{src}->{dst}" + (f"@{suffix}" if suffix else ""),
            "type": type_, "src": src, "dst": dst, "label": label,
            "confidence": confidence, "boundary": bool(boundary),
            "evidence": [evidence] if evidence else [],
            "files": [evidence["file"]] if evidence else [],
            "scenarios": [], "props": props,
        }
        self.edges[key] = e
        return e

    def attach_edge(self, e: dict, file_: str):
        if file_ and file_ not in e["files"]:
            e["files"].append(file_)
        return e
