"""Generate a self-contained interactive HTML viewer for the lineage graph.

Rendering is Sigma.js v3 (WebGL) + Graphology, with a one-shot deterministic
d3-force layout computed in the browser — WebGL rendering stays at
because the graph is in the thousands of elements (5.5k nodes / 13.8k edges)
and pan/zoom must stay at 60fps.

UX:
- Semantic zoom (LOD): far out shows the macro layer only; closer shows code
  and reports; edge labels appear when the visible edge count is small.
- Deterministic layouts: chunked d3-force (seeded RNG) or a layered left→right
  placement (source → code → data → sink); both position sets are cached and
  toggleLR swaps instantly.
- Simple/Detail presets, Trace (1-3 hops), search-focus, type filters,
  hover detail card (structured DOM, never innerHTML), keyboard pan/zoom,
  aria-live status, empty state, reduced-motion support, optional FPS meter
  ('f' key or #fps).
- Chat variant (`chat=True`): a sidebar that queries `/api/ask` (with bounded
  conversation history) and highlights the answer's nodes in the graph.

Usage:
    lineage page                      # data/graph_demo.json if present, else graph.json
    lineage page --graph data/graph.json --out data/graph.html
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

from . import paths, tools

HERE = Path(__file__).resolve().parent
VENDOR_DIR = HERE / "vendor"
# load order matters: graphology first (Sigma needs it), then d3 modules
# in dependency order (dispatch, quadtree, timer, force).
VENDOR_FILES = ("graphology.umd.min.js", "sigma.min.js", "d3-dispatch.min.js",
                "d3-quadtree.min.js", "d3-timer.min.js", "d3-force.min.js")


def vendor_tags(src_prefix: str) -> str:
    """Script tags for an external vendor directory (views/ and chat server)."""
    return "".join(f'<script src="{src_prefix}{name}"></script>\n'
                   for name in VENDOR_FILES)


def inline_vendor() -> str:
    """Vendor JS embedded in the page (fully self-contained output)."""
    parts = []
    for name in VENDOR_FILES:
        path = VENDOR_DIR / name
        if path.exists():
            parts.append(f"<script>{path.read_text(encoding='utf-8')}</script>")
    return "\n".join(parts)


TYPE_COLORS = {
    "service": "#388bfd", "image": "#58a6ff", "build_stage": "#6e7681",
    "volume": "#d29922", "network": "#6e7681", "port": "#3fb950",
    "env_var": "#8b949e", "secret": "#f85149", "file": "#6e7681",
    "job": "#db6d28", "artifact": "#f0883e", "owner": "#39c5cf",
    "doctype": "#1f6feb", "field": "#79c0ff", "role": "#ffa657",
    "report": "#39c5cf", "print_format": "#56d364",
    "code": "#d29922", "api": "#8b949e", "source": "#3fb950", "sink": "#f85149",
}
TYPE_SIZES = {"doctype": 12, "service": 12, "volume": 10, "secret": 9,
              "role": 8, "report": 7, "print_format": 7, "field": 4,
              "source": 14, "sink": 11, "api": 3, "code": 5, "job": 8,
              "port": 7, "network": 7, "env_var": 6, "owner": 6}
HIDDEN_BY_DEFAULT = {"field", "env_var"}


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _join(v) -> str:
    if isinstance(v, (list, tuple)):
        return ", ".join(map(str, v))
    return str(v)


def _node_tip(n: dict) -> dict:
    p = n.get("props") or {}
    rows = []
    for key in ("module", "fieldtype", "options", "categories", "kind", "origin",
                "sensitivity", "weak", "schedule", "ref_doctype", "count",
                "files", "calls", "endpoints", "doctypes", "send_sites",
                "pii_count", "pii_fields", "external", "command"):
        if p.get(key) not in (None, "", [], False):
            rows.append([key, _join(p[key])[:160]])
    samples = [str(s) for s in (p.get("sample_values") or [])[:3]]
    rec = (p.get("sample_records") or [None])[0]
    if rec:
        samples.append("record: " + ", ".join(
            f"{k}={v}" for k, v in list(rec.items())[:4]))
    return {
        "title": f'{n["type"]}: {n["name"]}',
        "rows": rows[:7],
        "samples": samples,
        "ev": [f'{ev["file"]}:{ev["line"]}' for ev in n.get("evidence", [])[:2]],
    }


def _node_data(n: dict) -> dict:
    color = TYPE_COLORS.get(n["type"], "#8b949e")
    border, width = color, 1
    if n.get("sensitivity") == "likely":
        border, width = "#f85149", 3
    elif n.get("sensitivity") == "possible":
        border, width = "#d29922", 2
    elif n.get("boundary"):
        border, width = "#e6edf3", 2
    label = n["name"]
    pc = (n.get("props") or {}).get("pii_count")
    if pc:
        suff = f" · {pc} PII"
        if len(label) + len(suff) > 36:
            label = label[:36 - len(suff) - 1] + "…"
        label += suff
    elif len(label) > 33:
        label = label[:32] + "…"
    return {
        "id": n["id"], "label": label, "type": n["type"],
        "color": color, "border": border, "width": width,
        "size": (n.get("props") or {}).get("viz_size") or TYPE_SIZES.get(n["type"], 10),
        "tip": _node_tip(n),
    }


def _edge_data(e: dict) -> dict:
    return {
        "id": e["id"], "source": e["src"], "target": e["dst"], "type": e["type"],
        "label": e["type"], "inferred": e.get("confidence") == "inferred",
        "tip": {
            "title": f'{e["type"]} ({e.get("confidence", "parsed")})',
            "rows": ([["label", _join(e.get("label", ""))[:120]]] if e.get("label") else [])
                    + ([["count", _join(e.get("props", {}).get("count"))]]
                       if e.get("props", {}).get("count") else []),
            "samples": [],
            "ev": [f'{ev["file"]}:{ev["line"]}' for ev in e.get("evidence", [])[:2]],
        },
    }


def build_html(graph: dict, title: str, vendor_tag: str | None = None,
               default_hidden: set[str] | None = None,
               default_preset: str = "simple", caption: str = "",
               default_lr: bool = False, chat: bool = False) -> str:
    nodes = [_node_data(n) for n in graph["nodes"]]
    edges = [_edge_data(e) for e in graph["edges"]]
    data = json.dumps({"nodes": nodes, "edges": edges}).replace("</", "<\\/")
    types = sorted({n["type"] for n in graph["nodes"]})
    counts = {t: sum(1 for n in graph["nodes"] if n["type"] == t) for t in types}
    hidden = HIDDEN_BY_DEFAULT if default_hidden is None else default_hidden
    checks = "\n".join(
        f'<label class="chip"><input type="checkbox" class="typefilter" '
        f'value="{_esc(t)}"{" checked" if t not in hidden else ""}> '
        f'<span class="swatch" style="background:{TYPE_COLORS.get(t, "#8b949e")}"></span>'
        f'{_esc(t)} <em>{counts[t]}</em></label>' for t in types)
    caption_block = f'<div id="caption">{_esc(caption)}</div>' if caption else ""
    if vendor_tag is None:
        vendor_tag = inline_vendor()
    template = TEMPLATE
    for key, value in (("%%VENDOR_TAG%%", vendor_tag), ("%%DATA%%", data),
                       ("%%TITLE%%", _esc(title)), ("%%CHECKS%%", checks),
                       ("%%PRESET%%", default_preset),
                       ("%%CAPTION_BLOCK%%", caption_block),
                       ("%%DEFAULT_LR%%", "true" if default_lr else "false"),
                       ("%%CHAT_BLOCK%%", _CHAT_BLOCK if chat else ""),
                       ("%%CHAT_CSS%%", _CHAT_CSS if chat else ""),
                       ("%%CHAT_JS%%", _CHAT_JS if chat else ""),
                       ("%%NODES%%", str(len(nodes))),
                       ("%%EDGES%%", str(len(edges)))):
        template = template.replace(key, value)
    return template


_CHAT_BLOCK = """<aside id="chat" class="on">
 <div id="chead"><b>Ask the graph</b><span>grounded · every answer cites file:line · answers highlight nodes</span></div>
 <div id="msgs"><div class="msg bot">Ask about PII, storage, roles, code paths or exits.<br>
  Try: “Which PII fields does Customer have?” · “Who can export customer data?” · “What can leave this system?”</div></div>
 <form id="askform" autocomplete="off">
  <input id="askinput" placeholder="Ask about the graph…" aria-label="Question">
  <button id="askbtn" type="submit">Ask</button>
 </form>
