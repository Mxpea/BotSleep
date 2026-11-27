"""L4/Observability FlightRecorder: secret-redacting structured JSONL logger + live monitor sink."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set


class FlightRecorder:
    """Thread-safe structured JSONL flight recorder with secret scrubbing."""

    def __init__(
        self,
        output_dir: Path | str,
        *,
        enabled: bool = True,
        secrets: Iterable[str] = (),
        sink: Optional[Callable[[Dict[str, Any]], None]] = None,
        console: bool = False,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.enabled = bool(enabled)
        self.sink = sink
        self.console = bool(console)
        self._lock = threading.Lock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._secrets: Set[str] = {s for s in secrets if isinstance(s, str) and len(s) >= 3}
        self.log_path: Optional[Path] = None
        if self.enabled:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            self.log_path = self.output_dir / f"v2_flight_{stamp}.jsonl"

    def add_secret(self, secret: Optional[str]) -> None:
        if isinstance(secret, str) and len(secret) >= 3:
            with self._lock:
                self._secrets.add(secret)

    def add_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        """Register an in-process passive listener invoked on each recorded entry."""
        if callable(listener):
            with self._lock:
                if listener not in self._listeners:
                    self._listeners.append(listener)

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            out = value
            for secret in self._secrets:
                if secret in out:
                    out = out.replace(secret, "[REDACTED]")
            return out
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        return value

    def record(self, level: str, component: str, event: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        entry = {
            "ts": round(time.time(), 3),
            "level": level,
            "component": component,
            "event": event,
            "data": self._scrub(data or {}),
        }
        if self.enabled and self.log_path is not None:
            line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
            with self._lock:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        if self.console:
            print(f"[{entry['level']}] {component}.{event}: {json.dumps(entry['data'], ensure_ascii=False)}")
        if self.sink is not None:
            try:
                self.sink(entry)
            except Exception:
                pass
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(entry)
            except Exception:
                pass
        return entry
