"""Filesystem layout: locate the read-only source repos and generated artifacts.

Source repos are looked up in this order (first hit wins):

1. environment override   FRAPPE_DOCKER_REPO / ERPNEXT_REPO
2. sibling checkout       <project>/../frappe_docker  /  ../erpnext   (dev layout)
3. fetched checkout       <project>/sources/frappe_docker  /  sources/erpnext
                          (scripts/fetch_sources.sh)

Generated artifacts live in ``data/``; rendered pages in ``views/``.
Both are disposable and rebuilt by the CLI (`lineage build`, `lineage views`).
"""
from __future__ import annotations

import os
from pathlib import Path


def _discover_root() -> Path:
    """Locate the project root in both layouts:

    - source checkout: walk up from cwd, then from this file
    - pip-installed package: the checkout is found via cwd or $LINEAGE_ROOT
      (site-packages must never be mistaken for the root)
    """
    env = os.environ.get("LINEAGE_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for base in (Path.cwd(), *here.parents):
        if (base / "pyproject.toml").is_file() and (base / "SCHEMA.md").is_file():
            return base
    return here.parents[2]


PROJECT_ROOT = _discover_root()
DATA_DIR = PROJECT_ROOT / "data"
VIEWS_DIR = PROJECT_ROOT / "views"


def find_repo(name: str, env: str) -> Path:
    override = os.environ.get(env)
    if override:
        return Path(override)
    for candidate in (PROJECT_ROOT.parent / name,
                      PROJECT_ROOT / "sources" / name,
                      PROJECT_ROOT / name):
        if candidate.is_dir():
            return candidate
    return PROJECT_ROOT / "sources" / name


def frappe_docker_repo() -> Path:
    return find_repo("frappe_docker", "FRAPPE_DOCKER_REPO")


def erpnext_repo() -> Path:
    return find_repo("erpnext", "ERPNEXT_REPO")


def default_graph(prefer_demo: bool = False) -> Path:
    """Checked-in callers pass nothing; prefer_demo picks demo-populated graph."""
    candidates = []
    if prefer_demo:
        candidates.append(DATA_DIR / "graph_demo.json")
    candidates += [DATA_DIR / "graph.json", PROJECT_ROOT / "graph.json"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return DATA_DIR / "graph.json"


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
