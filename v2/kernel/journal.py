"""L0 Write-ahead PendingTransactionJournal and OS instance lock.

Invariants enforced:
- Every side-effecting write endpoint call is persisted to SQLite WAL *before* HTTP dispatch.
- If a write ends in `unknown` (timeout, disconnect, crash), ALL subsequent writes are frozen
  until explicitly cleared; never auto-replayed or bypassed by cleanup writes.
- `instance_lock()` prevents two bot processes from sharing the same (api, user, world_id) scope.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from typing import Any, Dict, Iterator, Optional, Sequence

from v2.kernel.storage import UnifiedWorldDB


class PendingWriteFrozenError(RuntimeError):
    """Raised when attempting a write while an unresolved/unknown write transaction is active."""

    def __init__(self, message: str, pending_tx: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = "pending_write_frozen"
        self.pending_tx = pending_tx


class InstanceLockError(RuntimeError):
    """Raised when another BotSleep process already owns the same scope lock."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "instance_locked"


def payload_digest(payload: Any) -> str:
    raw = json.dumps(payload or {}, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class PendingTransactionJournal:
    """Write-ahead pending transaction ledger backed by UnifiedWorldDB."""

    def __init__(self, db: UnifiedWorldDB) -> None:
        self.db = db
        self.lock_path = db.db_path.with_suffix(".instance.lock")

    def get_pending(self) -> Optional[Dict[str, Any]]:
        return self.db.get_active_pending_tx()

    def has_pending_unknown(self) -> bool:
        tx = self.get_pending()
        return tx is not None and tx.get("state") in {"inflight", "unknown"}

    def begin_write(
        self,
        endpoint: str,
        payload: Any,
        *,
        life_marker: str = "alive",
        dimension: str = "overworld",
        pos: Sequence[float] = (0.0, 64.0, 0.0),
    ) -> str:
        """Persist a write-ahead pending transaction; refuse if frozen by a prior unresolved write."""
        existing = self.get_pending()
        if existing is not None:
            raise PendingWriteFrozenError(
                f"Write '{endpoint}' blocked by unresolved {existing['state']} transaction "
                f"{existing['tx_id']} ({existing['endpoint']})",
                pending_tx=existing,
            )
        tx_id = uuid.uuid4().hex
        self.db.insert_pending_tx({
            "tx_id": tx_id,
            "endpoint": endpoint,
            "payload_digest": payload_digest(payload),
            "life_marker": life_marker,
            "dimension": dimension,
            "pos": [float(pos[0]), float(pos[1]), float(pos[2])],
            "started_at": time.time(),
            "state": "inflight",
            "details": {},
        })
        return tx_id

    def resolve_write(
        self,
        tx_id: str,
        outcome: str,
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Finalize a transaction. Only 'confirmed' or 'failed_known' releases the write gate."""
        if outcome in {"confirmed", "failed_known"}:
            self.db.delete_pending_tx(tx_id)
        elif outcome == "unknown":
            self.db.update_pending_tx(tx_id, "unknown", details or {"action_state": "unknown"})
        else:
            raise ValueError(f"Unsupported transaction outcome: {outcome}")

    def acknowledge_clear(self, reason: str = "operator_acknowledged") -> bool:
        """Explicit operator or verified-reset clearance of a frozen unknown write."""
        had = self.get_pending() is not None
        self.db.clear_all_pending_tx()
        self.db.save_checkpoint("last_pending_clear", {"reason": reason, "cleared_at": time.time()})
        return had

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

    @contextmanager
    def instance_lock(self) -> Iterator[Path]:
        """Acquire a non-blocking OS lock for this bot/world scope."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        locked = False
        try:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            try:
                self._os_lock(handle)
                locked = True
            except OSError:
                raise InstanceLockError(f"Scope already locked by another instance: {self.lock_path}") from None
            yield self.lock_path
        finally:
            if locked:
                try:
                    self._os_unlock(handle)
                except OSError:
                    pass
            handle.close()
