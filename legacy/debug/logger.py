"""Flight Recorder Structured Logger for BotSleep Actions and Telemetry."""

import json
import os
import time
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional


class FlightRecorder:
    def __init__(self, log_dir: str = "logs", *, secrets=(), sink=None, console=False, enabled=True):
        self.log_dir = log_dir
        self.enabled = enabled
        self.sink = sink
        self.console = console
        self._secrets = {value for value in secrets if isinstance(value, str) and value}
        self._lock = threading.RLock()
        self.session_id = uuid.uuid4().hex[:12]
        if enabled:
            os.makedirs(self.log_dir, exist_ok=True)
        date_str = time.strftime("%Y%m%d")
        self.log_file = os.path.join(self.log_dir, f"flight_{date_str}.jsonl")

    def add_secret(self, value):
        if isinstance(value, str) and value:
            with self._lock:
                self._secrets.add(value)

    def redact(self, value):
        if isinstance(value, dict):
            return {str(key): "[redacted]" if str(key).lower() in {
                "api_key", "password", "authorization", "token", "access_token", "secret", "headers"
            } else self.redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
            return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[redacted]", value)
        return value

    def record(self, level: str, module: str, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Appends a structured JSONL record to the flight log."""
        with self._lock:
            record = self.redact({
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "session_id": self.session_id,
                "level": level, "module": module, "event": event, "data": data or {},
            })
            encoded = json.dumps(record, ensure_ascii=False)
            if self.enabled:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(encoded + "\n")
            if self.sink:
                try:
                    self.sink(record)
                except Exception:
                    # An unavailable browser must never interrupt an active action.
                    pass
            if self.console and event != "HEARTBEAT":
                try:
                    print(encoded, flush=True)
                except (UnicodeError, OSError):
                    pass