</aside>"""

_CHAT_CSS = """
 #chat{width:392px;flex:none;border-right:1px solid var(--border);background:var(--panel);display:none;flex-direction:column}
 #chat.on{display:flex}
 #chead{padding:10px var(--s3);border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:2px}
 #chead b{font-size:14px}
 #chead span{color:var(--muted);font-size:11px}
 #msgs{flex:1;overflow-y:auto;padding:var(--s3);display:flex;flex-direction:column;gap:10px}
 .msg{max-width:100%;padding:8px 11px;border-radius:9px;font-size:12.5px;line-height:1.5;overflow-wrap:break-word}
 .msg.user{align-self:flex-end;background:#1f6feb;color:#fff}
 .msg.bot{align-self:flex-start;background:#0d1117;border:1px solid var(--border)}
 .msg .tools{color:var(--muted);font-size:11px;margin-top:6px}
 .evs{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}
 button.ev{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;background:#161b22;border:1px solid var(--border);border-radius:5px;color:#9fd0ff;padding:2px 6px;cursor:pointer}
 button.ev:hover{border-color:#3d5670}
 .err{color:var(--bad)}
 .warntxt{color:var(--warn);font-size:11px;margin-top:6px}
 #askform{display:flex;gap:6px;padding:10px var(--s3);border-top:1px solid var(--border)}
 #askinput{flex:1;min-width:0}
 @media (max-width: 900px){ #chat{width:46vw} }
"""

_CHAT_JS = """
const msgs = document.getElementById('msgs');
let chatHistory = [];
fetch('/api/meta').then(r => r.json()).then(m => {
  const head = document.getElementById('chead');
  if (!head) return;
  const chip = document.createElement('span');
  chip.id = 'llm-mode';
  chip.textContent = m.llm_enabled
    ? 'LLM: on'
    : 'LLM: off — deterministic router (set OPENAI_API_KEY and restart)';
  head.appendChild(chip);
}).catch(() => {});
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
function addMsg(cls,html){const d=document.createElement('div');d.className='msg '+cls;
  d.innerHTML=html;msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;return d;}
function pushHistory(role,content){chatHistory.push({role:role,
  content:String(content==null?'':content).slice(0,2000)});
  while(chatHistory.length>8)chatHistory.shift();}
async function ask(q){q=(q||'').trim();if(!q)return;
  addMsg('user',esc(q));
  const thinking=addMsg('bot','thinking…');
  try{
    const r=await fetch('/api/ask',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:q,history:chatHistory.slice(-8)})});
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    let html='<div>'+esc(d.answer).replace(/\\n/g,'<br>')+'</div>';
    if(d.mode||(d.tools&&d.tools.length))
      html+='<div class="tools">'+(d.mode?'mode: '+esc(d.mode)+' · ':'')
           +'tools: '+(d.tools||[]).map(esc).join(' → ')+'</div>';
    if(d.evidence&&d.evidence.length){html+='<div class="evs">';
      for(const e of d.evidence.slice(0,8)){
        html+='<button class="ev" data-nodes="'+esc((e.nodes||[]).join(','))+'" title="'+esc(e.file+':'+e.line)+'">'+esc(e.file+':'+e.line)+'</button>';}
      html+='</div>';}
    if(d.llm_error)
      html+='<div class="warntxt">LLM unavailable — answered by router: <i>'+esc(d.llm_error)+'</i></div>';
    thinking.innerHTML=html;
    pushHistory('user',q);
    pushHistory('assistant',d.answer);
    if(d.focus&&d.focus.length)focusNodes(d.focus);
    thinking.querySelectorAll('.ev').forEach(b=>{b.onclick=()=>{
      const ids=(b.dataset.nodes||'').split(',').filter(Boolean);
      if(ids.length)focusNodes(ids);};});
  }catch(err){thinking.innerHTML='<span class="err">'+esc(err)+'</span>';}}
document.getElementById('askform').addEventListener('submit',ev=>{
  ev.preventDefault();const el=document.getElementById('askinput');
  const q=el.value;el.value='';ask(q);});
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>%%TITLE%%</title>
<style>
 :root{
  --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3;
  --muted:#8b949e; --accent:#58a6ff; --ok:#3fb950; --warn:#d29922; --bad:#f85149;
  --r:8px; --s1:4px; --s2:8px; --s3:12px; --s4:16px;
 }
 *{box-sizing:border-box}
 html,body{height:100%}
 #app{display:flex;height:100vh}
 #main{flex:1;min-width:0;display:flex;flex-direction:column}
 body{margin:0;font:13px/1.45 ui-sans-serif,system-ui,"Segoe UI",sans-serif;
      background:var(--bg);color:var(--text)}
 .bar{display:flex;flex-wrap:wrap;gap:var(--s2);align-items:center;
      padding:var(--s2) var(--s3);background:var(--panel);
      border-bottom:1px solid var(--border)}
 .bar:last-of-type{gap:var(--s1)}
 .bar .grow{flex:1}
 button,input[type=text],select{
  font:inherit;color:var(--text);background:#0d1117;
  border:1px solid var(--border);border-radius:var(--r);padding:5px 10px}
 button{cursor:pointer;background:#21262d}
 button:hover{background:#2d333b;border-color:#454c54}
 button:focus-visible,input:focus-visible,select:focus-visible{
  outline:2px solid var(--accent);outline-offset:1px}
 .seg{display:inline-flex;border:1px solid var(--border);border-radius:var(--r);
      overflow:hidden}
 .seg button{border:0;border-radius:0;padding:5px 12px;background:transparent}
 .seg button.active{background:#1f6feb;color:#fff}
 #search{width:230px}
 .chip{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;
       border:1px solid var(--border);border-radius:999px;background:#0d1117;
       font-size:12px;white-space:nowrap}
 .chip input{margin:0;accent-color:var(--accent)}
 .chip em{color:var(--muted);font-style:normal}
 .swatch{display:inline-block;width:9px;height:9px;border-radius:50%}
 #legend{display:flex;gap:var(--s3);align-items:center;padding:6px var(--s3);
         color:var(--muted);font-size:12px;background:var(--panel);
         border-bottom:1px solid var(--border);flex-wrap:wrap}
 #legend .k{border-bottom:1px dotted var(--muted);cursor:help}
 #caption{padding:6px var(--s3);background:var(--panel);color:#aab7c4;font-size:12px;
          border-bottom:1px solid var(--border)}
 #status{color:var(--muted)}
 #stage{position:relative;flex:1;min-height:0}
 #net{position:absolute;inset:0;outline:none}
 #fps{position:absolute;top:8px;right:8px;z-index:40;display:none;
      background:rgba(13,17,23,.85);border:1px solid var(--border);border-radius:6px;
      padding:3px 8px;font:11px ui-monospace,SFMono-Regular,Menlo,monospace;
      color:var(--text);pointer-events:none}
 #empty{position:absolute;inset:0;display:none;place-items:center;text-align:center;
        color:var(--muted);background:rgba(13,17,23,.6);pointer-events:none}
 #empty.show{display:grid}
 #empty b{color:var(--text);display:block;font-size:15px;margin-bottom:var(--s1)}
 #tip{position:absolute;display:none;z-index:30;max-width:380px;
      background:var(--panel);border:1px solid var(--border);border-radius:var(--r);
      padding:8px 10px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
      color:var(--text);box-shadow:0 8px 24px rgba(0,0,0,.45);
      overflow-wrap:anywhere;word-break:break-word;pointer-events:none}
 #tip .ttitle{font-weight:600;font-family:ui-sans-serif,system-ui,sans-serif;margin-bottom:3px}
 #tip .trow b{color:var(--muted);font-weight:400}
 #tip .tev{color:var(--muted);font-size:11px}
 #tip .tsample{color:#9fd0ff;font-size:11px}
 @media (prefers-reduced-motion: reduce){ *{animation:none!important;transition:none!important} }
 @media (max-width: 760px){ #search{width:150px} }
 %%CHAT_CSS%%
</style></head><body>
<div id="app">
%%CHAT_BLOCK%%
<div id="main">
<div class="bar">
 <input id="search" type="text" aria-label="Focus a node"
        placeholder="focus node, e.g. doctype:customer">
 <button onclick="focusSearch()">Focus</button>
 <span class="seg" role="group" aria-label="Edge preset">
   <button onclick="setPreset('simple')" id="psimple">Simple</button>
   <button onclick="setPreset('detail')" id="pdetail">Detail</button>
 </span>
 <label class="chip"><input type="checkbox" id="trace"
   onchange="toggleTrace()"> Trace</label>
 <select id="depth" aria-label="Trace depth" onchange="traceDepth=+this.value;applyAll()">
   <option>1</option><option selected>2</option><option>3</option>
 </select>
 <button onclick="toggleLR()" id="lrbtn" title="Left-to-right layered flow layout">Layout: force</button>
 <button onclick="resetView()" title="Clear focus, trace and filters">Reset</button>
 <button onclick="fitAll()" title="Fit whole view">Fit</button>
 <span class="grow"></span>
 <span id="status" role="status" aria-live="polite">Loading…</span>
</div>
<div class="bar">%%CHECKS%%</div>
<div id="legend">
 <span class="k" title="Validated/parsed edges are brighter; regex-inferred edges are dimmer">— bright validated · dim inferred</span>
 <span class="k" title="Node rings: red = PII likely, amber = possible, white = boundary">◍ red PII likely · amber possible · white boundary</span>
 <span>scroll = zoom (macro → detail) · drag = pan · hover = details · click node = highlight its path</span>
</div>
%%CAPTION_BLOCK%%
<div id="stage">
 <div id="net" tabindex="0" aria-label="Lineage graph. Arrow keys pan, plus/minus zoom."></div>
 <div id="fps" aria-hidden="true"></div>
 <div id="tip" role="tooltip"></div>
 <div id="empty"><div><b>Nothing visible</b>Enable more node types above or press Reset.<br>(%%NODES%% nodes, %%EDGES%% edges in this view)</div></div>
</div>
</div>
</div>
%%VENDOR_TAG%%
<script>
const GRAPH = %%DATA%%;
const ALL_NODES = GRAPH.nodes, ALL_EDGES = GRAPH.edges;
const REDUCE = matchMedia('(prefers-reduced-motion: reduce)').matches;
// ---------------------------------------------------------------- model
const STRUCT_EDGES = new Set(["contains","contains_child","permits","configures",
  "owns","wires","joins_network","uses_image","built_from","builds_from",
  "clones","executes"]);
const MACRO = new Set(["doctype","service","source","sink","volume","network"]);
const EDGE_LOD_HIDE = new Set(["reads_from","writes_to","emits_email",
  "fetch_from","stored_in"]);
const LAYOUT_EXCLUDE = new Set(["emits_email", "wires", "configures", "owns"]);
const NODE_BY_ID = new Map(ALL_NODES.map(n => [n.id, n]));
const LAYOUT_LINKS = ALL_EDGES.filter(e => !LAYOUT_EXCLUDE.has(e.type)
  && e.source !== e.target && NODE_BY_ID.has(e.source) && NODE_BY_ID.has(e.target));
const LINK_DIST = {contains:70, contains_child:80, links_to:240, fetch_from:140,
  stored_in:320, reads_from:190, writes_to:190, displays:60, permits:150,
  mounts:120, connects_to:120, depends_on:100, proxies_to:100, publishes:60,
  uses_secret:80, executes:80, backs_up:220, joins_network:80, uses_image:60,
  writes_config:80};
const LDEG = new Map();
for (const e of LAYOUT_LINKS){
  LDEG.set(e.source, (LDEG.get(e.source) || 0) + 1);
  LDEG.set(e.target, (LDEG.get(e.target) || 0) + 1);
}
const ADJ = {};
ALL_EDGES.forEach(e => { (ADJ[e.source] = ADJ[e.source] || []).push(e);
                         (ADJ[e.target] = ADJ[e.target] || []).push(e); });
const EDGE_COLOR = "rgba(139,148,158,0.30)";
const EDGE_INFERRED = "rgba(110,118,129,0.16)";
const EDGE_HILITE = "rgba(88,166,255,0.85)";
const MACRO_EDGE_COLOR = "rgba(88,166,255,0.38)";
const DIM_NODE = "rgba(139,148,158,0.14)";
const MIN_RATIO = 0.05, MAX_RATIO = 10;

let preset = "%%PRESET%%", traceOn = false, traceDepth = 2, selectedId = null,
    selectedIds = new Set(), lrOn = %%DEFAULT_LR%%, booted = false,
    focusSet = null, hoveredId = null,
    lod = ALL_NODES.length > 2500 ? 0 : 1,
    highlightNodes = null, highlightEdges = null, FIT_RATIO = null,
    searchSet = null, searchNote = '';

// Deterministic RNG (LCG) — layout must be reproducible across reloads.
function lcg(seed){
  let s = seed >>> 0;
  return function(){ s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
}

// ---------------------------------------------------------------- graphology
function withAlpha(color, a){
  const m = /^#([0-9a-f]{6})$/i.exec(color || '');
  if (!m) return color;
  const v = parseInt(m[1], 16);
  return 'rgba(' + (v >> 16 & 255) + ',' + (v >> 8 & 255) + ',' + (v & 255) + ',' + a + ')';
}
const graph = new graphology.MultiGraph();
(function(){
  const rnd = lcg(7);
  for (const n of ALL_NODES){
    graph.addNode(n.id, {x: (rnd() - .5) * 1800, y: (rnd() - .5) * 1800,
      label: n.label, color: withAlpha(n.color, 0.82), size: n.size, border: n.border,
      width: n.width, type: n.type, tip: n.tip});
  }
  for (const e of ALL_EDGES){
    try {
      const key = graph.hasEdge(e.id) ? e.id + '#' + graph.size : e.id;
      graph.addEdgeWithKey(key, e.source, e.target, {label: e.label,
        inferred: e.inferred, type: 'line', tip: e.tip,
        color: e.inferred ? EDGE_INFERRED : EDGE_COLOR, size: 1});
    } catch (err) { /* dangling edge — skip, never break the page */ }
  }
})();

// Derived doctype-level aggregate edges for the macro layer: field->doctype
// links roll up to doctype->doctype so the macro view shows real structure
// even though field nodes are hidden at that zoom level.
const MACRO_EDGE_META = [];
(function(){
  const docIds = new Set(ALL_NODES.filter(n => n.type === 'doctype').map(n => n.id));
  const allowTarget = id => docIds.has(id) || id === 'volume:db-data' || id === 'volume:sites';
  const ownerDoc = id => id.startsWith('field:')
    ? 'doctype:' + id.slice(6).split('.')[0] : id;
  const agg = new Map();
  for (const e of ALL_EDGES){
    if (e.type !== 'links_to' && e.type !== 'fetch_from'
        && e.type !== 'contains_child' && e.type !== 'stored_in') continue;
    const s = ownerDoc(e.source), t = ownerDoc(e.target);
    if (!docIds.has(s) || !allowTarget(t) || s === t) continue;
    const k = s + '|' + t;
    const r = agg.get(k) || {s, t, n: 0, kinds: new Set()};
    r.n++; r.kinds.add(e.type); agg.set(k, r);
  }
  for (const r of agg.values()){
    if (!graph.hasNode(r.s) || !graph.hasNode(r.t)) continue;
    const kinds = [...r.kinds].sort().join('+');
    const id = 'macro:' + r.s + '->' + r.t;
    try {
      graph.addEdgeWithKey(id, r.s, r.t, {
        macro: true, type: 'line', label: kinds + ' ×' + r.n,
        color: MACRO_EDGE_COLOR,
        size: 0.7 + Math.min(4, Math.log2(1 + r.n) * 0.9),
        tip: {title: kinds + ' ×' + r.n,
              rows: [['from', r.s], ['to', r.t], ['kinds', kinds]],
              samples: [], ev: []}});
      MACRO_EDGE_META.push({id, s: r.s, t: r.t});
    } catch (err) { /* never break the page */ }
  }
})();

// Sigma has no pixelRatio setting in this build; its renderer reads
// window.devicePixelRatio at construction. Force 1 on large graphs.
if (ALL_NODES.length > 2500){
  try { Object.defineProperty(window, 'devicePixelRatio',
        {configurable: true, get(){ return 1; }}); } catch (err) {}
}

// Node program with a PII/boundary ring (rest of the node filled by type color).
const BorderCircle = Sigma.rendering.createNodeBorderProgram({
  borders: [
    {size: {attribute: 'width', mode: 'pixels'}, color: {attribute: 'border'}},
    {size: {fill: true}, color: {attribute: 'color'}}
  ]});

// ---------------------------------------------------------------- view state
let visNodeSet = new Set(), labelSet = new Set(), visEdgeSet = new Set();
let visNodeCount = 0, visEdgeCount = 0, edgeLabelsOn = false;

function checkedTypes(){
  return new Set([...document.querySelectorAll('.typefilter:checked')].map(c => c.value));
}
function neighborhood(id, depth){
  const seen = new Set([id]); let frontier = [id];
  for (let d = 0; d < depth; d++){
    const nxt = [];
    for (const cur of frontier) for (const e of ADJ[cur] || []){
      const other = e.source === cur ? e.target : e.source;
      if (!seen.has(other)){ seen.add(other); nxt.push(other); }
    }
    frontier = nxt;
  }
  return seen;
}

function computeVisibility(){
  const types = checkedTypes();
  let traceIds = null;
  if (traceOn && selectedId) traceIds = neighborhood(selectedId, traceDepth);
  const selNeigh = selectedId ? neighborhood(selectedId, 1) : null;
  const nodes = new Set();
  for (const n of ALL_NODES){
    let ok = types.has(n.type);
    if (ok && traceIds && !traceIds.has(n.id)) ok = false;
    if (ok && focusSet && !focusSet.has(n.id)) ok = false;
    if (ok && lod === 0 && !MACRO.has(n.type)) ok = false;
    if (ok) nodes.add(n.id);
  }
  if (searchSet){
    // search view: matches + their 1-hop context only (matches are forced
    // visible even if their type chip or LOD would hide them)
    const ring = new Set();
    for (const id of searchSet){
      ring.add(id);
      for (const x of neighborhood(id, 1)) if (nodes.has(x)) ring.add(x);
    }
    nodes.clear();
    for (const id of ring) nodes.add(id);
  }
  const fewLabels = nodes.size <= 700;   // airy views: label every visible node
  const labels = new Set();
  for (const n of ALL_NODES){
    if (!nodes.has(n.id)) continue;
    if (fewLabels || MACRO.has(n.type) || n.id === hoveredId
        || selectedIds.has(n.id) || (selNeigh && selNeigh.has(n.id)))
      labels.add(n.id);
  }
  const edges = new Set();
  for (const e of ALL_EDGES){
    let ok = preset === 'detail' || !STRUCT_EDGES.has(e.type);
    if (ok && lod === 0 && EDGE_LOD_HIDE.has(e.type)) ok = false;
    if (ok && (!nodes.has(e.source) || !nodes.has(e.target))) ok = false;
    if (ok) edges.add(e.id);
  }
  if (lod === 0){
    for (const m of MACRO_EDGE_META)
      if (nodes.has(m.s) && nodes.has(m.t)) edges.add(m.id);
  }
  // Click highlight: build the path (selected node + N-hop neighborhood,
  // edges between highlighted nodes) so the rest can be dimmed instead of hidden.
  highlightNodes = null; highlightEdges = null;
  if (selectedId && ADJ[selectedId]){
    const hn = new Set([selectedId]);
    for (const id of neighborhood(selectedId, 1)) if (nodes.has(id)) hn.add(id);
    const he = new Set();
    for (const e of ALL_EDGES){
      if (nodes.has(e.source) && nodes.has(e.target)
          && hn.has(e.source) && hn.has(e.target)) he.add(e.id);
    }
    if (lod === 0){
      for (const m of MACRO_EDGE_META)
        if (hn.has(m.s) && hn.has(m.t)) he.add(m.id);
    }
    if (hn.size > 1){ highlightNodes = hn; highlightEdges = he; }
  }
  if (searchSet){
    const hn = new Set();
    for (const id of searchSet) if (nodes.has(id)) hn.add(id);
    const he = new Set();
    for (const e of ALL_EDGES){
      if (nodes.has(e.source) && nodes.has(e.target)
          && hn.has(e.source) && hn.has(e.target)) he.add(e.id);
    }
    if (lod === 0){
      for (const m of MACRO_EDGE_META)
        if (hn.has(m.s) && hn.has(m.t)) he.add(m.id);
    }
    highlightNodes = hn; highlightEdges = he;
  }
  visNodeSet = nodes; labelSet = labels; visEdgeSet = edges;
  visNodeCount = nodes.size; visEdgeCount = edges.size;
  edgeLabelsOn = visEdgeCount > 0 && visEdgeCount <= 400;
}
computeVisibility();

// ---------------------------------------------------------------- tooltip
const tipEl = document.getElementById('tip');
const stageEl = document.getElementById('stage');
function hideTip(){ tipEl.style.display = 'none'; }
function showTip(tip, vp){
  if (!tip) return;
  tipEl.textContent = '';
  const title = document.createElement('div');
  title.className = 'ttitle'; title.textContent = tip.title;
  tipEl.appendChild(title);
  for (const [k, v] of tip.rows || []){
    const row = document.createElement('div'); row.className = 'trow';
    const b = document.createElement('b'); b.textContent = k + ': ';
    row.appendChild(b); row.appendChild(document.createTextNode(v));
    tipEl.appendChild(row);
  }
  for (const s of tip.samples || []){
    const d = document.createElement('div'); d.className = 'tsample';
    d.textContent = 'sample: ' + s; tipEl.appendChild(d);
  }
  for (const e of tip.ev || []){
    const d = document.createElement('div'); d.className = 'tev';
    d.textContent = e; tipEl.appendChild(d);
  }
  tipEl.style.display = 'block';
  const W = stageEl.clientWidth, H = stageEl.clientHeight;
  const px = vp ? vp.x : 0, py = vp ? vp.y : 0;
  let x = px + 16, y = py + 12;
  const tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
  if (x + tw > W - 8) x = Math.max(8, px - tw - 16);
  if (y + th > H - 8) y = Math.max(8, py - th - 12);
  tipEl.style.left = x + 'px'; tipEl.style.top = y + 'px';
}

// ---------------------------------------------------------------- sigma
// Reducers map our lineage attrs onto Sigma display data. `type` is remapped to
// the registered program names (circle/line) — lineage types are not programs.
function nodeReducer(id, data){
  const d = Object.assign({}, data);
  d.type = 'circle';
  if (!visNodeSet.has(id)){ d.hidden = true; return d; }
  d.label = labelSet.has(id) ? (data.label || '') : '';
  if (highlightNodes && !highlightNodes.has(id)){
    d.color = DIM_NODE;            // dim, don't hide — keep context visible
    d.label = '';
  }
  if (selectedIds.has(id) || id === hoveredId) d.size = (data.size || 10) * 1.3;
  return d;
}
function edgeReducer(id, data){
  const d = Object.assign({}, data);
  d.type = 'line';   // spec: no arrowheads in Sigma v3; inferred edges stay dim
  if (data.macro
      && (lod !== 0 || !visNodeSet.has(data.source) || !visNodeSet.has(data.target))){
    d.hidden = true; return d;
  }
  if (!visEdgeSet.has(id)){ d.hidden = true; return d; }
  d.label = edgeLabelsOn ? (data.label || '') : '';
  if (highlightEdges){
    if (highlightEdges.has(id)){ d.color = EDGE_HILITE; d.size = Math.max(1.7, data.size || 1); return d; }
    d.color = 'rgba(139,148,158,0.05)'; d.size = 0.5; d.label = '';
    return d;
  }
  if (data.macro){ d.color = MACRO_EDGE_COLOR; return d; }
  d.color = data.inferred ? EDGE_INFERRED : EDGE_COLOR;
  d.size = data.inferred ? 0.8 : 1.1;
  return d;
}
const sigma = new Sigma(graph, document.getElementById('net'), {
  renderLabels: true, renderEdgeLabels: true,
  hideEdgesOnMove: true, hideLabelsOnMove: true,
  enableEdgeEvents: true,
  minCameraRatio: MIN_RATIO, maxCameraRatio: MAX_RATIO,
  labelFont: 'ui-sans-serif, system-ui, "Segoe UI", sans-serif',
  labelSize: 11, labelWeight: '500', labelColor: {color: '#e6edf3'},
  labelDensity: 0.6, labelGridCellSize: 110, labelRenderedSizeThreshold: 3,
  edgeLabelFont: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  edgeLabelSize: 9, edgeLabelWeight: 'normal',
  edgeLabelColor: {color: '#8b949e'},
  defaultEdgeColor: '#8b949e', defaultNodeColor: '#8b949e',
  nodeProgramClasses: {circle: BorderCircle},
  nodeReducer: nodeReducer, edgeReducer: edgeReducer,
  zIndex: false});

window.__lineage = {sigma, graph, applyAll, focusNodes, fitTo};

// ---------------------------------------------------------------- camera/lod
function applyLOD(){
  if (!FIT_RATIO) return;
  const z = sigma.getCamera().getState().ratio;
  let next = lod;
  if (z > FIT_RATIO * 1.3) next = 0;        // far out: macro layer only
  else if (z < FIT_RATIO * 0.85) next = 1;  // zoomed in: everything
  if (next !== lod){ lod = next; applyAll(); }
}
let lodTimer = null;
sigma.getCamera().on('updated', () => {
  if (REDUCE){ applyLOD(); return; }
  clearTimeout(lodTimer); lodTimer = setTimeout(applyLOD, 160);
});

// ---------------------------------------------------------------- apply
function applyAll(){
  if (!booted) return;
  computeVisibility();
  sigma.refresh({schedule: true});
  document.getElementById('empty').classList.toggle('show', visNodeCount === 0);
  const lodName = lod === 0 ? 'macro' : 'full';
  document.getElementById('status').textContent =
    visNodeCount + ' nodes / ' + visEdgeCount + ' edges · ' + lodName +
    (traceOn && selectedId ? ' · trace ×' + traceDepth : '') +
    (highlightNodes && !searchSet ? ' · path ×' + highlightNodes.size : '') +
    (searchNote ? ' · ' + searchNote : '') +
    (focusSet ? ' · focused ×' + focusSet.size : '') + (lrOn ? ' · LR' : '');
}

// ---------------------------------------------------------------- layout
const POS = {force: null, lr: null};
let forceRunning = false;

function positionsOf(simNodes){
  const m = {};
  for (const n of simNodes) m[n.id] = {x: n.x, y: n.y};
  return m;
}
function runForceLayout(onDone){
  if (forceRunning) return;
  forceRunning = true;
  const big = ALL_NODES.length > 900;
  // Layout the ~2600 non-field nodes only: fields are placed in tight
  // deterministic orbits around their owner afterwards. Letting 2953 tiny
  // field nodes participate tramples the visible structure (collision +
  // leaf springs compress the doctype layer into a ball).
  const simNodes = ALL_NODES.filter(n => n.type !== 'field').map(n => ({id: n.id}));
  const simIds = new Set(simNodes.map(n => n.id));
  const simLinks = LAYOUT_LINKS.filter(e => simIds.has(e.source) && simIds.has(e.target));
  const rnd = lcg(42);
  for (const n of simNodes){ n.x = (rnd() - .5) * 2400; n.y = (rnd() - .5) * 2400; }
  // Dispersion model (Springy/Obsidian-like, tuned for hub-heavy lineage data):
  //  - per-edge-type rest length (links are long, attachments short),
  //  - link strength capped by 1/min-degree so hubs (the 600-edge volume,
  //    Account, Customer) cannot crush the whole graph into a hairball,
  //  - strong many-body repulsion (hub-scaled) + weak center pull,
  //  - collision radius keeps labels from overlapping.
  const sim = d3.forceSimulation(simNodes)
    .randomSource(rnd)
    .force('link', d3.forceLink(simLinks.map(e => ({
        source: e.source, target: e.target, type: e.type})))
      .id(d => d.id)
      .distance(l => LINK_DIST[l.type] || 160)
      .strength(l => {
        const dmin = Math.min(LDEG.get(l.source.id) || 1, LDEG.get(l.target.id) || 1);
        const cap = (l.type === 'contains' || l.type === 'displays') ? 0.85
                  : (l.type === 'stored_in' ? 0.05 : 0.3);
        return Math.min(1 / dmin, cap);
      }))
    .force('charge', d3.forceManyBody()
      .strength(d => -(big ? 100 : 110) - (big ? 34 : 24) * Math.sqrt(LDEG.get(d.id) || 0))
      .distanceMax(big ? 2800 : 1600).theta(0.9))
    .force('x', d3.forceX(0).strength(big ? 0.010 : 0.02))
    .force('y', d3.forceY(0).strength(big ? 0.010 : 0.02))
    .force('collide', d3.forceCollide(big ? 14 : 16))
    .stop();
  const total = big ? 320 : 300;
  const chunkSize = big ? 8 : 20;
  let tick = 0;
  (function chunk(){
    const end = Math.min(total, tick + chunkSize);   // small chunks keep it responsive
    for (; tick < end; tick++) sim.tick();
    if (tick < total){
      document.getElementById('status').textContent = 'computing layout…';
      setTimeout(chunk, 0);
    } else {
      sim.stop();
      POS.force = positionsOf(simNodes);
      placeFields(POS.force, rnd);
      forceRunning = false;
      onDone();
    }
  })();
}

function placeFields(posMap, rnd){
  // Deterministic golden-angle spiral per owner doctype: fields orbit close,
  // so zooming in (full LOD) reveals each doctype's field ring.
  const FA = 2.399963;
  const byOwner = new Map();
  for (const e of ALL_EDGES){
    if (e.type !== 'contains') continue;
    if (!byOwner.has(e.source)) byOwner.set(e.source, []);
    byOwner.get(e.source).push(e.target);
  }
  for (const [owner, fids] of byOwner){
    const o = posMap[owner];
    if (!o) continue;
    fids.sort();
    for (let i = 0; i < fids.length; i++){
      if (posMap[fids[i]]) continue;
      const r = 15 + 5.5 * Math.sqrt(i + 1);
      const a = i * FA + 0.7;
      posMap[fids[i]] = {x: o.x + r * Math.cos(a), y: o.y + r * Math.sin(a)};
    }
  }
  for (const n of ALL_NODES)   // safety net: never leave a node unpositioned
    if (!posMap[n.id]) posMap[n.id] = {x: (rnd() - .5) * 2600, y: (rnd() - .5) * 2600};
}

function computeLR(){
  // Deterministic layered placement: BFS levels along directed layout edges
  // from in-degree-0 roots; x = level·240, y centered within the level.
  const adj = new Map(), indeg = new Map();
  for (const n of ALL_NODES){ adj.set(n.id, []); indeg.set(n.id, 0); }
  for (const e of LAYOUT_LINKS){
    adj.get(e.source).push(e.target);
    indeg.set(e.target, indeg.get(e.target) + 1);
  }
  const level = new Map(), queue = [];
  for (const n of ALL_NODES) if (indeg.get(n.id) === 0){ level.set(n.id, 0); queue.push(n.id); }
  for (let qi = 0; qi < queue.length; qi++){
    const cur = queue[qi], lv = level.get(cur);
    for (const t of adj.get(cur))
      if (!level.has(t)){ level.set(t, lv + 1); queue.push(t); }
  }
  let maxL = 0;
  level.forEach(l => { if (l > maxL) maxL = l; });
  for (const n of ALL_NODES){   // cycle leftovers: BFS after the deepest column
    if (level.has(n.id)) continue;
    const q2 = [n.id]; level.set(n.id, maxL + 1);
    for (let i = 0; i < q2.length; i++){
      const cur = q2[i], lv = level.get(cur);
      for (const t of adj.get(cur))
        if (!level.has(t)){ level.set(t, lv + 1); q2.push(t); }
    }
  }
  const cols = new Map();
  for (const n of ALL_NODES){
    const lv = level.get(n.id) || 0;
    if (!cols.has(lv)) cols.set(lv, []);
    cols.get(lv).push(n.id);
  }
  const pos = {};
  for (const lv of [...cols.keys()].sort((a, b) => a - b)){
    const arr = cols.get(lv);
    arr.forEach((id, i) => { pos[id] = {x: lv * 240, y: (i - (arr.length - 1) / 2) * 46}; });
  }
  return pos;
}

let GBOX = null;
function computeGBox(){
  let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
  graph.forEachNode(id => { const a = graph.getNodeAttributes(id);
    if (a.x < x1) x1 = a.x; if (a.x > x2) x2 = a.x;
    if (a.y < y1) y1 = a.y; if (a.y > y2) y2 = a.y; });
  if (!isFinite(x1)){ x1 = y1 = 0; x2 = y2 = 1; }
  const o = Math.max(x2 - x1, y2 - y1) || 1;
  return {cx: (x1 + x2) / 2, cy: (y1 + y2) / 2, o};
}
function applyPositions(map){
  graph.updateEachNodeAttributes((id, attrs) => {
    const p = map[id];
    if (p){ attrs.x = p.x; attrs.y = p.y; }
    return attrs;
  });
  GBOX = computeGBox();
  sigma.refresh({schedule: true});
}
GBOX = computeGBox();

function runLayout(mode){
  document.getElementById('status').textContent = 'computing layout…';
  const finish = () => {
    booted = true;
    applyAll();
    fitAll();
  };
  if (mode === 'lr'){
    if (!POS.lr) POS.lr = computeLR();
    applyPositions(POS.lr);
    finish();
  } else {
    if (POS.force){ applyPositions(POS.force); finish(); return; }
    runForceLayout(() => {
      if (lrOn || !POS.force) return;   // user switched to LR meanwhile
      applyPositions(POS.force);
      finish();
    });
  }
}

// ---------------------------------------------------------------- viewport
function fitTo(ids, pad){
  ids = (ids || []).filter(id => graph.hasNode(id));
  if (!ids.length || !GBOX) return;
  let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
  for (const id of ids){ const a = graph.getNodeAttributes(id);
    if (a.x < x1) x1 = a.x; if (a.x > x2) x2 = a.x;
    if (a.y < y1) y1 = a.y; if (a.y > y2) y2 = a.y; }
  pad = pad || 40;
  const dim = sigma.getDimensions();
  const h = Math.max(1, Math.min(dim.width, dim.height));
  const FW = (x2 - x1) / GBOX.o, FH = (y2 - y1) / GBOX.o;
  let ratio = Math.max((FW * h) / Math.max(1, dim.width - 2 * pad),
                       (FH * h) / Math.max(1, dim.height - 2 * pad));
  ratio = Math.min(MAX_RATIO, Math.max(0.1, ratio));
  const target = {x: 0.5 + ((x1 + x2) / 2 - GBOX.cx) / GBOX.o,
                  y: 0.5 + ((y1 + y2) / 2 - GBOX.cy) / GBOX.o, ratio};
  if (REDUCE) sigma.getCamera().setState(target);
  else sigma.getCamera().animate(target, {duration: 500, easing: 'quadraticInOut'});
  return ratio;
}
function fitAll(){
  const ids = [...visNodeSet];
  const r = fitTo(ids.length ? ids : graph.nodes(), 40);
  if (r) FIT_RATIO = r;   // calibrates the macro/full zoom thresholds
}

// ---------------------------------------------------------------- interactions
function refreshNode(id){
  if (!id || !sigma) return;
  try { sigma.refresh({partialGraph: {nodes: [id]}, skipIndexation: true, schedule: true}); }
  catch (err) { sigma.refresh({schedule: true}); }
}
function edgeMidVp(key){
  let ends;
  try { ends = graph.extremities(key); } catch (err) { return null; }
  const a = graph.getNodeAttributes(ends[0]), b = graph.getNodeAttributes(ends[1]);
  if (!a || !b) return null;
  const p1 = sigma.graphToViewport({x: a.x, y: a.y});
  const p2 = sigma.graphToViewport({x: b.x, y: b.y});
  return {x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2};
}
sigma.on('enterNode', ({node}) => {
  hoveredId = node;
  const a = graph.getNodeAttributes(node);
  showTip(a ? a.tip : null, a ? sigma.graphToViewport({x: a.x, y: a.y}) : null);
  labelSet.add(node);
  refreshNode(node);
});
sigma.on('leaveNode', ({node}) => {
  hideTip();
  if (hoveredId !== node) return;
  hoveredId = null;
  computeVisibility();
  refreshNode(node);
});
sigma.on('enterEdge', ({edge}) => {
  const a = graph.getEdgeAttributes(edge);
  if (a) showTip(a.tip, edgeMidVp(edge));
});
sigma.on('leaveEdge', hideTip);
sigma.on('clickNode', ({node}) => {
  selectedIds = new Set([node]); selectedId = node;
  searchSet = null; searchNote = '';
  applyAll();
});
sigma.on('clickStage', () => {
  let changed = false;
  if (focusSet){ focusSet = null; changed = true; }
  if (selectedId){ selectedIds = new Set(); selectedId = null; changed = true; }
  if (searchSet){ searchSet = null; searchNote = ''; changed = true; }
  if (changed) applyAll();
});
sigma.getCamera().on('updated', hideTip);

// ---------------------------------------------------------------- controls
function setPreset(p){
  preset = p;
  document.getElementById('psimple').className = p === 'simple' ? 'active' : '';
  document.getElementById('pdetail').className = p === 'detail' ? 'active' : '';
  applyAll();
}
function toggleTrace(){
  traceOn = document.getElementById('trace').checked; applyAll();
}
function toggleLR(){
  lrOn = !lrOn;
  document.getElementById('lrbtn').textContent = 'Layout: ' + (lrOn ? 'LR' : 'force');
  runLayout(lrOn ? 'lr' : 'force');
}

function revealAndShow(ids){
  let changed = false;
  for (const id of ids){
    const n = NODE_BY_ID.get(id);
    if (!n) continue;
    const cb = document.querySelector('.typefilter[value="' + n.type + '"]');
    if (cb && !cb.checked){ cb.checked = true; changed = true; }
    if (lod === 0 && !MACRO.has(n.type)){ lod = 1; changed = true; }
  }
  if (changed) applyAll();
  computeVisibility();   // fresh sets even when applyAll was skipped (pre-boot)
  return ids.filter(id => visNodeSet.has(id));
}
function focusNodes(ids){
  focusSet = null;   // a new answer replaces the previous focus
  const sel = revealAndShow(ids || []);
  if (!sel.length) return;
  selectedIds = new Set(sel); selectedId = sel[0];
  const focused = new Set();
  for (const id of sel) for (const x of neighborhood(id, 1)) focused.add(x);
  focusSet = focused;
  applyAll();
  fitTo(sel, 130);
}
function searchScore(q, n){
  const id = n.id.toLowerCase();
  const label = String(n.label || '').toLowerCase();
  if (id === q || label === q) return 1000;
  let score = 0;
  if (id.includes(q)) score = Math.max(score, 300 - Math.min(120, id.length * 0.1));
  if (label.includes(q)) score = Math.max(score, 260);
  if (id.endsWith(q)) score += 40;
  const toks = q.split(/\s+/).filter(Boolean);
  if (toks.length > 1){
    const hay = id + ' ' + label;
    if (toks.every(t => hay.includes(t))) score = Math.max(score, 220 + toks.length * 10);
  }
  const name = id.replace(/^[a-z_]+:/, '');
  let qi = 0, streak = 0, fz = 0;
  for (let i = 0; i < name.length && qi < q.length; i++){
    if (name[i] === q[qi]){ qi++; streak++; fz += 4 + streak * 2; }
    else streak = 0;
  }
  if (qi === q.length) score = Math.max(score, 60 + Math.min(80, fz * 0.4));
  return score;
}
function applySearch(q){
  if (!q){
    if (searchSet){ searchSet = null; searchNote = ''; applyAll(); }
    return;
  }
  const scored = [];
  for (const n of ALL_NODES){
    const s = searchScore(q, n);
    if (s > 0) scored.push([s, n.id]);
  }
  if (!scored.length){
    searchNote = "search '" + q + "': 0 matches";
    if (searchSet){ searchSet = null; }
    applyAll();
    return;
  }
  scored.sort((a, b) => b[0] - a[0] || (a[1] < b[1] ? -1 : 1));
  const top = scored.slice(0, 60).map(x => x[1]);
  searchSet = new Set(top);
  searchNote = "search '" + q + "': " + scored.length + ' matches (top ' + top.length + ' shown)';
  applyAll();
  fitTo(top, 70);
}
function focusSearch(){ applySearch(document.getElementById('search').value.trim().toLowerCase()); }
let searchTimer = null;
document.getElementById('search').addEventListener('keydown',
  e => { if (e.key === 'Enter'){ clearTimeout(searchTimer); focusSearch(); } });
document.getElementById('search').addEventListener('input', e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => applySearch(e.target.value.trim().toLowerCase()), 300);
});
function resetView(){
  document.getElementById('trace').checked = false; traceOn = false;
  document.getElementById('search').value = '';
  document.querySelectorAll('.typefilter').forEach(c => { c.checked = true; });
  selectedIds = new Set(); selectedId = null; focusSet = null; hoveredId = null;
  searchSet = null; searchNote = '';
  lod = 1;
  applyAll();
  fitAll();
}

