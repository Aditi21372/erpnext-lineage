"""Generate fictional sample data for graph nodes and apply it for demos.

Usage:
    lineage dummy generate --out data/dummy_data.json [--seed 42] [--records 3]
    lineage dummy apply    --data data/dummy_data.json --out data/graph_demo.json

Then view the populated graph:
    lineage render --graph data/graph_demo.json --focus field:customer.email_id --depth 1
    lineage agent --graph data/graph_demo.json "Which PII fields does Customer have?"

All values are deterministic (seeded), obviously fictional and use reserved
ranges: @example.com emails, +91-90000-xxxxx phones, DEMO- prefixed ids.
Secrets/passwords are always masked — never plausible values.
"""
from __future__ import annotations

import copy
import json
import random
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from . import paths, tools

FIRST = ["Aarav", "Priya", "Rohan", "Ananya", "Vikram", "Meera",
         "Kabir", "Isha", "Arjun", "Sneha"]
LAST = ["Sharma", "Patel", "Mehta", "Iyer", "Khan", "Reddy", "Nair", "Gupta"]
CITIES = [("Bengaluru", "560001"), ("Mumbai", "400001"), ("Delhi", "110001"),
          ("Pune", "411001"), ("Chennai", "600001")]
MASK_PASSWORD = "<redacted-demo-password>"
SECRET_RE = re.compile(r"(PASSWORD|SECRET|TOKEN|API[_-]?KEY|CREDENTIAL)", re.I)
GENERIC_TYPES = ("network", "port", "file", "owner", "artifact", "build_stage",
                 "image", "env_var")


def _fieldname(node: dict) -> str:
    return node["name"].split(".", 1)[1] if "." in node["name"] else node["name"]


def _value(node: dict, rng: random.Random, idx: int) -> str:
    p = node["props"]
    fn = _fieldname(node)
    ft = p.get("fieldtype", "")
    opts = p.get("options")
    cats = p.get("categories") or []
    if ft == "Password":
        return MASK_PASSWORD
    if ft in ("Attach", "Attach Image"):
        return f"demo_attachment_{idx}.pdf"
    if "contact" in cats:
        if fn == "email_id" or "email" in fn or opts == "Email":
            return f"{rng.choice(FIRST).lower()}.{rng.choice(LAST).lower()}{idx}@example.com"
        return f"+91-90000-{idx:05d}"
    if "identity" in cats:
        if "pan" in fn:
            return f"DEMOP{idx:04d}K"
        if "gstin" in fn:
            return f"29DEMOP{idx:04d}K1ZQ"
        if "aadhaar" in fn:
            return f"0000-0000-{idx:04d}"
        if "passport" in fn:
            return f"P{idx:07d}"
        if fn in ("first_name", "middle_name"):
            return FIRST[idx % len(FIRST)]
        if fn == "last_name":
            return LAST[idx % len(LAST)]
        if "name" in fn:
            return f"{FIRST[idx % len(FIRST)]} {LAST[idx % len(LAST)]}"
        if fn in ("dob", "date_of_birth", "birth_date"):
            return (date(1990, 1, 1) + timedelta(days=idx * 137)).isoformat()
        return f"DEMO-TAX-{idx:04d}"
    if "location" in cats:
        city, pin = CITIES[idx % len(CITIES)]
        if "address" in fn:
            return f"{10 + idx}, MG Road"
        if "city" in fn:
            return city
        if fn == "state":
            return "Karnataka"
        return pin
    if "files" in cats:
        return f"demo_document_{idx}.pdf"
    if ft in ("Link", "Table", "Table MultiSelect") and isinstance(opts, str):
        letters = re.sub(r"[^A-Z]", "", opts.upper())[:4] or "DOC"
        return f"{letters}-DEMO-{idx:04d}"
    return f"DEMO-{fn}-{idx:03d}"


def _node_samples(node: dict, idx: int) -> str:
    t = node["type"]
    name = node["name"]
    if t == "volume":
        return f"/srv/demo/{name}/block_{idx}.dat"
    if t == "service":
        return f"running | demo replica {idx}"
    if t == "secret":
        return "***masked***"
    if t == "env_var":
        if SECRET_RE.search(name):
            return "***masked***"
        return f"demo-value-{idx}"
    if t == "role":
        return f"demo.user{idx}@example.com"
    if t == "report":
        return f"{40 + idx} rows | demo run 2026-10-0{idx + 1}"
    if t == "print_format":
        return f"printed {idx} times (demo)"
    if t == "job":
        return f"last run 2026-10-0{idx + 1} 02:00 UTC (demo)"
    return f"demo {t} data {idx}"


