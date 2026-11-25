"""L4 Non-blocking SerialModelWorker: runs LLM inference in background so BT safety never blocks."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable, Optional


@dataclass
class WorkerResult:
    job_id: str
    context_version: int
    ok: bool
    value: Any = None
    error: Optional[Exception] = None
    finished_at: float = 0.0


class SerialModelWorker:
    """Single background worker thread executing at most one LLM call at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._inflight_job_id: Optional[str] = None
        self._completed: Optional[WorkerResult] = None

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._inflight_job_id is not None

    def submit(self, job_id: str, context_version: int, fn: Callable[[], Any]) -> bool:
        """Submit a background model call if idle. Returns False if another call is still running."""
        with self._lock:
            if self._inflight_job_id is not None:
                return False
            self._inflight_job_id = job_id
            self._completed = None

            def _runner() -> None:
                try:
                    val = fn()
                    res = WorkerResult(job_id, context_version, True, value=val, finished_at=time.time())
                except Exception as exc:
                    res = WorkerResult(job_id, context_version, False, error=exc, finished_at=time.time())
                with self._lock:
                    self._completed = res
                    self._inflight_job_id = None

            self._thread = threading.Thread(target=_runner, name=f"v2-llm-{job_id}", daemon=True)
            self._thread.start()
            return True

    def poll(self, *, expected_context_version: Optional[int] = None) -> Optional[WorkerResult]:
        """Non-blocking fetch of finished result; discards stale proposals if context_version changed."""
        with self._lock:
            res = self._completed
            if res is None:
                return None
            self._completed = None
        if expected_context_version is not None and res.context_version != expected_context_version:
            return None
        return res

    def join(self, timeout: Optional[float] = 5.0) -> None:
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
