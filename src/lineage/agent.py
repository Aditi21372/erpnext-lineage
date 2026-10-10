"""LLM agent over the lineage graph: OpenAI-compatible tool-calling loop.

Usage:
    python agent.py "where is personal data stored?"
    python agent.py --plain "what can leave this system?"

Env: OPENAI_API_KEY, OPENAI_BASE_URL (default https://api.openai.com/v1),
OPENAI_MODEL (default gpt-4o-mini); OPENAI_SESSION and OPENAI_TIMEOUT
optionally override the session id and the request timeout (default 90s).
Without a key, falls back to the deterministic router (same tools, no LLM).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

from . import tools

# Stable per-process session id; OPENAI_SESSION overrides it per request.
_SESSION_ID = "lineage-" + uuid.uuid4().hex[:12]
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRY_SLEEPS = (0.8, 1.6)

SYSTEM_PROMPT = """You answer data-lineage questions about the frappe_docker deployment
and the ERPNext application (combined graph).

Node types: service, image, build_stage, volume, network, port, env_var, secret,
file, job, artifact, owner, doctype, field, role, report, print_format.
Edge types: mounts, reads_from, writes_to, connects_to, proxies_to, depends_on,
publishes, exposes, uses_secret, executes, backs_up, clones, builds_from, built_from,
configures, writes_config, joins_network, owns, uses_image,
contains (doctype->field / artifact->doctype), links_to (field->doctype),
contains_child, fetch_from (field->doctype), stored_in (doctype->volume:db-data,
Attach fields->volume:sites), permits (role->doctype), displays (print_format->doctype).

Key semantics:
- Volumes 'sites' and 'db-data' hold personal data; doctypes are stored in db-data
  (one MariaDB), file attachments in sites.
- Field nodes are materialized only for PII/Link/Table/fetch_from/Attach/Password;
  categories: contact, identity, location, credentials, files.
- Confidence is validated/parsed/inferred — say so for inferred edges (stored_in).
- Script reports (query_type=script) have reads_known=false: reads are unknown, say so.

Report tools:
- doctype_report(slug or display name, case-insensitive) answers "tell me about
  <DocType>" in one call: flags, materialized fields + PII, child tables,
  outbound/inbound links, role permissions, reports, print formats, storage.
- pii_report() summarizes PII/compliance across the whole graph: totals by
  category, top doctypes, attachments, password fields, searchable PII, email
  emitters, storage.
Prefer doctype_report for single-DocType questions and pii_report for PII or
compliance summaries. One report call usually answers the question — do not
re-fetch its data with follow-up list_edges/get_node calls; synthesize the
answer from the report result.

