"""Telemetry and Reflex Daemon

Collects real-time environment metrics and operates high-frequency reflex triggers.
"""

import threading
import time
from typing import Dict, Any, Optional, Callable
from .client import MinecraftAgentClient


class TelemetryManager:
    """Maintains and caches real-time perception state of the Bot."""

    def __init__(self, client: MinecraftAgentClient):
        self.client = client
        self.last_status: Dict[str, Any] = {}
        self.last_update_time: float = 0.0

    def poll(self) -> Dict[str, Any]:
        """Polls fresh status from mc-agent-api."""
        res = self.client.get_status()
        if res.get("ok"):
            self.last_status = res.get("result", {})
            self.last_update_time = time.time()
        return self.last_status


class ReflexDaemon:
    """High-frequency background daemon (e.g. 20Hz) monitoring emergency reflexes (like falling)."""

    def __init__(
        self,
        client: MinecraftAgentClient,
        frequency_hz: int = 20,
        on_fall_detected: Optional[Callable[[], None]] = None,
    ):
        self.client = client
        self.interval = 1.0 / frequency_hz
        self.on_fall_detected = on_fall_detected
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Starts the reflex monitoring loop in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ReflexDaemon")
        self._thread.start()

    def stop(self) -> None:
        """Stops the reflex monitoring loop."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None

    def _loop(self) -> None:
        while self._running:
            try:
                # TODO: Check falling speed / air status
                # If emergency condition is met, invoke self.on_fall_detected()
                time.sleep(self.interval)
            except Exception:
                pass
