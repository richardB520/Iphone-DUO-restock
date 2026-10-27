"""Tiny local web dashboard (stdlib only) showing live stock status."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

log = logging.getLogger(__name__)


def serve(monitor, host: str, port: int) -> ThreadingHTTPServer:
    page = resources.files("duo_restock").joinpath("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page)
            elif self.path == "/api/status":
                body = json.dumps(monitor.snapshot(), ensure_ascii=False).encode()
                self._send(200, "application/json; charset=utf-8", body)
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.debug("dashboard: " + fmt, *args)

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("dashboard running at http://%s:%d", host, port)
    return server