document.getElementById('net').addEventListener('keydown', e => {
  const cam = sigma.getCamera(), st = cam.getState();
  const dim = sigma.getDimensions();
  const h = Math.max(1, Math.min(dim.width, dim.height));
  const step = 60 * st.ratio / h;
  if (e.key === '+' || e.key === '=') cam.setState({ratio: Math.min(MAX_RATIO, Math.max(MIN_RATIO, st.ratio / 1.2))});
  else if (e.key === '-') cam.setState({ratio: Math.min(MAX_RATIO, Math.max(MIN_RATIO, st.ratio * 1.2))});
  else if (e.key === 'ArrowLeft') cam.setState({x: st.x - step});
  else if (e.key === 'ArrowRight') cam.setState({x: st.x + step});
  else if (e.key === 'ArrowUp') cam.setState({y: st.y + step});
  else if (e.key === 'ArrowDown') cam.setState({y: st.y - step});
  else return;
  e.preventDefault();
});

// ---------------------------------------------------------------- fps meter
const fpsEl = document.getElementById('fps');
let fpsOn = false, fpsFrames = 0, fpsLast = 0, fpsRaf = 0;
function fpsLoop(ts){
  if (!fpsOn){ fpsRaf = 0; return; }
  fpsFrames++;
  if (ts - fpsLast >= 500){
    fpsEl.textContent = Math.round(fpsFrames * 1000 / Math.max(1, ts - fpsLast)) + ' fps';
    fpsFrames = 0; fpsLast = ts;
  }
  fpsRaf = requestAnimationFrame(fpsLoop);
}
function toggleFps(on){
  fpsOn = (typeof on === 'boolean') ? on : !fpsOn;
  fpsEl.style.display = fpsOn ? 'block' : 'none';
  if (fpsOn){
    fpsFrames = 0; fpsLast = performance.now();
    fpsEl.textContent = '… fps';
    if (!fpsRaf) fpsRaf = requestAnimationFrame(fpsLoop);
  } else if (fpsRaf){ cancelAnimationFrame(fpsRaf); fpsRaf = 0; }
}
document.addEventListener('keydown', e => {
  const t = e.target;
  if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
  if (e.key === 'f' || e.key === 'F') toggleFps();
});
if (location.hash === '#fps') toggleFps(true);
window.addEventListener('hashchange', () => toggleFps(location.hash === '#fps'));

