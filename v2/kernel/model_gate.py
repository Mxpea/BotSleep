"""L0 Cross-process FIFO ModelRequestGate with 30s transport expiry and per-provider slot isolation.

Purified directly from battle-tested core/model_gate.py (0 deadlocks across v25-v31 9-bot runs).
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, Iterator, Optional


class ModelGateError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.details = {"executed": False}


_THREAD_LOCKS: Dict[str, threading.Lock] = {}
_GLOBAL_THREAD_LOCKS: Dict[str, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()
_UNFINISHED = {"llm_timeout", "llm_network_error"}
TRANSPORT_WINDOW_SECONDS = 30


class ModelRequestGate:
    """Global concurrency-1 FIFO lock plus a credential-scoped atomic request ledger."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        directory: Optional[Path | str] = None,
        wait_seconds: float = 5.0,
        recorder: Any = None,
        global_concurrency: bool = True,
        auto_recover_uncertain: bool = False,
        scope_id: Optional[str] = None,
    ) -> None:
        identity = endpoint.rstrip("/").removesuffix("/chat/completions")
        raw_identity = identity + "\0" + api_key
        if not global_concurrency and scope_id:
            raw_identity += "\0" + str(scope_id)
        digest = hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()
        self.directory = Path(directory or Path(tempfile.gettempdir()) / "botsleep-model-gates")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.directory / f"{digest}.lock"
        self.state_path = self.directory / f"{digest}.json"
        self.global_concurrency = bool(global_concurrency)
        self.auto_recover_uncertain = bool(auto_recover_uncertain)
        self.global_lock_path = self.directory / "global.lock"
        queue_prefix = "global_queue" if self.global_concurrency else f"{digest}.queue"
        self.queue_lock_path = self.directory / f"{queue_prefix}.lock"
        self.queue_path = self.directory / f"{queue_prefix}.json"
        self.wait_seconds = float(wait_seconds)
        self.recorder = recorder
        with _REGISTRY_LOCK:
            self._thread_lock = _THREAD_LOCKS.setdefault(str(self.lock_path.resolve()), threading.Lock())
            self._global_thread_lock = _GLOBAL_THREAD_LOCKS.setdefault(
                str(self.directory.resolve()), threading.Lock()
            )

    def _record(self, event: str, data: Dict[str, Any]) -> None:
        if self.recorder is not None:
            level = "WARN" if ("UNCERTAIN" in event or "RECOVERED" in event) else "INFO"
            self.recorder.record(level, "llm", event, data)

    def _read(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"state": "idle"}
        except (OSError, ValueError):
            raise ModelGateError("llm_gate_corrupt", "Model request ledger cannot be verified") from None
        if not isinstance(value, dict) or value.get("state") not in {"idle", "inflight", "uncertain"}:
            raise ModelGateError("llm_gate_corrupt", "Model request ledger has an invalid state")
        return value

    def _write_json_atomic(self, target_path: Path, value: Any) -> None:
        temporary = target_path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=True, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(5):
                try:
                    os.replace(temporary, target_path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.015)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _write(self, value: Dict[str, Any]) -> None:
        self._write_json_atomic(self.state_path, value)

    @staticmethod
    def _os_lock(handle: Any) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _os_unlock(handle: Any) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_queue(self) -> list[Dict[str, Any]]:
        try:
            data = json.loads(self.queue_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [
            row for row in data
            if isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and isinstance(row.get("heartbeat"), (int, float))
        ]

    @contextmanager
    def _queue_file_locked(self) -> Iterator[None]:
        with _REGISTRY_LOCK:
            q_thread_lock = _GLOBAL_THREAD_LOCKS.setdefault(
                str(self.queue_lock_path.resolve()), threading.Lock()
            )
        deadline = time.monotonic() + 2.0
        while not q_thread_lock.acquire(timeout=0.02):
            if time.monotonic() >= deadline:
                raise ModelGateError("llm_busy", "Model wait queue is temporarily contended")
        handle = None
        locked = False
        try:
            handle = self.queue_lock_path.open("a+b")
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            while True:
                try:
                    self._os_lock(handle)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ModelGateError("llm_busy", "Model wait queue lock timed out") from None
                    time.sleep(0.01)
            yield
        finally:
            if locked:
                self._os_unlock(handle)
            if handle:
                handle.close()
            q_thread_lock.release()

    def _queue_touch(self, ticket_id: str) -> str:
        now = time.time()
        with self._queue_file_locked():
            rows = [row for row in self._read_queue() if (now - row["heartbeat"]) <= 5.0]
            updated = False
            for row in rows:
                if row["id"] == ticket_id:
                    row["heartbeat"] = now
                    updated = True
                    break
            if not updated:
                rows.append({"id": ticket_id, "pid": os.getpid(), "heartbeat": now})
            self._write_json_atomic(self.queue_path, rows)
            return rows[0]["id"] if rows else ticket_id

    def _queue_remove(self, ticket_id: str) -> None:
        now = time.time()
        try:
            with self._queue_file_locked():
                rows = [
                    row for row in self._read_queue()
                    if row["id"] != ticket_id and (now - row["heartbeat"]) <= 5.0
                ]
                self._write_json_atomic(self.queue_path, rows)
        except Exception:
            pass

    @staticmethod
    def _open_lock_file(path: Path) -> Any:
        handle = path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        return handle

    @contextmanager
    def _locked(self, cancel_event: Optional[threading.Event] = None, *, wait_seconds: Optional[float] = None) -> Iterator[None]:
        timeout = self.wait_seconds if wait_seconds is None else max(0.05, float(wait_seconds))
        deadline = time.monotonic() + timeout
        ticket_id = uuid.uuid4().hex
        global_thread_acquired = False
        cred_thread_acquired = False
        global_handle = None
        cred_handle = None
        global_locked = False
        cred_locked = False

        def release_attempt_locks() -> None:
            nonlocal global_thread_acquired, cred_thread_acquired, global_locked, cred_locked, global_handle, cred_handle
            if cred_locked:
                self._os_unlock(cred_handle)
                cred_locked = False
            if cred_handle is not None:
                cred_handle.close()
                cred_handle = None
            if global_locked:
                self._os_unlock(global_handle)
                global_locked = False
            if global_handle is not None:
                global_handle.close()
                global_handle = None
            if cred_thread_acquired:
                self._thread_lock.release()
                cred_thread_acquired = False
            if global_thread_acquired:
                self._global_thread_lock.release()
                global_thread_acquired = False

        try:
            while True:
                self._check_wait(deadline, cancel_event)
                head_id = self._queue_touch(ticket_id)
                if head_id == ticket_id:
                    if not self.global_concurrency or self._global_thread_lock.acquire(blocking=False):
                        global_thread_acquired = self.global_concurrency
                        if self._thread_lock.acquire(blocking=False):
                            cred_thread_acquired = True
                            try:
                                if self.global_concurrency:
                                    global_handle = self._open_lock_file(self.global_lock_path)
                                    self._os_lock(global_handle)
                                    global_locked = True
                                cred_handle = self._open_lock_file(self.lock_path)
                                self._os_lock(cred_handle)
                                cred_locked = True
                                break
                            except OSError:
                                release_attempt_locks()
                        else:
                            release_attempt_locks()
                    else:
                        release_attempt_locks()
                self._check_wait(deadline, cancel_event)
                time.sleep(min(0.05, max(0.005, deadline - time.monotonic())))
            self._queue_remove(ticket_id)
            yield
        finally:
            self._queue_remove(ticket_id)
            release_attempt_locks()

    @staticmethod
    def _check_wait(deadline: float, cancel_event: Optional[threading.Event], *, check_deadline: bool = True) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ModelGateError("llm_cancelled", "Model slot wait cancelled; no request sent")
        if check_deadline and time.monotonic() >= deadline:
            raise ModelGateError("llm_busy", "Another local request still owns the model slot")

    @contextmanager
    def request(self, cancel_event: Optional[threading.Event] = None, *, wait_seconds: Optional[float] = None) -> Iterator[None]:
        with self._locked(cancel_event, wait_seconds=wait_seconds):
            previous = self._read()
            if self._retry_due(previous):
                self._record("MODEL_TRANSPORT_WINDOW_EXPIRED", {"error_code": previous["error_code"]})
            elif previous["state"] != "idle":
                if self.auto_recover_uncertain:
                    self._record("MODEL_STALE_SLOT_RECOVERED", {
                        "previous_state": previous["state"],
                        "error_code": previous.get("error_code"),
                    })
                else:
                    self._record("MODEL_REQUEST_UNCERTAIN", {"reason": "previous_request_not_confirmed_finished"})
                    raise ModelGateError("llm_request_uncertain", "Previous model request has not cleared its gate")
            state: Dict[str, Any] = {
                "state": "inflight",
                "request_id": uuid.uuid4().hex,
                "pid": os.getpid(),
                "started_at": time.time(),
            }
            self._write(state)
            self._record("MODEL_SLOT_ACQUIRED", {"request_id": state["request_id"]})
            try:
                yield
            except BaseException as error:
                code = getattr(error, "code", None)
                uncertain = code in _UNFINISHED or not isinstance(error, Exception) or code is None
                recovered = bool(self.auto_recover_uncertain and isinstance(error, Exception) and code in _UNFINISHED)
                if recovered:
                    uncertain = False
                state.update(
                    state="uncertain" if uncertain else "idle",
                    finished_at=time.time(),
                    error_code=code or "interrupted",
                )
                self._write(state)
                event_data: Dict[str, Any] = {"request_id": state["request_id"], "error_code": state["error_code"]}
                if recovered:
                    event_data["recovered_uncertain"] = True
                self._record("MODEL_REQUEST_UNCERTAIN" if uncertain else "MODEL_SLOT_RELEASED", event_data)
                raise
            else:
                state.update(state="idle", finished_at=time.time())
                self._write(state)
                self._record("MODEL_SLOT_RELEASED", {"request_id": state["request_id"]})

    def acknowledge_provider_idle(self) -> None:
        with self._locked():
            self._write({"state": "idle", "acknowledged_at": time.time(), "reason": "operator_confirmed_provider_idle"})
            self._record("MODEL_IDLE_ACKNOWLEDGED", {"reason": "operator_confirmed_provider_idle"})

    @staticmethod
    def _retry_due(value: Dict[str, Any]) -> bool:
        started = value.get("started_at")
        return (
            value["state"] == "uncertain"
            and value.get("error_code") in _UNFINISHED
            and isinstance(started, (int, float))
            and started > 0
            and time.time() - started >= TRANSPORT_WINDOW_SECONDS
        )

    def clear_expired_transport(self) -> bool:
        with self._locked():
            if self._retry_due(self._read()):
                self._write({"state": "idle", "reason": "transport_window_expired"})
                return True
            return False

    def snapshot(self) -> Dict[str, Any]:
        value = self._read()
        if self._retry_due(value):
            return {"state": "idle"}
        return {key: value[key] for key in ("state", "started_at", "finished_at", "error_code") if key in value}
