"""A bounded, read-only loopback dashboard for explicitly published events."""

import base64
import copy
import hashlib
import ipaddress
import json
import math
import re
import socket
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


REDACTED = "[redacted]"
SECRET_KEYS = {"password", "passwd", "pwd", "secret", "apikey", "accesstoken", "refreshtoken",
               "authtoken", "token", "authorization", "cookie", "setcookie", "clientsecret", "credentials"}
MAX_STRING = 512 * 1024


def _secret_key(key):
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized in SECRET_KEYS or normalized.endswith(("password", "passwd", "apikey", "secret", "token"))


class EventMonitor:
    """Thread-safe event bus. It never reads a log file, config, or environment."""

    def __init__(self, max_events=500, max_bytes=8 * 1024 * 1024):
        self.max_events = max(1, min(500, int(max_events)))
        self.max_bytes = max(4096, int(max_bytes))
        self._records = deque()
        self._bytes = 0
        self._seq = 0
        self._dropped = 0
        self._secrets = deque(maxlen=256)
        self._lock = threading.RLock()
        self._state = {
            "has_data": False, "last_seq": 0, "updated_at": None,
            "run": {"state": "waiting", "username": None, "planner": None, "model": None,
                    "steps": 0, "busy": False, "paused": False, "goal": None,
                    "phase": "idle", "pause_reason": None},
            "status": {}, "decision": None, "last_result": None,
            "llm": {"state": "idle", "request": None, "response": None, "error": None,
                    "usage": None, "elapsed_ms": None, "request_model": None, "actual_model": None},
        }

    def _find_secrets(self, value, depth=0):
        if depth > 20:
            return
        if isinstance(value, dict):
            for key, child in list(value.items())[:2048]:
                if _secret_key(key) and isinstance(child, str) and 4 <= len(child) <= 4096:
                    if child not in self._secrets:
                        self._secrets.append(child)
                else:
                    self._find_secrets(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value[:2048]:
                self._find_secrets(child, depth + 1)

    def _text(self, value):
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer " + REDACTED, value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", REDACTED, value)
        value = re.sub(r"(?i)(https?://)[^\s/?#@]+:[^\s/?#@]+@", r"\1[redacted]@", value)
        value = re.sub(r"(?i)([?&](?:api[_-]?key|password|secret|access_token|token|key)=)[^&\s#\"']+", r"\1[redacted]", value)
        value = re.sub(r"(?i)(\b(?:api[_-]?key|password|passwd|client_secret|access_token|refresh_token|authorization)\b[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)", r"\1[redacted]", value)
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "\n[monitor: text limit exceeded]"

    def _clean(self, value, depth=0):
        if depth > 20:
            return "[monitor: nesting limit exceeded]"
        if isinstance(value, dict):
            result = {}
            for key, child in list(value.items())[:2048]:
                cleaned_key = self._text(str(key))[:256]
                result[cleaned_key] = REDACTED if _secret_key(key) else self._clean(child, depth + 1)
            if len(value) > 2048:
                result["_monitor_truncated"] = "object field limit exceeded"
            return result
        if isinstance(value, (list, tuple)):
            result = [self._clean(child, depth + 1) for child in value[:2048]]
            if len(value) > 2048:
                result.append("[monitor: array limit exceeded]")
            return result
        if isinstance(value, str):
            return self._text(value)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        return f"[unsupported {type(value).__name__}]"

    def publish(self, record: dict) -> int:
        if not isinstance(record, dict):
            raise TypeError("Monitor records must be dictionaries")
        with self._lock:
            self._find_secrets(record)
            self._seq += 1
            cleaned = self._clean({
                "timestamp": record.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                "level": record.get("level", "INFO"), "module": record.get("module", "agent"),
                "event": record.get("event", "EVENT"), "data": record.get("data") or {},
            })
            cleaned["seq"] = self._seq
            encoded = json.dumps(cleaned, ensure_ascii=False).encode("utf-8")
            if len(encoded) > min(1024 * 1024, self.max_bytes):
                cleaned["data"] = {"_monitor_truncated": True,
                                   "message": "Published record exceeds the dashboard memory limit; inspect the redacted flight log."}
                encoded = json.dumps(cleaned, ensure_ascii=False).encode("utf-8")
            self._records.append((cleaned, len(encoded)))
            self._bytes += len(encoded)
            while len(self._records) > self.max_events or self._bytes > self.max_bytes:
                _, size = self._records.popleft()
                self._bytes -= size
                self._dropped += 1
            self._update_state(cleaned)
            return self._seq

    def _update_state(self, record):
        state = self._state
        state.update(has_data=True, last_seq=record["seq"], updated_at=record["timestamp"])
        data = record["data"] if isinstance(record["data"], dict) else {}
        event = str(record["event"]).upper()
        run = state["run"]
        if isinstance(data.get("status"), dict):
            state["status"] = data["status"]
        status = state["status"]
        for key in ("username", "planner", "model", "steps", "busy", "paused", "goal", "phase", "pause_reason"):
            if key in data:
                run[key] = data[key]
        if status.get("username"):
            run["username"] = status["username"]
        if event == "STARTUP":
            run["state"] = "starting"
        if event == "HEARTBEAT":
            run["state"] = ("completed" if data.get("completed") else "finished" if data.get("finished")
                            else "paused" if run["paused"] else "running")
            if "busy" not in data:
                run["busy"] = status.get("busy", False)
        if event == "AGENT_PHASE":
            run["busy"] = run["phase"] in {"observing", "thinking", "rechecking", "acting"}
            if not run["paused"]:
                run["state"] = "completed" if run["phase"] == "complete" else "running"
        if isinstance(data.get("decision"), dict):
            state["decision"] = data["decision"]
            run["goal"] = data["decision"].get("goal", run["goal"])
        if event in {"DECISION", "SLEEP_STARTED"}:
            name = (state["decision"] or {}).get("name", "auto_sleep")
            run.update(state="running", busy=name not in {"wait", "goal_complete"})
        if event in {"STEP_RESULT", "SLEEP_RESULT"}:
            state["last_result"] = data.get("result", data)
            if "step" in data:
                run["steps"] = data["step"]
            result = state["last_result"] if isinstance(state["last_result"], dict) else {}
            run.update(busy=False, state="paused" if run["paused"] else "running")
            result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
            if result_data.get("goal_complete"):
                run["state"] = "completed"
        if event in {"PAUSED", "RECOVERY_REQUIRED"}:
            run.update(paused=True, state="paused", phase="paused", pause_reason=data.get("reason"))
        elif event == "RESUMED":
            run.update(paused=False, state="running", phase="waiting", pause_reason=None)
        elif event == "SHUTDOWN":
            run.update(state="stopped", busy=False)
        elif event == "CRASH":
            run.update(state="error", busy=False)
        elif event == "BUDGET_EXHAUSTED":
            run.update(state="finished", busy=False)
        llm = state["llm"]
        if event == "LLM_REQUEST":
            llm.update(state="requesting", request=data, error=None,
                       request_model=data.get("model"), actual_model=None)
        elif event == "LLM_RESPONSE":
            response = data.get("response", data.get("raw_response", data if "choices" in data or "output" in data else {}))
            response = response if isinstance(response, dict) else {}
            actual_model = response.get("model")
            actual_model = actual_model.strip() if isinstance(actual_model, str) and actual_model.strip() else None
            llm.update(state="responded", response=data, error=None,
                       actual_model=actual_model,
                       usage=data.get("usage", response.get("usage")),
                       elapsed_ms=data.get("elapsed_ms", data.get("latency_ms", data.get("duration_ms"))))
        elif event == "LLM_ERROR":
            llm.update(state="error", error=data,
                       elapsed_ms=data.get("elapsed_ms", data.get("latency_ms")))

    def snapshot(self, since=0) -> dict:
        if isinstance(since, bool) or not isinstance(since, int) or since < 0:
            raise ValueError("since must be a non-negative integer")
        with self._lock:
            oldest = self._records[0][0]["seq"] if self._records else self._seq + 1
            reset = since > self._seq or (since != 0 and since < oldest - 1)
            records = [record for record, _ in self._records if reset or record["seq"] > since]
            return copy.deepcopy({"events": records, "last_seq": self._seq, "oldest_seq": oldest,
                                  "reset": reset, "dropped": self._dropped})

    def state(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)


