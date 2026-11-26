"""L1 BlockUnreachableTracker: persistent (dim, pos, action) failure counter + TTL blacklist."""

from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

from v2.domain import Coord3
from v2.kernel.storage import UnifiedWorldDB


class BlockUnreachableTracker:
    """Tracks unreachable block/action pairs and blocked navigation edges with exponential TTL."""

    def __init__(
        self,
        db: Optional[UnifiedWorldDB] = None,
        *,
        base_ttl_seconds: float = 60.0,
        max_ttl_seconds: float = 600.0,
        edge_ttl_seconds: float = 30.0,
    ) -> None:
        self.db = db
        self.base_ttl = float(base_ttl_seconds)
        self.max_ttl = float(max_ttl_seconds)
        self.edge_ttl = float(edge_ttl_seconds)
        self._entries: Dict[Tuple[str, Coord3, str], Tuple[int, float, str]] = {}
        self._blocked_edges: Dict[Tuple[str, Coord3, Coord3], float] = {}

    def load_dimension(self, dimension: str, now: Optional[float] = None) -> None:
        if self.db is None:
            return
        for row in self.db.load_unreachable(dimension, now=now):
            key = (row["dimension"], tuple(row["pos"]), row["action"])
            self._entries[key] = (int(row["fail_count"]), float(row["expires_at"]), str(row["last_reason"]))

    def record_failure(
        self,
        dimension: str,
        pos: Coord3,
        action: str,
        reason: str = "failed",
        *,
        now: Optional[float] = None,
        threshold: int = 2,
    ) -> int:
        ts = time.time() if now is None else now
        key = (dimension, (int(pos[0]), int(pos[1]), int(pos[2])), action)
        prev_cnt, _, _ = self._entries.get(key, (0, 0.0, ""))
        cnt = prev_cnt + 1
        ttl = min(self.max_ttl, self.base_ttl * (2 ** max(0, cnt - threshold))) if cnt >= threshold else 15.0
        expires_at = ts + ttl
        self._entries[key] = (cnt, expires_at, reason)
        if self.db is not None:
            self.db.upsert_unreachable(dimension, key[1], action, cnt, expires_at, reason)
        return cnt

    def is_blocked(
        self,
        dimension: str,
        pos: Coord3,
        action: str,
        *,
        now: Optional[float] = None,
        min_failures: int = 2,
    ) -> bool:
        ts = time.time() if now is None else now
        key = (dimension, (int(pos[0]), int(pos[1]), int(pos[2])), action)
        entry = self._entries.get(key)
        if entry is None:
            return False
        cnt, expires_at, _ = entry
        if expires_at <= ts:
            self._entries.pop(key, None)
            return False
        return cnt >= min_failures

    def clear(self, dimension: str, pos: Coord3, action: Optional[str] = None) -> None:
        coord = (int(pos[0]), int(pos[1]), int(pos[2]))
        if action is not None:
            self._entries.pop((dimension, coord, action), None)
        else:
            for k in [k for k in self._entries if k[0] == dimension and k[1] == coord]:
                self._entries.pop(k, None)

    def record_blocked_edge(
        self,
        dimension: str,
        src: Coord3,
        dst: Coord3,
        *,
        now: Optional[float] = None,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        ts = time.time() if now is None else now
        ttl = self.edge_ttl if ttl_seconds is None else float(ttl_seconds)
        self._blocked_edges[(dimension, src, dst)] = ts + ttl

    def is_edge_blocked(self, dimension: str, src: Coord3, dst: Coord3, *, now: Optional[float] = None) -> bool:
        ts = time.time() if now is None else now
        exp = self._blocked_edges.get((dimension, src, dst))
        if exp is None:
            return False
        if exp <= ts:
            self._blocked_edges.pop((dimension, src, dst), None)
            return False
        return True
