"""Tiny local web dashboard (stdlib only) showing live stock status."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import config

log = logging.getLogger(__name__)

MAX_BODY = 4096


def serve(monitor, host: str, port: int) -> ThreadingHTTPServer:
    page = resources.files("duo_restock").joinpath("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page)
            elif self.path == "/api/status":
                self._json(200, monitor.snapshot())
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):  # noqa: N802 - http.server API
            actions = {
                "/api/settings": self._settings,
                "/api/watch": self._watch,
                "/api/pause": self._pause,
                "/api/check-now": lambda body: {"result": monitor.check_now()},
                "/api/test-notify": lambda body: {"result": monitor.test_notify()},
            }
            action = actions.get(self.path)
            if action is None:
                self._send(404, "text/plain", b"not found")
                return
            # Requiring a JSON body means another website open in the browser
            # cannot change settings with a plain cross-site form post.
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(415, {"error": "expected application/json"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._json(413, {"error": "request too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise TypeError
                extra = action(body)
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                self._json(400, {"error": "请求内容不完整"})
                return
            except config.ConfigError as e:
                self._json(400, {"error": str(e)})
                return
            except (OSError, ValueError) as e:
                self._json(500, {"error": f"保存 config.toml 失败：{e}"})
                return
            self._json(200, {**monitor.snapshot(), **extra})

        def _settings(self, body: dict) -> dict:
            interval = config.check_interval(body["interval_seconds"])
            active_hours = config.parse_active_hours(body.get("active_hours"))
            return {"outcome": monitor.update_settings(interval, active_hours)}

        def _watch(self, body: dict) -> dict:
            location = str(body["location"]).strip()
            if not location and not monitor.cfg.store:
                raise config.ConfigError("请填写邮编或地区")
            if len(location) > 100 or "\n" in location:
                raise config.ConfigError("邮编或地区格式不对")
            products = config.parse_products(body["products"], monitor.cfg.region)
            bark = body.get("bark_url")
            if bark is not None:
                bark = str(bark).strip()
                if not bark.startswith("https://") or len(bark) > 300 or "\n" in bark:
                    raise config.ConfigError("Bark 地址应该以 https:// 开头，例如 https://api.day.app/你的key")
            monitor.update_watch(location, products, bark)
            return {}

        def _pause(self, body: dict) -> dict:
            monitor.set_paused(bool(body["paused"]))
            return {}

        def _json(self, code: int, data: dict) -> None:
            self._send(code, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode())

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