class MonitorServer:
    """Serve only published events and the bundled dashboard on loopback."""

    def __init__(self, host="127.0.0.1", port=18930, log_dir=None):
        host = "127.0.0.1" if host == "localhost" else host
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError("Monitor host must be a loopback address") from None
        if not address.is_loopback:
            raise ValueError("Monitor only supports loopback binding")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Monitor port must be between 0 and 65535")
        self.host, self.port = host, port
        self.events = EventMonitor()
        self._server = None
        self._thread = None
        # log_dir is accepted for wiring compatibility, but no files are read.
        self._html = Path(__file__).with_name("monitor.html").read_text(encoding="utf-8")
        scripts = re.findall(r"<script>(.*?)</script>", self._html, re.S)
        hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'" for script in scripts]
        self._csp = ("default-src 'none'; script-src " + " ".join(hashes) + "; style-src 'unsafe-inline'; "
                     "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                     "frame-ancestors 'none'; object-src 'none'")

    @property
    def address(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def publish(self, record):
        return self.events.publish(record)

    def snapshot(self, since=0):
        return self.events.snapshot(since)

    def state(self):
        return self.events.state()

    def start(self):
        if self._server is not None:
            return self
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "BotSleepMonitor"
            sys_version = ""

            def log_message(self, *_):
                pass  # Never log request URLs, query values or authorization headers.

            def send_payload(self, status, value, content_type="application/json; charset=utf-8"):
                body = value.encode("utf-8") if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", owner._csp)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                allowed_hosts = {urlsplit(owner.address).netloc.lower(), f"localhost:{owner.port}"}
                if self.headers.get("Host", "").lower() not in allowed_hosts:
                    self.send_payload(403, {"error": "Host not permitted"})
                    return
                origin = self.headers.get("Origin")
                if origin and origin not in {owner.address, f"http://localhost:{owner.port}"}:
                    self.send_payload(403, {"error": "Origin not permitted"})
                    return
                parsed = urlsplit(self.path)
                if parsed.scheme or parsed.netloc:
                    self.send_payload(400, {"error": "Invalid request target"})
                elif parsed.path == "/" and not parsed.query:
                    self.send_payload(200, owner._html, "text/html; charset=utf-8")
                elif parsed.path == "/api/state" and not parsed.query:
                    self.send_payload(200, owner.state())
                elif parsed.path == "/api/events":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    value = query.get("since", ["0"])
                    if set(query) - {"since"} or len(value) != 1 or not re.fullmatch(r"\d{1,20}", value[0]):
                        self.send_payload(400, {"error": "Invalid event cursor"})
                    else:
                        self.send_payload(200, owner.snapshot(int(value[0])))
                else:
                    self.send_payload(404, {"error": "Not found"})

            def do_POST(self):
                self.send_payload(405, {"error": "Read-only monitor"})

            do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_POST

        server_type = ThreadingHTTPServer
        if ":" in self.host:
            class IPv6Server(ThreadingHTTPServer):
                address_family = socket.AF_INET6
            server_type = IPv6Server
        self._server = server_type((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=lambda: self._server.serve_forever(poll_interval=0.05), daemon=True, name="BotSleepMonitor")
        self._thread.start()
        return self

    def close(self):
        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None