Rules:
1. ALWAYS call tools; never answer from memory.
2. Every claim must cite evidence (file:line) returned by the tools.
3. Secrets are masked; never attempt to reveal values.
4. Be concise: answer, then evidence list."""

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "graph_summary",
        "description": "Counts of node and edge types plus graph metadata.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "list_nodes",
        "description": "List nodes, optionally filtered by type, scenario, or name.",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string"},
            "scenario": {"type": "string"},
            "name_contains": {"type": "string"},
            "module": {"type": "string"},
            "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "list_edges",
        "description": "List edges, optionally filtered by type, src, or dst. "
                       "Use for permits/links_to/fetch_from/stored_in queries.",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string"},
            "src": {"type": "string"},
            "dst": {"type": "string"},
            "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "get_node",
        "description": "Get one node with its incoming/outgoing edges and evidence.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}}, "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "trace",
        "description": "BFS around a node up to depth; returns edges with direction.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"},
            "direction": {"type": "string", "enum": ["in", "out", "both"]},
            "depth": {"type": "integer"}}, "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "find_path",
        "description": "Shortest path between two nodes.",
        "parameters": {"type": "object", "properties": {
            "src": {"type": "string"}, "dst": {"type": "string"},
            "max_depth": {"type": "integer"}}, "required": ["src", "dst"]}}},
    {"type": "function", "function": {
        "name": "search_pii",
        "description": "Sensitive stores, secrets (with weak flags) and PII env vars.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "find_exits",
        "description": "Boundary crossings: published ports, host paths, backups, "
                       "docker.sock, secret files.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "taint",
        "description": "All nodes that can carry/hold data starting from a node "
                       "(undirected over data-flow edges).",
        "parameters": {"type": "object", "properties": {
            "start": {"type": "string"}}, "required": ["start"]}}},
    {"type": "function", "function": {
        "name": "reachable_from",
        "description": "Blast radius: who reaches this node (in) or what it reaches (out).",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"},
            "direction": {"type": "string", "enum": ["in", "out"]}},
            "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "scenario_report",
        "description": "Summary of one of the 18 compose scenarios (or 'pwd').",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "doctype_report",
        "description": "One-shot report on a DocType (slug or display name, "
                       "case-insensitive): flags, materialized fields + PII, "
                       "child tables, links in/out, role permissions, reports "
                       "reading it, print formats, physical storage, evidence.",
        "parameters": {"type": "object", "properties": {
            "doctype": {"type": "string",
                        "description": "DocType slug ('customer') or display name ('Customer')."}},
            "required": ["doctype"]}}},
    {"type": "function", "function": {
        "name": "pii_report",
        "description": "Compliance-style PII summary of the whole graph: PII field "
                       "totals by category, top doctypes, attachments, password "
                       "fields, searchable PII, email emitters, storage.",
        "parameters": {"type": "object", "properties": {}}}},
]

INT_ARGS = {"depth", "limit", "max_depth"}


def _focus_from_result(graph: dict, name: str, result) -> list[str]:
    """Node ids a tool result is about — used to highlight the graph in chat."""
    node_ids = {n["id"] for n in graph["nodes"]}
    ids: list[str] = []

    def add(v):
        if isinstance(v, str) and v in node_ids:
            ids.append(v)

    if name in ("get_node", "doctype_report") and isinstance(result, dict):
        add(result.get("id"))
    elif name == "pii_report" and isinstance(result, dict):
        for pair in result.get("top_doctypes") or []:
            if isinstance(pair, (list, tuple)) and pair:
                add(pair[0])
    elif name == "trace" and isinstance(result, dict):
        for i in result.get("reached_nodes") or []:
            add(i)
    elif name == "find_path" and isinstance(result, list):
        for i in result:
            add(i)
    elif name in ("search_pii", "find_exits"):
        for h in (result if isinstance(result, list) else []):
            if isinstance(h, dict):
                add(h.get("id") or h.get("node"))
    elif name in ("taint", "reachable_from", "list_nodes"):
        for n in (result if isinstance(result, list) else []):
            if isinstance(n, dict):
                add(n.get("id"))
    elif name == "list_edges":
        for e in (result if isinstance(result, list) else []):
            if isinstance(e, dict):
                add(e.get("src"))
                add(e.get("dst"))
    return ids


def _evidence_from_tool_result(graph: dict, result) -> list[dict]:
    """Pull (node, file, line) triples out of raw tool results for the chat UI."""
    node_ids = {n["id"] for n in graph["nodes"]}
    out: list[dict] = []
    items = result if isinstance(result, list) else [result]
    for item in items:
        if not isinstance(item, dict):
            continue
        node = item.get("node")
        if isinstance(node, dict) and node.get("id") in node_ids:
            nid, src = node["id"], node            # trace() nests the start node
        elif item.get("id") in node_ids:
            nid, src = item["id"], item            # get_node / node lists
        elif isinstance(node, str) and node in node_ids:
            nid, src = node, item                  # find_exits entries
        else:
            continue
        for ev in (src.get("evidence") or [])[:2]:
            out.append({"node": nid, **ev})
    return out


def dispatch(graph: dict, name: str, args: dict) -> dict | list:
    """Execute one tool call against the graph; returns JSON-safe data."""
    fn = getattr(tools, name, None)
    if fn is None or name not in {s["function"]["name"] for s in TOOL_SPECS}:
        return {"error": f"unknown tool: {name}"}
    kwargs = {}
    for key, value in (args or {}).items():
        if key in INT_ARGS and isinstance(value, str) and value.isdigit():
            value = int(value)
        kwargs[key] = value
    try:
        result = fn(graph, **kwargs)
    except (KeyError, TypeError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return json.loads(json.dumps(result, default=str))


def run_loop(graph: dict, question: str, llm, max_steps: int = 8,
             history: list[dict] | None = None) -> dict:
    """Agent loop. `llm(messages, tool_specs)` -> {tool_calls|content}.

    `history` is an optional list of {"role": "user"|"assistant", "content": str}
    turns inserted after the system prompt, before the current question.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in history or []:
        if isinstance(turn, dict) and turn.get("role") in ("user", "assistant") \
                and isinstance(turn.get("content"), str):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})
    used: list[str] = []
    focus: list[str] = []
    evidence: list[dict] = []
    for _ in range(max_steps):
        reply = llm(messages, TOOL_SPECS)
        tool_calls = reply.get("tool_calls") or []
        if not tool_calls:
            return {"answer": reply.get("content") or "", "tool_calls_used": used,
                    "focus": sorted(set(focus))[:300], "evidence": evidence[:20],
                    "transcript": messages}
        messages.append({"role": "assistant", "content": reply.get("content"),
                         "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call["function"]
            name = fn["name"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError as exc:
                result = {"error": f"invalid tool arguments for {name}: {exc}"}
            else:
                result = dispatch(graph, name, args)
            used.append(name)
            focus.extend(_focus_from_result(graph, name, result))
            if len(evidence) < 20:
                evidence.extend(_evidence_from_tool_result(graph, result))
            blob = json.dumps(result, default=str)
            if len(blob) > 8000:
                blob = blob[:8000] + " …[truncated: result exceeded 8KB]"
            messages.append({"role": "tool", "tool_call_id": call.get("id", name),
                             "name": name, "content": blob})
    return {"answer": "[stopped: max tool steps reached]", "tool_calls_used": used,
            "focus": sorted(set(focus))[:300], "evidence": evidence[:20],
            "transcript": messages}


def _upstream_error(data) -> str | None:
    """Message from a provider error envelope, or None for a normal payload."""
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if err is None and data.get("type") != "error":
        return None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or "unknown upstream error")
    if isinstance(err, str) and err:
        return err
    return str(data.get("message") or "unknown upstream error")


def _post_chat(url: str, body: bytes, headers: dict, timeout: float) -> bytes:
    """POST with retries on 429/500/502/503/504; sleep 0.8s then 1.6s."""
    req = urllib.request.Request(url, data=body, headers=headers)
    for attempt in range(len(_RETRY_SLEEPS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRY_STATUS and attempt < len(_RETRY_SLEEPS):
                time.sleep(_RETRY_SLEEPS[attempt])
                continue
            raise RuntimeError(f"LLM HTTP {exc.code}: {exc.read()[:300]!r}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"LLM unreachable: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"LLM request invalid: {exc}") from exc
    raise RuntimeError("LLM unreachable: retries exhausted")


def llm_openai(messages: list, tool_specs: list) -> dict:
    """One chat-completions call against any OpenAI-compatible endpoint."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    try:
        timeout = float(os.environ.get("OPENAI_TIMEOUT", "90"))
    except ValueError:
        timeout = 90.0
    session = os.environ.get("OPENAI_SESSION") or _SESSION_ID
    body = json.dumps({"model": model, "messages": messages, "tools": tool_specs,
                       "temperature": 0}).encode()
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {api_key}",
               "User-Agent": "lineage-agent/1.1",
               "x-opencode-session": session}
    raw = _post_chat(f"{base}/chat/completions", body, headers, timeout)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"LLM malformed response: {exc}") from exc
    err = _upstream_error(data)
    if err is not None:
        raise RuntimeError(f"LLM error: {err}")
    if not isinstance(data, dict) or "choices" not in data:
        raise RuntimeError("LLM malformed response: missing 'choices' in reply")
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM malformed response: {exc}") from exc
    if not isinstance(msg, dict):
        raise RuntimeError(f"LLM malformed response: message is {type(msg).__name__}")
    return {"content": msg.get("content"), "tool_calls": msg.get("tool_calls") or []}


# --------------------------------------------------- deterministic fallback

def answer_without_llm(graph: dict, question: str) -> dict:
    """Same tools, keyword-routed — keeps the demo alive without an API key."""
    q = question.lower()
    evidence: list[dict] = []
    tools_used: list[str] = []

    def collect(nodes_or_edges):
        for item in nodes_or_edges:
            # edges carry no node id of their own; anchor on the source node
            nid = item.get("src") or item.get("node") or item.get("id")
            for ev in item.get("evidence", [])[:2]:
                evidence.append({"node": nid, **ev})

    if any(w in q for w in ("credential", "secret", "password")):
        hits = [h for h in tools.search_pii(graph) if h["type"] == "secret"]
        tools_used = ["search_pii"]
        collect(hits)
        weak = [h for h in hits if any("weak=True" in r for r in h["reasons"])]
        answer = (f"{len(hits)} secret nodes; {len(weak)} weak credentials. "
                  f"Weak: {', '.join(h['id'] for h in weak)}. "
                  "Secrets appear in env vars, CLI flags (create-site), docker secret "
                  "files, and example.env.")
    elif any(w in q for w in ("leave", "exit", "out ", "egress", "boundary")):
        exits = tools.find_exits(graph)
        tools_used = ["find_exits"]
        collect([{"id": e["node"], "evidence": e["evidence"]} for e in exits])
        kinds = sorted({e["kind"] for e in exits})
        answer = (f"{len(exits)} boundary crossings: {', '.join(kinds)}. "
                  "Includes published ports, host bind mounts, docker.sock control "
                  "plane, backup job, and secret files.")
    elif "blast radius" in q or "db service" in q or "database service" in q:
        nodes_ = tools.reachable_from(graph, "service:db", direction="in")
        tools_used = ["reachable_from"]
        collect(nodes_)
        answer = ("Callers of service:db: " +
                  ", ".join(n["id"] for n in nodes_ if n["type"] == "service") +
                  ". They reach it via DB_HOST env refs and wait-for-it checks.")
    elif "8080" in q or "trace" in q or "request" in q:
        tr = tools.trace(graph, "port:frontend:8080", direction="in", depth=2)
        path = tools.find_path(graph, "port:frontend:8080", "volume:sites")
        tools_used = ["trace", "find_path"]
        collect([tr["node"]] + tr["edges"])
        answer = ("A request on :8080 enters via frontend nginx, proxies to backend "
                  f"(gunicorn :8000) and websocket (:9000). Path to storage: "
                  f"{' -> '.join(path)}.")
    elif "field" in q:
        hits = [h for h in tools.search_pii(graph) if h["type"] == "field"]
        if "customer" in q:
            hits = [h for h in hits if h["id"].startswith("field:customer.")]
        tools_used = ["search_pii"]
        collect(hits)
        scope = " on Customer" if "customer" in q else ""
        answer = (f"{len(hits)} PII-flagged fields{scope}; e.g. " +
                  ", ".join(h["label"] for h in hits[:8]) +
                  ". fetch_from chains (e.g. customer.email_id <- Contact.email_id) "
                  "show where values are derived from.")
    elif any(w in q for w in ("role", "permission", "export")):
        perm = tools.list_edges(graph, type="permits", limit=2000)
        exp = [e for e in perm if e["props"].get("export")]
        tools_used = ["list_edges"]
        collect(exp[:8])
        answer = (f"{len(perm)} role-doctype permission edges; {len(exp)} grant export. "
                  "Example: " + ", ".join(f"{e['src']} -> {e['dst']}" for e in exp[:5]) +
                  ".")
    elif "fetch" in q:
        ff = tools.list_edges(graph, type="fetch_from", limit=2000)
        tools_used = ["list_edges"]
        collect(ff[:8])
        answer = (f"{len(ff)} fetch_from chains (field values derived from another "
                  "doctype, e.g. customer.email_id <- Contact.email_id).")
    elif "physical" in q or "rest" in q or "stored in" in q or "storage" in q:
        db = tools.list_edges(graph, type="stored_in", dst="volume:db-data", limit=2000)
        files = tools.list_edges(graph, type="stored_in", dst="volume:sites", limit=2000)
        tools_used = ["list_edges"]
        collect(db[:6] + files[:6])
        answer = (f"{len(db)} doctypes are stored in the MariaDB volume db-data "
                  f"(inferred); {len(files)} attach fields land as files in the sites "
                  "volume. Both are mounted by backend/queue/scheduler containers.")
    elif "taint" in q or "personal" in q or "pii" in q:
        stores = [n for n in tools.search_pii(graph) if n["type"] == "volume"]
        tainted = []
        for s in stores:
            tainted.extend(tools.taint(graph, s["id"]))
        tools_used = ["search_pii", "taint"]
        collect(tainted)
        answer = (f"Starting from {[s['id'] for s in stores]}, "
                  f"{len({n['id'] for n in tainted})} nodes can carry that data — "
                  "every service mounting them, queues, and the backup job.")
    else:
        hits = tools.search_pii(graph)
        stores = [h for h in hits if h["type"] == "volume"]
        tools_used = ["search_pii", "taint"]
        tainted = [n for s in stores for n in tools.taint(graph, s["id"])]
        collect(stores + tainted)
        answer = ("Personal data rests in " + ", ".join(h["id"] for h in stores) +
                  ". Reachable by every service that mounts them (backend, frontend, "
                  "schedulers, queues, websocket) and the backup job.")

    focus = sorted({e["node"] for e in evidence if e.get("node")})
    return {"answer": answer, "evidence": evidence[:12], "tools_used": tools_used,
            "focus": focus[:300]}


def ask(graph: dict, question: str, history: list[dict] | None = None) -> dict:
    if os.environ.get("OPENAI_API_KEY"):
        try:
            return run_loop(graph, question, llm_openai, history=history)
        except RuntimeError as exc:
            fallback = answer_without_llm(graph, question)
            fallback["llm_error"] = str(exc)
            return fallback
    return answer_without_llm(graph, question)


def main() -> int:
    argv = sys.argv[1:]
    graph_path = None
    plain = "--plain" in argv
    if "--graph" in argv:
        i = argv.index("--graph")
        if i + 1 < len(argv):
            graph_path = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    question = " ".join(args)
    graph = tools.load(graph_path)
    if plain:
        result = answer_without_llm(graph, question)
    else:
        result = ask(graph, question)
    print(result["answer"])
    if result.get("evidence"):
        print("\nEvidence:")
        for ev in result["evidence"][:8]:
            print(f"  {ev.get('file')}:{ev.get('line')}  {ev.get('snippet', '')[:80]}")
    if result.get("tool_calls_used"):
        print(f"\n[tools: {', '.join(result['tool_calls_used'])}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
