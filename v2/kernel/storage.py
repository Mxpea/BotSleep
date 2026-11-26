"""L0 SQLite WAL UnifiedWorldDB scoped by (api_base_url, username, world_id).

Replaces scattered JSON files and temp-file renames with a single crash-safe,
thread-serialized WAL database storing pending_tx, voxel_chunks, landmarks,
unreachable_blocks, and checkpoints.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import zlib
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_tx (
    tx_id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    life_marker TEXT NOT NULL,
    dimension TEXT NOT NULL,
    pos_json TEXT NOT NULL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    state TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voxel_chunks (
    dimension TEXT NOT NULL,
    cx INTEGER NOT NULL,
    cy INTEGER NOT NULL,
    cz INTEGER NOT NULL,
    blob BLOB NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (dimension, cx, cy, cz)
);

CREATE TABLE IF NOT EXISTS landmarks (
    dimension TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    z INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (dimension, kind, name)
);

CREATE TABLE IF NOT EXISTS unreachable_blocks (
    dimension TEXT NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    z INTEGER NOT NULL,
    action TEXT NOT NULL,
    fail_count INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    last_reason TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (dimension, x, y, z, action)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    slot TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def scope_digest(api_base_url: str, username: str, world_id: str) -> str:
    raw = f"{api_base_url.rstrip('/')}\0{username}\0{world_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class UnifiedWorldDB:
    """Thread-safe SQLite WAL persistence for a single bot/world scope."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        api_base_url: str = "",
        username: str = "",
        world_id: str = "default",
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.scope_id = scope_digest(api_base_url, username, world_id)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.executescript(SCHEMA_SQL)
            now = time.time()
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value, updated_at) VALUES (?, ?, ?)",
                ("scope", json.dumps({"api_base_url": api_base_url, "username": username, "world_id": world_id}), now),
            )
            self._conn.commit()

    @classmethod
    def scope_db_path(
        cls,
        state_dir: Path | str,
        api_base_url: str,
        username: str,
        world_id: str = "default",
    ) -> Path:
        sid = scope_digest(api_base_url, username, world_id)
        return Path(state_dir) / f"world_{username}_{world_id}_{sid}.sqlite3"

    @classmethod
    def for_scope(
        cls,
        state_dir: Path | str,
        api_base_url: str,
        username: str,
        world_id: str = "default",
    ) -> "UnifiedWorldDB":
        path = cls.scope_db_path(state_dir, api_base_url, username, world_id)
        return cls(path, api_base_url=api_base_url, username=username, world_id=world_id)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- Pending Transaction Persistence ----

    def insert_pending_tx(self, row: Dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO pending_tx
                   (tx_id, endpoint, payload_digest, life_marker, dimension, pos_json, started_at, updated_at, state, details_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["tx_id"],
                    row["endpoint"],
                    row["payload_digest"],
                    row["life_marker"],
                    row["dimension"],
                    json.dumps(row["pos"]),
                    float(row.get("started_at", now)),
                    now,
                    row["state"],
                    json.dumps(row.get("details", {})),
                ),
            )
            self._conn.commit()

    def update_pending_tx(self, tx_id: str, state: str, details: Optional[Dict[str, Any]] = None) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE pending_tx SET state=?, updated_at=?, details_json=? WHERE tx_id=?",
                (state, now, json.dumps(details or {}), tx_id),
            )
            self._conn.commit()

    def delete_pending_tx(self, tx_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pending_tx WHERE tx_id=?", (tx_id,))
            self._conn.commit()

    def clear_all_pending_tx(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pending_tx")
            self._conn.commit()

    def get_active_pending_tx(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM pending_tx WHERE state IN ('inflight', 'unknown') ORDER BY started_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "tx_id": row["tx_id"],
                "endpoint": row["endpoint"],
                "payload_digest": row["payload_digest"],
                "life_marker": row["life_marker"],
                "dimension": row["dimension"],
                "pos": json.loads(row["pos_json"]),
                "started_at": row["started_at"],
                "updated_at": row["updated_at"],
                "state": row["state"],
                "details": json.loads(row["details_json"]),
            }

    # ---- Voxel Chunks (zlib-compressed JSON payloads) ----

    def save_voxel_chunk(self, dimension: str, cx: int, cy: int, cz: int, payload: Dict[str, Any]) -> None:
        blob = zlib.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), level=3)
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO voxel_chunks (dimension, cx, cy, cz, blob, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (dimension, int(cx), int(cy), int(cz), blob, now),
            )
            self._conn.commit()

    def load_voxel_chunk(self, dimension: str, cx: int, cy: int, cz: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT blob FROM voxel_chunks WHERE dimension=? AND cx=? AND cy=? AND cz=?",
                (dimension, int(cx), int(cy), int(cz)),
            ).fetchone()
            if row is None:
                return None
            return json.loads(zlib.decompress(row["blob"]).decode("utf-8"))

    # ---- Landmarks ----

    def save_landmark(
        self,
        dimension: str,
        kind: str,
        name: str,
        pos: Tuple[int, int, int],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO landmarks
                   (dimension, kind, name, x, y, z, metadata_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (dimension, kind, name, int(pos[0]), int(pos[1]), int(pos[2]), json.dumps(metadata or {}), now),
            )
            self._conn.commit()

    def list_landmarks(self, dimension: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if kind is None:
                rows = self._conn.execute(
                    "SELECT * FROM landmarks WHERE dimension=? ORDER BY updated_at DESC",
                    (dimension,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM landmarks WHERE dimension=? AND kind=? ORDER BY updated_at DESC",
                    (dimension, kind),
                ).fetchall()
            return [
                {
                    "dimension": r["dimension"],
                    "kind": r["kind"],
                    "name": r["name"],
                    "pos": (r["x"], r["y"], r["z"]),
                    "metadata": json.loads(r["metadata_json"]),
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]

    # ---- Unreachable Blocks ----

    def upsert_unreachable(
        self,
        dimension: str,
        pos: Tuple[int, int, int],
        action: str,
        fail_count: int,
        expires_at: float,
        last_reason: str,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO unreachable_blocks
                   (dimension, x, y, z, action, fail_count, expires_at, last_reason, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (dimension, int(pos[0]), int(pos[1]), int(pos[2]), action, int(fail_count), float(expires_at), last_reason, now),
            )
            self._conn.commit()

    def load_unreachable(self, dimension: str, now: Optional[float] = None) -> List[Dict[str, Any]]:
        ts = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM unreachable_blocks WHERE dimension=? AND expires_at > ?",
                (dimension, ts),
            ).fetchall()
            return [
                {
                    "dimension": r["dimension"],
                    "pos": (r["x"], r["y"], r["z"]),
                    "action": r["action"],
                    "fail_count": r["fail_count"],
                    "expires_at": r["expires_at"],
                    "last_reason": r["last_reason"],
                }
                for r in rows
            ]

    def prune_unreachable(self, now: Optional[float] = None) -> int:
        ts = time.time() if now is None else now
        with self._lock:
            cur = self._conn.execute("DELETE FROM unreachable_blocks WHERE expires_at <= ?", (ts,))
            self._conn.commit()
            return int(cur.rowcount)

    # ---- Checkpoints ----

    def save_checkpoint(self, slot: str, payload: Dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoints(slot, payload_json, updated_at) VALUES (?, ?, ?)",
                (slot, json.dumps(payload, ensure_ascii=False), now),
            )
            self._conn.commit()

    def load_checkpoint(self, slot: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT payload_json FROM checkpoints WHERE slot=?", (slot,)).fetchone()
            return json.loads(row["payload_json"]) if row is not None else None