// ---------------------------------------------------------------- boot
setPreset(preset);
if (lrOn){
  document.getElementById('lrbtn').textContent = 'Layout: LR';
  runLayout('lr');
} else {
  runLayout('force');
}
%%CHAT_JS%%
</script>
</body></html>"""


def main(argv: list[str]) -> int:
    graph_path, out, focus = None, str(paths.DATA_DIR / "graph.html"), None
    for i, a in enumerate(argv):
        if a == "--graph" and i + 1 < len(argv):
            graph_path = argv[i + 1]
        elif a == "--out" and i + 1 < len(argv):
            out = argv[i + 1]
        elif a == "--focus" and i + 1 < len(argv):
            focus = argv[i + 1]
    if graph_path is None:
        graph_path = str(paths.default_graph(prefer_demo=True))
    graph = tools.load(graph_path)
    if not (VENDOR_DIR / "sigma.min.js").exists():
        print("WARNING: vendor/sigma.min.js missing — viewer will be empty")
    html_text = build_html(graph, f"Lineage — {graph_path}")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(html_text, encoding="utf-8")
    print(f"{out} written from {graph_path} "
          f"({round(len(html_text) / 1e6, 1)} MB, self-contained).")
    if focus:
        print(f"Search box: paste '{focus}' and press Enter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