def generate(graph: dict, seed: int = 42, records: int = 3) -> dict:
    rng = random.Random(seed)
    out = {
        "meta": {
            "warning": "ALL VALUES ARE FICTIONAL (seeded demo data)",
            "seed": seed,
            "records_per_doctype": records,
            "source_commit": graph["meta"]["git_commit"],
        },
        "fields": {}, "doctypes": {}, "nodes": {},
    }

    fields = sorted((n for n in graph["nodes"] if n["type"] == "field"),
                    key=lambda n: n["id"])
    for f in fields:
        out["fields"][f["id"]] = {
            "fieldtype": f["props"].get("fieldtype"),
            "category": f["props"].get("categories") or [],
            "samples": [_value(f, rng, i + 1) for i in range(records)],
        }

    fields_by_doc: dict[str, list[dict]] = {}
    for f in fields:
        doc = f["id"].split(":", 1)[1].split(".", 1)[0]
        fields_by_doc.setdefault(doc, []).append(f)
    for dt in sorted((n for n in graph["nodes"]
                      if n["type"] == "doctype" and not n["props"].get("external")),
                     key=lambda n: n["id"]):
        s = dt["id"].split(":", 1)[1]
        doc_fields = sorted(fields_by_doc.get(s, []), key=lambda n: n["id"])
        if not doc_fields:
            continue
        recs = []
        for i in range(records):
            rec = {"name": f"DEMO-{s.upper()}-{i + 1:04d}"}
            for f in doc_fields:
                rec[_fieldname(f)] = _value(f, rng, i + 1)
            recs.append(rec)
        out["doctypes"][s] = {"records": recs}

    for n in graph["nodes"]:
        if n["type"] in ("field", "doctype"):
            continue
        out["nodes"][n["id"]] = {
            "samples": [_node_samples(n, i + 1) for i in range(min(records, 2))]
        }
    return out


def apply(graph: dict, data: dict) -> dict:
    g2 = copy.deepcopy(graph)
    fdata, ddata, ndata = data["fields"], data["doctypes"], data.get("nodes", {})
    for n in g2["nodes"]:
        if n["id"] in fdata:
            n["props"]["sample_values"] = fdata[n["id"]]["samples"][:5]
        elif n["type"] == "doctype":
            slug_ = n["id"].split(":", 1)[1]
            if slug_ in ddata:
                n["props"]["sample_records"] = ddata[slug_]["records"][:3]
        elif n["id"] in ndata:
            n["props"]["sample_values"] = ndata[n["id"]]["samples"][:3]
    g2["meta"]["demo_data"] = {"warning": "fictional sample data applied",
                               "source": data["meta"]["warning"]}
    return g2


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else ""
    opts: dict[str, str] = {}
    i = 1
    while i < len(argv):
        if argv[i].startswith("--") and i + 1 < len(argv):
            opts[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    graph_path = opts.get("graph")
    paths.ensure_data_dir()
    if mode == "generate":
        g = tools.load(graph_path)
        data = generate(g, seed=int(opts.get("seed", 42)),
                        records=int(opts.get("records", 3)))
        out = Path(opts.get("out", paths.DATA_DIR / "dummy_data.json"))
        out.write_text(json.dumps(data, indent=1), encoding="utf-8")
        print(f"{out} written: {len(data['fields'])} fields, "
              f"{len(data['doctypes'])} doctypes, {len(data['nodes'])} other nodes")
        return 0
    if mode == "apply":
        g = tools.load(graph_path)
        data = json.loads(Path(opts.get("data", paths.DATA_DIR / "dummy_data.json"))
                          .read_text(encoding="utf-8"))
        g2 = apply(g, data)
        out = Path(opts.get("out", paths.DATA_DIR / "graph_demo.json"))
        out.write_text(json.dumps(g2, indent=1), encoding="utf-8")
        populated = sum(1 for n in g2["nodes"]
                        if n["props"].get("sample_values")
                        or n["props"].get("sample_records"))
        print(f"{out} written: {populated} nodes populated with sample data")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
