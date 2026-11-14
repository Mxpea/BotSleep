"""One model request per provider credential, including across local processes.

An incomplete transport response leaves a durable uncertainty latch. A client
timeout cannot prove the provider stopped generating, so waiting or restarting
must not silently grant another request.
"""

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class ModelGateError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.details = {"executed": False}


_THREAD_LOCKS = {}
_REGISTRY_LOCK = threading.Lock()
_UNFINISHED = {"llm_timeout", "llm_network_error"}


class ModelRequestGate:
    """A credential-scoped OS lock plus an atomically written request ledger."""

    def __init__(self, endpoint, api_key, *, directory=None, wait_seconds=5, recorder=None):
        identity = endpoint.rstrip("/").removesuffix("/chat/completions")
        digest = hashlib.sha256((identity + "\0" + api_key).encode()).hexdigest()
        self.directory = Path(directory or Path(tempfile.gettempdir()) / "botsleep-model-gates")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.directory / (digest + ".lock")
        self.state_path = self.directory / (digest + ".json")
        self.wait_seconds = wait_seconds
        self.recorder = recorder
        with _REGISTRY_LOCK:
            self._thread_lock = _THREAD_LOCKS.setdefault(str(self.lock_path.resolve()), threading.Lock())

    def _record(self, event, data):
        if self.recorder:
            self.recorder.record("WARN" if "UNCERTAIN" in event else "INFO", "llm", event, data)

    def _read(self):
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"state": "idle"}
        except (OSError, ValueError):
            raise ModelGateError("llm_gate_corrupt", "Model request ledger cannot be verified") from None
        if not isinstance(value, dict) or value.get("state") not in {"idle", "inflight", "uncertain"}:
            raise ModelGateError("llm_gate_corrupt", "Model request ledger has an invalid state")
        return value

    def _write(self, value):
        temporary = self.state_path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=True, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _os_lock(handle):
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _os_unlock(handle):
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _locked(self, cancel_event=None):
        deadline = time.monotonic() + self.wait_seconds
        handle = None
        acquired = False
        while not self._thread_lock.acquire(timeout=0.05):
            self._check_wait(deadline, cancel_event)
        try:
            handle = self.lock_path.open("a+b")
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            while True:
                self._check_wait(deadline, cancel_event, check_deadline=False)
                try:
                    self._os_lock(handle)
                    acquired = True
                    break
                except OSError:
                    self._check_wait(deadline, cancel_event)
                    time.sleep(0.05)
            yield
        finally:
            if acquired:
                self._os_unlock(handle)
            if handle:
                handle.close()
            self._thread_lock.release()

    @staticmethod
    def _check_wait(deadline, cancel_event, *, check_deadline=True):
        if cancel_event is not None and cancel_event.is_set():
            raise ModelGateError("llm_cancelled", "Model slot wait cancelled; no request sent")
        if check_deadline and time.monotonic() >= deadline:
            raise ModelGateError("llm_busy", "Another local request still owns the model slot")

    @contextmanager
    def request(self, cancel_event=None):
        with self._locked(cancel_event):
            previous = self._read()
            if previous["state"] != "idle":
                self._record("MODEL_REQUEST_UNCERTAIN", {"reason": "previous_request_not_confirmed_finished"})
                raise ModelGateError("llm_request_uncertain", "Previous model request may still be running; confirm provider idle before resuming")
            state = {"state": "inflight", "request_id": uuid.uuid4().hex,
                     "pid": os.getpid(), "started_at": time.time()}
            self._write(state)
            self._record("MODEL_SLOT_ACQUIRED", {"request_id": state["request_id"]})
            try:
                yield
            except BaseException as error:
                # Completed HTTP errors/cancellation after a complete response
                # release the slot. Unknown transport outcomes and process-level
                # interruptions require explicit provider-idle acknowledgement.
                code = getattr(error, "code", None)
                uncertain = code in _UNFINISHED or not isinstance(error, Exception) or code is None
                state.update(state="uncertain" if uncertain else "idle", finished_at=time.time(), error_code=code or "interrupted")
                self._write(state)
                self._record("MODEL_REQUEST_UNCERTAIN" if uncertain else "MODEL_SLOT_RELEASED",
                             {"request_id": state["request_id"], "error_code": state["error_code"]})
                raise
            else:
                state.update(state="idle", finished_at=time.time())
                self._write(state)
                self._record("MODEL_SLOT_RELEASED", {"request_id": state["request_id"]})

    def acknowledge_provider_idle(self):
        """Only an explicit operator acknowledgement may clear crash/timeout debt."""
        with self._locked():
            self._write({"state": "idle", "acknowledged_at": time.time(), "reason": "operator_confirmed_provider_idle"})
            self._record("MODEL_IDLE_ACKNOWLEDGED", {"reason": "operator_confirmed_provider_idle"})

    def snapshot(self):
        value = self._read()
        return {key: value[key] for key in ("state", "started_at", "finished_at", "error_code") if key in value}
