"""One sleep task at a time, while the main thread remains available to stop it."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Optional

from skills import SkillResult
from skills.sleep import AutoSleepSkill


class SleepController:
    SKILL_OPTIONS = frozenset({
        "radius", "threat_check", "threat_distance", "poll_interval_seconds",
        "timeout_seconds", "move_timeout_seconds", "confirm_timeout_seconds",
    })

    def __init__(self, client, settings, recorder=None, skill=None):
        self.client = client
        self.settings = settings
        self.recorder = recorder
        self.skill = skill or AutoSleepSkill(client)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="auto-sleep")
        self._future = None
        self._cancel = threading.Event()
        self._paused = False
        self._closed = False
        self._next_attempt = 0.0

    @property
    def busy(self) -> bool:
        return self._future is not None

    def _record(self, level, event, data):
        if self.recorder:
            self.recorder.record(level, "auto_sleep", event, data)

    def request_sleep(self) -> bool:
        """Resume automation and request one attempt; never overlap an existing task."""
        if self._closed or self._future is not None:
            return False
        self._paused = False
        self._cancel = threading.Event()
        arguments = {key: value for key, value in self.settings.items()
                     if key in self.SKILL_OPTIONS}
        arguments["cancel_event"] = self._cancel
        self._record("INFO", "SLEEP_STARTED", {"radius": arguments["radius"]})
        self._future = self._executor.submit(self.skill.execute, **arguments)
        return True

    def pause(self) -> None:
        """Request cancellation and keep automation paused until #sleep or restart."""
        self._paused = True
        self._cancel.set()
        if self._future is not None and not self._future.done():
            self.client.stop()
        self._record("INFO", "SLEEP_STOP_REQUESTED", {})

    def tick(self, automatic=True) -> Optional[SkillResult]:
        if self._closed:
            return None
        if self._future is not None:
            if not self._future.done():
                return None
            try:
                result = self._future.result()
                if not isinstance(result, SkillResult):
                    raise TypeError("sleep skill returned an invalid result")
            except Exception as exc:
                result = SkillResult(False, str(exc), error_code="internal")
            finally:
                self._future = None
                self._next_attempt = time.monotonic() + self.settings["retry_seconds"]
            self._record("INFO" if result.success else "WARN", "SLEEP_RESULT", asdict(result))
            return result
        if not automatic or not self.settings["enabled"] or self._paused:
            return None
        if time.monotonic() < self._next_attempt:
            return None
        # Poll fresh state: API failure must not turn a stale cache into an action.
        status = self.client.get_status()["result"]
        if not status.get("ready") or not status.get("alive", False) or status.get("busy"):
            return None
        if status.get("is_sleeping") or not status.get("is_day", True) or status.get("thundering"):
            self.request_sleep()
        return None

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.pause()
        finally:
            self._closed = True
            # Skill waits and HTTP calls are bounded; do not log out underneath a worker.
            self._executor.shutdown(wait=True, cancel_futures=True)
            if self._future is not None:
                try:
                    result = self._future.result()
                    if isinstance(result, SkillResult):
                        self._record("INFO" if result.success else "WARN", "SLEEP_RESULT", asdict(result))
                finally:
                    self._future = None
