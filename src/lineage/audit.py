"""Adversarial audit of the generated graph.

Checks: schema integrity, unique ids, evidence line-accuracy against the
actual repo files (handles redacted/truncated snippets), secret-leak scan.
Run: lineage audit   (exit code 0 = passed)
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from . import paths, tools

REPOS = {"erpnext": paths.erpnext_repo(), "frappe_docker": paths.frappe_docker_repo()}


def repo_path(file_: str) -> Path | None:
    if file_.startswith("erpnext/"):
        return REPOS["erpnext"] / file_
    if (REPOS["frappe_docker"] / file_).exists():
        return REPOS["frappe_docker"] / file_
    return None


def evidence_audit(g: dict) -> tuple[int, list[str]]:
    cache: dict[Path, list[str]] = {}

    def raw_line(p: Path, num: int) -> str | None:
        if p not in cache:
            try:
                cache[p] = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                cache[p] = []
        lines = cache[p]
        return lines[num - 1] if 1 <= num <= len(lines) else None

    bad: list[str] = []
    total = 0
    items = [("node", n) for n in g["nodes"]] + [("edge", e) for e in g["edges"]]
    for kind, item in items:
        for ev in item["evidence"]:
            total += 1
            p = repo_path(ev["file"])
            raw = raw_line(p, ev["line"]) if p else None
            if raw is None:
                bad.append(f"{kind} {item['id']}: out-of-range {ev['file']}:{ev['line']}")
                continue
            pos = 0
            for part in (ev["snippet"] or "").split("***"):
                idx = raw.find(part, pos)
                if idx < 0:
                    bad.append(f"{kind} {item['id']}: mismatch {ev['file']}:{ev['line']}")
                    break
                pos = idx + len(part)
    return total, bad


def evidence_locality(g: dict) -> tuple[int, list[str]]:
    """links_to / contains_child evidence must sit inside the source field's
    own JSON object — not at another field that happens to share the target
    (Builder.ev's first-match behaviour got this wrong for parallel fields)."""
    idx = {n["id"]: n for n in g["nodes"]}
    cache: dict[str, list[str]] = {}

    def lines_for(file_: str) -> list[str]:
        if file_ not in cache:
            p = repo_path(file_)
            try:
                cache[file_] = (p.read_text(encoding="utf-8", errors="replace")
                                .splitlines() if p else [])
            except OSError:
                cache[file_] = []
        return cache[file_]

    bad: list[str] = []
    checked = 0
    for e in g["edges"]:
        if e["type"] == "links_to" and e["src"].startswith("field:"):
            sfn = e["src"].split(":", 1)[1]
            s_doc, fn = sfn.split(".", 1)
            did = f"doctype:{s_doc}"
        elif e["type"] == "contains_child":
            did, fn = e["src"], e["props"].get("via_field")
        else:
            continue
        dn = idx.get(did)
        if not dn or not fn:
            continue
        file_ = next((ev["file"] for ev in dn["evidence"]
                      if ev["file"].startswith("erpnext/")), None)
        if not file_:
            continue
        lines = lines_for(file_)
        start = None
        for i, ln in enumerate(lines):
            if f'"fieldname": "{fn}"' in ln:
                start = i
                break
        if start is None:
            continue
        end = len(lines)
        for j in range(start + 1, min(start + 60, len(lines))):
            if '"fieldname"' in lines[j]:
                end = j
                break
        checked += 1
        for ev in e["evidence"]:
            if ev["line"] and not (start + 1 <= ev["line"] <= end + 1):
                bad.append(f"{e['id']}: evidence {file_}:{ev['line']} not inside "
                           f"field '{fn}' (lines {start + 1}-{end + 1})")
                break
    return checked, bad


def dup_ids(g: dict) -> tuple[list[str], list[str]]:
    nodes = [k for k, v in Counter(n["id"] for n in g["nodes"]).items() if v > 1]
    edges = [k for k, v in Counter(e["id"] for e in g["edges"]).items() if v > 1]
    return nodes, edges


def leak_scan(g: dict) -> list[str]:
    blob = json.dumps(g)
    leaks = []
    for pat in (r": *admin\b", r"=admin\b", r"password=admin", r"PASSWORD: *admin",
                r"DB_PASSWORD=-\d", r"PASSWORD:-123", r"password:-changeit"):
        for m in re.finditer(pat, blob, re.I):
            leaks.append(blob[max(0, m.start() - 40):m.end() + 10])
    return leaks


def main(graph_path: str | None = None) -> int:
    g = tools.load(graph_path)
    problems = 0

    errs = tools.validate_schema(g)
    print(f"schema: {len(errs)} errors" + (" (!)" if errs else "  OK"))
    problems += bool(errs)

    dn, de = dup_ids(g)
    print(f"dup ids: nodes={len(dn)} edges={len(de)}"
          + (" (!)" if dn or de else "  OK"))
    problems += bool(dn or de)

    total, bad = evidence_audit(g)
    print(f"evidence: {total} entries, {len(bad)} mismatches"
          + (" (!)" if bad else "  OK"))
    for b in bad[:10]:
        print("   ", b)
    problems += bool(bad)

    checked_loc, bad_loc = evidence_locality(g)
    print(f"evidence locality: {checked_loc} field-scoped edges, "
          f"{len(bad_loc)} misplaced" + (" (!)" if bad_loc else "  OK"))
    for b in bad_loc[:10]:
        print("   ", b)
    problems += bool(bad_loc)

    leaks = leak_scan(g)
    print(f"secret leaks: {len(leaks)}" + (" (!)" if leaks else "  OK"))
    for l in leaks[:5]:
        print("   ", l)
    problems += bool(leaks)

    print("AUDIT", "FAILED" if problems else "PASSED")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
