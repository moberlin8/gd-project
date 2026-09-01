#!/usr/bin/env python3
"""
GD RAT Web API — HTTP wrapper for the GD RAT query interface.

Exposes the RAG query endpoint on port 9119.
Run via systemd: gd-rat-web.service

Endpoints:
  GET /health             — health check
  GET /search?q=<question>  — retrieve + synthesize answer from GD comments
"""

import json
import os
import sys
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Add index directory to path
INDEX_DIR = Path(__file__).parent
sys.path.insert(0, str(INDEX_DIR))
sys.path.insert(0, str(INDEX_DIR.parent))

from query_rat import load_index, search, respond

# Lazy-load index on first request
_index = None
_metadata = None
_setlists = None
_model = None


def get_index():
    """Lazy-load the FAISS index on first request."""
    global _index, _metadata, _setlists, _model
    if _index is None:
        _index, _metadata, _setlists, _model = load_index()
    return _index, _metadata, _model


class GDRatHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress default logging
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._respond(200, {"status": "ok", "service": "gd-rat-web"})
            return

        if path == "/search":
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0]

            if not query:
                self._respond(400, {"error": "Missing ?q=<question> parameter"})
                return

            try:
                index, metadata, model = get_index()
                results = search(index, metadata, model, query, k=10)
                answer = respond(query, results)
                self._respond(200, {
                    "query": query,
                    "answer": answer,
                    "num_results": len(results),
                    "timestamp": os.environ.get("GD_RAT_INDEX_TIME", "unknown"),
                })
            except Exception as e:
                self._respond(500, {"error": str(e)})
            return

        self._respond(404, {"error": "Not found. Use /health or /search?q=<question>"})

    def _respond(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def main():
    host = os.environ.get("GD_RAT_HOST", "0.0.0.0")
    port = int(os.environ.get("GD_RAT_PORT", "9119"))

    server = HTTPServer((host, port), GDRatHandler)
    print(f"GD RAT Web API listening on http://{host}:{port}")
    print(f"  /health — status check")
    print(f"  /search?q=<question> — query the GD knowledge base")
    server.serve_forever()


if __name__ == "__main__":
    main()
