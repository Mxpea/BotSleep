"""L4/Observability MonitorServer: read-only loopback dashboard for BT branch, cache hit %, token ratio, competition."""

from __future__ import annotations

from collections import deque
import http.server
import json
import socketserver
import threading
import time
from typing import Any, Deque, Dict, Optional


_HTML_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>BotSleep v2 Monitor</title>
<style>body{font-family:monospace;background:#111;color:#eee;padding:16px}pre{background:#1c1c1c;padding:12px;border-radius:6px}</style>
</head><body><h2>BotSleep v2 Live Monitor (Read-Only)</h2><pre id="out">Loading...</pre>
<script>async function p(){const r=await fetch('/api/snapshot');document.getElementById('out').textContent=JSON.stringify(await r.json(),null,2)}p();setInterval(p,1500);</script>
</body></html>"""


class MonitorServer:
    """Loopback-only HTTP server serving live read-only v2 telemetry snapshots."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 18930, max_events: int = 100) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("MonitorServer host must be a loopback address")
        self.host = host
        self.port = int(port)
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {
            "engine": "v2",
            "updated_at": time.time(),
            "bt": {},
            "gateway": {"cache_hit_ratio": 0.0, "cache_hits": 0, "cache_misses": 0},
            "llm": {"confirmed_model": "未确认", "prompt_tokens": 0, "completion_tokens": 0, "ratio": 0.0},
            "pending_frozen": False,
            "competition": None,
        }
        self._events: Deque[Dict[str, Any]] = deque(maxlen=max_events)
        self._server: Optional[socketserver.TCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.address: str = ""

    def publish(self, event: Dict[str, Any]) -> None:
        with self._lock:
            self._events.append(event)
            self._state["updated_at"] = time.time()

    def update_snapshot(self, **sections: Any) -> None:
        with self._lock:
            self._state.update(sections)
            self._state["updated_at"] = time.time()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {**self._state, "recent_events": list(self._events)[-20:]}

    def start(self) -> "MonitorServer":
        mon = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                if self.path.startswith("/api/snapshot"):
                    body = json.dumps(mon.snapshot(), ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    body = _HTML_PAGE.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = _Server((self.host, self.port), Handler)
        actual_port = self._server.server_address[1]
        self.address = f"http://{self.host}:{actual_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, name="v2-monitor", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
