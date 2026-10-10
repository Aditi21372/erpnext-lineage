"""Unified CLI: `lineage <command>`.

    lineage build     rebuild data/graph.json from the pinned source repos
    lineage test      run the spec + vulnerability test suite (92 tests)
    lineage audit     adversarial audit of the artifact against the sources
    lineage query     tool CLI: summary | validate | trace | search_pii | ...
    lineage agent     ask a question (LLM tool loop; --plain for no-LLM)
    lineage chat      local chat UI: ask the graph, answers highlight nodes
    lineage render    export Mermaid / Graphviz DOT
    lineage views     write the six small HTML views to views/
    lineage page      write one self-contained interactive HTML page
    lineage dummy     generate / apply fictional demo data

Every command returns a non-zero exit code on failure, so CI can chain them.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from . import paths


def _cmd_build(args) -> int:
    from . import build
    repo = Path(args.frappe_docker) if args.frappe_docker else paths.frappe_docker_repo()
    erp = None if args.no_erpnext else (
        Path(args.erpnext) if args.erpnext else paths.erpnext_repo())
    if not (repo / "compose.yaml").exists() and not (repo / "pwd.yml").exists():
        print(f"error: {repo} does not look like a frappe_docker checkout "
              f"(run scripts/fetch_sources.sh or set FRAPPE_DOCKER_REPO)")
        return 1
    graph = build.build_graph(repo, erp)
    out = Path(args.out) if args.out else paths.ensure_data_dir() / "graph.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(graph, indent=1), encoding="utf-8")
    m = graph["meta"]
    print(f"{out} written: {m['node_count']} nodes, {m['edge_count']} edges "
          f"(frappe_docker {m['git_commit']}"
          + (f", erpnext {m.get('git_commits', {}).get('erpnext')}" if "git_commits" in m else "")
          + ")")
    return 0


def _cmd_test(args) -> int:
    spec_path = paths.PROJECT_ROOT / "tests" / "test_spec.py"
    spec = importlib.util.spec_from_file_location("lineage_test_spec", spec_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run(phase=args.phase, only=args.only)


def _cmd_audit(args) -> int:
    from . import audit
    return audit.main(args.graph)


def _cmd_query(args) -> int:
    from . import tools
    return tools.main(args.rest)


def _cmd_agent(args) -> int:
    from . import agent
    argv = list(args.question)
    if args.graph:
        argv += ["--graph", args.graph]
    if args.plain:
        argv.append("--plain")
    sys.argv = ["lineage agent"] + argv
    return agent.main()


def _cmd_chat(args) -> int:
    from . import chat
    argv = ["--host", args.host, "--port", str(args.port)]
    if args.graph:
        argv += ["--graph", args.graph]
    if args.no_open:
        argv.append("--no-open")
    return chat.main(argv)


def _cmd_render(args) -> int:
    from . import render
    argv = ["--format", args.format, "--depth", str(args.depth)]
    if args.focus:
        argv += ["--focus", args.focus]
    if args.graph:
        argv += ["--graph", args.graph]
    if args.out:
        argv += ["--out", args.out]
    return render.main(argv)


def _cmd_views(args) -> int:
    from . import views
    argv = ["--outdir", args.outdir]
    if args.graph:
        argv += ["--graph", args.graph]
    if args.no_mermaid:
        argv.append("--no-mermaid")
    return views.main(argv)


def _cmd_page(args) -> int:
    from . import viewer
    argv = []
    if args.graph:
        argv += ["--graph", args.graph]
    if args.out:
        argv += ["--out", args.out]
    if args.focus:
        argv += ["--focus", args.focus]
    return viewer.main(argv)


def _cmd_dummy(args) -> int:
    from . import dummy
    return dummy.main(args.rest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lineage", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="rebuild data/graph.json from the source repos")
    p.add_argument("--frappe-docker", help="path to frappe_docker (or $FRAPPE_DOCKER_REPO)")
    p.add_argument("--erpnext", help="path to erpnext (or $ERPNEXT_REPO)")
    p.add_argument("--no-erpnext", action="store_true", help="deployment layer only")
    p.add_argument("--out", help="output path (default data/graph.json)")
    p.set_defaults(fn=_cmd_build)

    p = sub.add_parser("test", help="run the spec + vulnerability tests")
    p.add_argument("--phase", help="only tests of one phase (p1..p6)")
    p.add_argument("--only", help="only tests whose name contains this")
    p.set_defaults(fn=_cmd_test)

    p = sub.add_parser("audit", help="adversarial audit of the artifact")
    p.add_argument("--graph", help="graph path (default data/graph.json)")
    p.set_defaults(fn=_cmd_audit)

    p = sub.add_parser("query", help="tool CLI (summary, validate, trace, ...)")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=_cmd_query)

    p = sub.add_parser("agent", help="ask a lineage question")
    p.add_argument("question", nargs=argparse.REMAINDER)
    p.add_argument("--graph", help="graph path (default data/graph.json)")
    p.add_argument("--plain", action="store_true", help="deterministic router, no LLM")
    p.set_defaults(fn=_cmd_agent)

    p = sub.add_parser("chat", help="local chat UI: ask the graph, answers highlight nodes")
    p.add_argument("--graph", help="graph path (default: demo graph if present)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.set_defaults(fn=_cmd_chat)

    p = sub.add_parser("render", help="export Mermaid / Graphviz DOT")
    p.add_argument("--format", choices=["mermaid", "dot"], default="mermaid")
    p.add_argument("--focus", help="node id to center the subgraph on")
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--graph", help="graph path")
    p.add_argument("--out", help="output file (default data/graph.mmd|.dot)")
    p.set_defaults(fn=_cmd_render)

    p = sub.add_parser("views", help="write the six small HTML views")
    p.add_argument("--graph", help="graph path (default: demo graph if present)")
    p.add_argument("--outdir", default=str(paths.VIEWS_DIR))
    p.add_argument("--no-mermaid", action="store_true", help="skip .mmd exports")
    p.set_defaults(fn=_cmd_views)

    p = sub.add_parser("page", help="single self-contained interactive HTML page")
    p.add_argument("--graph", help="graph path (default: demo graph if present)")
    p.add_argument("--out", default=str(paths.DATA_DIR / "graph.html"))
    p.add_argument("--focus", help="node id to suggest in the search box")
    p.set_defaults(fn=_cmd_page)

    p = sub.add_parser("dummy", help="generate / apply fictional demo data")
    p.add_argument("rest", nargs=argparse.REMAINDER,
                   help="generate|apply [--graph ...] [--out ...] [--seed N] [--records N]")
    p.set_defaults(fn=_cmd_dummy)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
