"""Export the static site for GitHub Pages: landing + full graph + six views.

Everything except the chat is static — the viewer is self-contained (vendored
WebGL JS + embedded graph data) and needs no server. The chat sidebar talks to
a local HTTP API, so the hosted page links to the run instructions instead of
showing a broken panel.

Usage:
    lineage site [--graph data/graph_demo.json] [--outdir site]
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import paths, tools, viewer

LANDING = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Lineage — ERPNext + frappe_docker</title>
<style>
 :root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;
       --muted:#8b949e;--accent:#58a6ff;--ok:#3fb950;--warn:#d29922}
 *{box-sizing:border-box}
 body{margin:0;font:15px/1.6 ui-sans-serif,system-ui,"Segoe UI",sans-serif;
      background:var(--bg);color:var(--text)}
 header{padding:44px 32px 10px;max-width:1000px;margin:0 auto}
 h1{margin:0 0 6px;font-size:26px}
 .sub{color:var(--muted);font-size:14px;max-width:760px}
 .stats{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap}
 .stat{background:var(--panel);border:1px solid var(--border);border-radius:8px;
       padding:6px 12px;font:13px ui-monospace,Menlo,monospace;color:var(--accent)}
 .grid{max-width:1000px;margin:22px auto 60px;padding:0 32px;
       display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
 a.card{display:block;background:var(--panel);border:1px solid var(--border);
        border-radius:12px;padding:18px 20px;text-decoration:none;color:var(--text)}
 a.card:hover{border-color:#3d5670;background:#1a222c}
 .card h2{margin:0 0 6px;font-size:16px;color:var(--accent)}
 .card p{margin:0;color:var(--muted);font-size:13px}
 .tag{display:inline-block;margin-top:10px;font:11px ui-monospace,Menlo,monospace;
      border-radius:5px;padding:2px 8px;background:#22303f;color:#9fd0ff}
 .note{max-width:1000px;margin:0 auto 60px;padding:0 32px}
 .note .box{background:var(--panel);border:1px solid var(--border);
            border-left:3px solid var(--warn);border-radius:10px;padding:14px 18px;
            color:var(--muted);font-size:13.5px}
 .note code{background:#0b0f14;padding:2px 7px;border-radius:5px;color:#9fd0ff;
            font:12px ui-monospace,Menlo,monospace}
 a{color:var(--accent)}
</style></head><body>
<header>
 <h1>Lineage — ERPNext + frappe_docker</h1>
 <div class="sub">Evidence-backed static lineage graph built from two pinned
  upstream clones. Every node and edge carries <code>file:line</code> evidence,
  a confidence tier and a sensitivity flag. This hosted copy is fully static —
  the interactive chat agent runs locally with one Docker command.</div>
 <div class="stats">
  <span class="stat">%%NODES%% nodes</span>
  <span class="stat">%%EDGES%% edges</span>
  <span class="stat">erpnext @ %%ERPNEXT%%</span>
  <span class="stat">frappe_docker @ %%DOCKER%%</span>
 </div>
</header>
<div class="grid">
 <a class="card" href="graph.html">
  <h2>Full graph →</h2>
  <p>The complete interactive map: WebGL rendering at ~120 fps, semantic zoom
     (macro → fields), fuzzy live search, click any node to highlight its path,
     hover for evidence.</p>
  <span class="tag">interactive · 5.5k nodes</span>
 </a>
 <a class="card" href="views/pii-flow.html">
  <h2>PII flow →</h2>
  <p>All PII-bearing DocTypes with their entry points (API / import),
     relationships and the email exit sink, aggregated to stay readable.</p>
  <span class="tag">91 nodes</span>
 </a>
 <a class="card" href="views/customer-flow.html">
  <h2>Customer — one record's journey →</h2>
  <p>From the 8 PII fields on Customer, through fetch_from chains and the code
     that reads them, to storage, reports and who may email/export.</p>
  <span class="tag">29 nodes</span>
 </a>
 <a class="card" href="views/exits.html">
  <h2>Exits — boundary crossings →</h2>
  <p>Published ports, host bind mounts (incl. docker.sock), secrets, the
     backup job and every code site that sends email.</p>
  <span class="tag">48 nodes</span>
 </a>
 <a class="card" href="views/deployment.html">
  <h2>Deployment →</h2>
  <p>The frappe_docker runtime topology: services, volumes, networks, ports,
     secrets and scheduled jobs.</p>
  <span class="tag">91 nodes</span>
 </a>
 <a class="card" href="views/access.html">
  <h2>Access — roles × PII DocTypes →</h2>
  <p>Which roles hold read / write / export / email permissions on the
     DocTypes that contain PII fields.</p>
  <span class="tag">122 nodes</span>
 </a>
 <a class="card" href="views/structure.html">
  <h2>Structure — DocType relationships →</h2>
  <p>The advanced view: link / child / fetch_from edges between DocTypes plus
     reports and print formats.</p>
  <span class="tag">820 nodes</span>
 </a>
 <a class="card" href="https://github.com/Aditi21372/erpnext-lineage">
  <h2>Source + full README →</h2>
  <p>Parsers, 13 query tools, the grounded LLM agent, tests (98) and the
     adversarial evidence audit.</p>
  <span class="tag">github.com</span>
 </a>
</div>
<div class="note"><div class="box">
 <b>Chat with the graph (runs locally).</b> The chat agent needs a server for
 the tool loop and the LLM; this static copy cannot answer questions. Run it
 with one command and open <code>http://localhost:8765</code>:
 <br><br><code>docker compose up --build</code> &nbsp;(add your
 <code>OPENAI_API_KEY</code> in <code>.env</code> for LLM answers —
 any OpenAI-compatible endpoint; OpenCode Go verified)
</div></div>
</body></html>"""


