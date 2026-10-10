"""Local chat UI over the lineage graph. Standard library only.

`lineage chat` serves one page: the interactive graph on the right, a chat
panel on the left. Questions go to the same grounded agent as `lineage agent`
(LLM tool loop when OPENAI_API_KEY is set, otherwise the deterministic
router); every answer carries evidence, and the nodes the answer is about
are highlighted in the graph.

Security: binds 127.0.0.1 by default. The HTTP API is read-only over an
in-memory graph dict — no filesystem, shell, or network egress is reachable
through it, and the graph contains no secrets (evidence snippets are redacted
at build time).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import agent, paths, tools, viewer


MAX_HISTORY_ITEMS = 8
MAX_HISTORY_CONTENT = 2000


def validate_history(history) -> list[dict]:
    """Validate an optional chat-history payload; raises ValueError when bad."""
    if not isinstance(history, list):
        raise ValueError("history must be a list of {role, content} objects")
    if len(history) > MAX_HISTORY_ITEMS:
        raise ValueError(f"history must have at most {MAX_HISTORY_ITEMS} items")
    out: list[dict] = []
    for i, item in enumerate(history):
        if not isinstance(item, dict):
            raise ValueError(f"history[{i}] must be an object")
        role = item.get("role")
        if role not in ("user", "assistant"):
            raise ValueError(f"history[{i}].role must be 'user' or 'assistant'")
        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError(f"history[{i}].content must be a string")
        if len(content) > MAX_HISTORY_CONTENT:
            raise ValueError(f"history[{i}].content exceeds "
                             f"{MAX_HISTORY_CONTENT} characters")
        out.append({"role": role, "content": content})
    return out


def build_response(graph: dict, question: str, history: list[dict] | None = None) -> dict:
    """Run the agent and shape the result for the chat UI."""
    result = agent.ask(graph, question, history=history)
    node_ids = {n["id"] for n in graph["nodes"]}
    evidence = []
    for ev in (result.get("evidence") or [])[:8]:
        evidence.append({"file": ev.get("file"), "line": ev.get("line"),
                         "snippet": ev.get("snippet", ""),
                         "nodes": [ev["node"]] if ev.get("node") in node_ids else []})
    mode = "router"
    if os.environ.get("OPENAI_API_KEY") and not result.get("llm_error"):
        mode = "llm"
    return {
        "answer": result.get("answer", ""),
        "tools": result.get("tool_calls_used") or result.get("tools_used") or [],
        "evidence": evidence,
        "focus": [f for f in (result.get("focus") or []) if f in node_ids][:120],
        "mode": mode,
        "llm_error": result.get("llm_error"),
    }


class _Handler(BaseHTTPRequestHandler):
    graph: dict = {}
    page: bytes = b""
    vendors: dict = {}
    views_dir: Path = Path(".")

    def log_message(self, *args):  # keep the console clean
        pass

    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_view(self, rel: str):
        """Serve generated view pages from views/ (path-traversal safe)."""
        target = (self.views_dir / rel).resolve()
        base = self.views_dir.resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            self._send(404, b'{"error":"not found"}')
            return
        ctype = ("text/html; charset=utf-8" if target.suffix == ".html"
                 else "text/plain; charset=utf-8")
        self._send(200, target.read_bytes(), ctype)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, self.page, "text/html; charset=utf-8")
        elif self.path.startswith("/vendor/"):
            name = self.path[len("/vendor/"):]
            body = self.vendors.get(name)
            if body is None:
                self._send(404, b'{"error":"not found"}')
            else:
                self._send(200, body, "application/javascript; charset=utf-8")
        elif self.path.startswith("/views/"):
            self._serve_view(self.path[len("/views/"):])
        elif self.path == "/api/meta":
            meta = dict(self.graph["meta"])
            meta["llm_enabled"] = bool(os.environ.get("OPENAI_API_KEY"))
            self._send(200, json.dumps(meta).encode("utf-8"))
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path != "/api/ask":
            self._send(404, b'{"error":"not found"}')
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            question = str(payload.get("question") or "").strip()
            if not question:
                self._send(400, json.dumps({"error": "empty question"}).encode())
                return
            history = payload.get("history")
            history = validate_history(history) if history is not None else None
            result = build_response(self.graph, question, history=history)
            self._send(200, json.dumps(result).encode("utf-8"))
        except (ValueError, TypeError, KeyError) as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode())
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill the server
            self._send(500, json.dumps(
                {"error": f"{type(exc).__name__}: {exc}"}).encode())


def make_server(graph: dict, host: str = "127.0.0.1", port: int = 8765
                ) -> ThreadingHTTPServer:
    """Build the server (port=0 picks a free port — used by the tests)."""
    page = viewer.build_html(graph, "Lineage chat — ask the graph", chat=True,
                             vendor_tag=viewer.vendor_tags("/vendor/"))
    vendors = {name: (viewer.VENDOR_DIR / name).read_bytes()
               for name in viewer.VENDOR_FILES
               if (viewer.VENDOR_DIR / name).exists()}
    handler = type("Handler", (_Handler,), {
        "graph": graph,
        "page": page.encode("utf-8"),
        "vendors": vendors,
        "views_dir": paths.VIEWS_DIR,
    })
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str]) -> int:
    host, port, graph_path, open_browser = "127.0.0.1", 8765, None, True
    for i, a in enumerate(argv):
        if a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
        elif a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a == "--graph" and i + 1 < len(argv):
            graph_path = argv[i + 1]
        elif a == "--no-open":
            open_browser = False
    graph = tools.load(graph_path or str(paths.default_graph(prefer_demo=True)))
    httpd = make_server(graph, host, port)
    url = f"http://{host}:{httpd.server_address[1]}/"
    mode = ("LLM tool loop (OPENAI_API_KEY set)"
            if os.environ.get("OPENAI_API_KEY")
            else "deterministic router — set OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL "
                 "and restart (docker: `docker compose up -d`) for LLM answers")
    print(f"lineage chat: {url}  —  {mode}; Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