def build(graph: dict, outdir: Path, views_src: Path | None = None) -> dict:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stats = {"pages": []}

    page = viewer.build_html(graph, "Lineage — full graph", chat=False)
    (outdir / "graph.html").write_text(page, encoding="utf-8")
    stats["pages"].append("graph.html")

    views_src = Path(views_src) if views_src else paths.VIEWS_DIR
    if (views_src / "index.html").exists():
        dst = outdir / "views"
        dst.mkdir(exist_ok=True)
        for f in sorted(views_src.glob("*.html")):
            shutil.copy(f, dst / f.name)
            stats["pages"].append(f"views/{f.name}")
        vend_src = views_src / "vendor"
        if vend_src.is_dir():
            vend_dst = dst / "vendor"
            vend_dst.mkdir(exist_ok=True)
            for f in sorted(vend_src.iterdir()):
                if f.is_file():
                    shutil.copy(f, vend_dst / f.name)
    else:
        stats["views_skipped"] = "run `lineage views` first to include the six views"

    meta = graph.get("meta", {})
    commits = meta.get("git_commits", {})
    landing = (LANDING
               .replace("%%NODES%%", f"{meta.get('node_count', 0):,}")
               .replace("%%EDGES%%", f"{meta.get('edge_count', 0):,}")
               .replace("%%ERPNEXT%%", str(commits.get("erpnext", "?"))[:7])
               .replace("%%DOCKER%%", str(commits.get("frappe_docker", "?"))[:7]))
    (outdir / "index.html").write_text(landing, encoding="utf-8")
    stats["pages"].insert(0, "index.html")
    return stats


def main(argv: list[str]) -> int:
    graph_path, outdir = None, str(paths.PROJECT_ROOT / "site")
    for i, a in enumerate(argv):
        if a == "--graph" and i + 1 < len(argv):
            graph_path = argv[i + 1]
        elif a == "--outdir" and i + 1 < len(argv):
            outdir = argv[i + 1]
    if graph_path is None:
        graph_path = str(paths.default_graph(prefer_demo=True))
    graph = tools.load(graph_path)
    stats = build(graph, Path(outdir))
    print(f"{outdir}/ written — {len(stats['pages'])} pages "
          f"from {graph_path}")
    for page in stats["pages"]:
        print(f"  {page}")
    if stats.get("views_skipped"):
        print(f"  note: {stats['views_skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
