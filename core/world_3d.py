"""Persistent universal 3D voxel world memory, topological frontiers, ore registry, and blocked edge tracking."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import zlib
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from core.cave_probe import (
    evaluate_ore_block,
    has_exposed_air_face,
    is_solid_floor,
    ore_cell_hazard_free,
)
from core.structure_spec import ORE_DROP_MAP


Coord3D = Tuple[int, int, int]
Edge3D = Tuple[Coord3D, Coord3D]


def norm_coord(pos: Sequence[int]) -> Coord3D:
    return (int(pos[0]), int(pos[1]), int(pos[2]))


def _chunk_key(x: int, y: int, z: int) -> Coord3D:
    return (x >> 4, y >> 4, z >> 4)


@dataclass
class FrontierNode:
    """Walkable/climbable boundary stance bordering unexplored 3D world space."""

    pos: Coord3D
    unseen_neighbors: int = 1
    clearance: int = 0
    requires_op: str = "walk"
    fail_count: int = 0
    discovered_at: float = 0.0
    last_score: float = 0.0


@dataclass
class OreNode:
    """Tracked ore coordinate with mining & hazard status."""

    pos: Coord3D
    name: str
    status: str = "pending"  # pending | mined | skipped_quota | skipped_tool | skipped_hazard | unreachable | seen
    fail_count: int = 0


class VoxelWorld3D:
    """Scoped, chunk-backed 3D world map shared across pathfinding, exploration, mining, and retreat."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        storage_path: Optional[Union[str, Path]] = None,
        *,
        scope: str = "default",
        block_cache: Optional[Dict[Coord3D, Dict[str, Any]]] = None,
        auto_load: bool = True,
    ) -> None:
        self.scope = str(scope or "default")
        self.db_path: Optional[Path] = None
        if storage_path is not None:
            p = Path(storage_path)
            self.db_path = p if p.suffix in {".sqlite", ".sqlite3", ".db"} else (p / "world_3d.sqlite3")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.block_cache: Dict[Coord3D, Dict[str, Any]] = block_cache if block_cache is not None else {}
        self.voxels: Dict[Coord3D, str] = {
            k: str(v.get("name") or "air")
            for k, v in self.block_cache.items()
            if isinstance(v, dict)
        }
        self._dirty_chunks: Set[Coord3D] = {_chunk_key(x, y, z) for (x, y, z) in self.voxels}

        self.visited_stances: Dict[Coord3D, int] = {}
        self.scanned_cells: Set[Coord3D] = set()
        self.open_frontiers: Dict[Coord3D, FrontierNode] = {}
        self.ore_registry: Dict[Coord3D, OreNode] = {}
        self.blocked_edges: Dict[Edge3D, int] = {}
        self.placed_scaffolds: Set[Coord3D] = set()

        if self.db_path is not None:
            self._init_schema()
            if auto_load and self.db_path.exists():
                self.load()

    @classmethod
    def for_runtime(
        cls,
        *,
        endpoint: str = "http://127.0.0.1:18900/api/v1",
        username: str = "SleepBot",
        world_id: str = "default",
        dimension: str = "overworld",
        base_dir: Union[str, Path] = "runtime/world_3d",
        auto_load: bool = True,
    ) -> "VoxelWorld3D":
        """Create/attach a persistent 3D world database scoped to (endpoint, username, world_id, dimension)."""
        scope_payload = json.dumps(
            {
                "endpoint": str(endpoint).rstrip("/"),
                "username": str(username),
                "world_id": str(world_id),
                "dimension": str(dimension),
            },
            sort_keys=True,
        )
        scope_hash = hashlib.sha256(scope_payload.encode("utf-8")).hexdigest()[:24]
        return cls(Path(base_dir) / scope_hash / "world_3d.sqlite3", scope=scope_payload, auto_load=auto_load)

    def _connect(self) -> sqlite3.Connection:
        assert self.db_path is not None
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        if self.db_path is None:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    cx INTEGER NOT NULL,
                    cy INTEGER NOT NULL,
                    cz INTEGER NOT NULL,
                    payload BLOB NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (cx, cy, cz)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS state_blobs (
                    key TEXT PRIMARY KEY,
                    payload BLOB NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(self.SCHEMA_VERSION)),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                ("scope", self.scope),
            )
            conn.commit()

    def set_block(self, pos: Sequence[int], block: Mapping[str, Any]) -> Dict[str, Any]:
        key = norm_coord(pos)
        name = str(block.get("name") or "air")
        props = dict(block.get("properties") or {}) if isinstance(block.get("properties"), Mapping) else {}
        cell = {"name": name, "properties": props, "pos": [key[0], key[1], key[2]]}
        self.block_cache[key] = cell
        self.voxels[key] = name
        self._dirty_chunks.add(_chunk_key(*key))
        return cell

    def get_block_cached(self, pos: Sequence[int]) -> Optional[Dict[str, Any]]:
        key = norm_coord(pos)
        if key in self.block_cache:
            return self.block_cache[key]
        if key in self.voxels:
            cell = {"name": self.voxels[key], "properties": {}, "pos": [key[0], key[1], key[2]]}
            self.block_cache[key] = cell
            return cell
        return None

    def read_block(
        self,
        pos: Sequence[int],
        fallback_reader: Optional[Callable[[Sequence[int]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        key = norm_coord(pos)
        cached = self.get_block_cached(key)
        if cached is not None:
            return cached
        if fallback_reader is None:
            return {"name": "unknown", "properties": {}, "pos": [key[0], key[1], key[2]]}
        raw = fallback_reader(key)
        if isinstance(raw, Mapping) and raw.get("name"):
            return self.set_block(key, raw)
        return self.set_block(key, {"name": "unknown", "properties": {}})

    def invalidate(self, pos: Sequence[int], radius: int = 1) -> None:
        px, py, pz = norm_coord(pos)
        r = max(0, int(radius))
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for dz in range(-r, r + 1):
                    k = (px + dx, py + dy, pz + dz)
                    self.block_cache.pop(k, None)
                    if self.voxels.pop(k, None) is not None:
                        self._dirty_chunks.add(_chunk_key(*k))

    def record_stance(self, pos: Sequence[int], *, cover_radius: int = 2) -> None:
        """Mark pos as visited and sweep tight nearby ground coverage, pruning covered frontiers."""
        key = norm_coord(pos)
        self.visited_stances[key] = self.visited_stances.get(key, 0) + 1
        r = max(1, int(cover_radius))
        kx, ky, kz = key
        for dx in range(-r, r + 1):
            for dy in (-1, 0, 1):
                for dz in range(-r, r + 1):
                    if abs(dx) + abs(dz) <= r + 1:
                        c = (kx + dx, ky + dy, kz + dz)
                        self.scanned_cells.add(c)
                        self.open_frontiers.pop(c, None)

    def record_blocked_edge(self, src: Sequence[int], dst: Sequence[int]) -> None:
        u = norm_coord(src)
        v = norm_coord(dst)
        edge = (u, v)
        self.blocked_edges[edge] = self.blocked_edges.get(edge, 0) + 1
        if v in self.open_frontiers:
            f = self.open_frontiers[v]
            f.fail_count += 1
            if f.fail_count >= 2:
                self.open_frontiers.pop(v, None)

    def clear_blocked_edges_near(self, pos: Sequence[int], radius: int = 1) -> None:
        px, py, pz = norm_coord(pos)
        drop_keys = [
            e for e in self.blocked_edges
            if (abs(e[0][0] - px) <= radius and abs(e[0][1] - py) <= radius and abs(e[0][2] - pz) <= radius)
            or (abs(e[1][0] - px) <= radius and abs(e[1][1] - py) <= radius and abs(e[1][2] - pz) <= radius)
        ]
        for e in drop_keys:
            self.blocked_edges.pop(e, None)

    def is_edge_blocked(self, src: Sequence[int], dst: Sequence[int], *, max_fails: int = 1) -> bool:
        edge = (norm_coord(src), norm_coord(dst))
        return self.blocked_edges.get(edge, 0) >= int(max_fails)

    def mark_frontier_failed(self, pos: Sequence[int]) -> None:
        key = norm_coord(pos)
        if key in self.open_frontiers:
            f = self.open_frontiers[key]
            f.fail_count += 1
            if f.fail_count >= 2:
                self.open_frontiers.pop(key, None)

    def mark_ore_status(self, pos: Sequence[int], status: str, *, name: Optional[str] = None) -> None:
        key = norm_coord(pos)
        prev = self.ore_registry.get(key)
        ore_name = name or (prev.name if prev else str(self.voxels.get(key) or "ore"))
        if prev is None:
            self.ore_registry[key] = OreNode(pos=key, name=ore_name, status=status)
        else:
            prev.status = status
            if name:
                prev.name = name

    def update_frontiers_from_graph(
        self,
        graph: Mapping[str, Any],
        *,
        entry_pos: Optional[Sequence[int]] = None,
        max_radius: Optional[int] = None,
    ) -> None:
        """Merge local/global horizon scan outputs into persistent 3D open_frontiers & ore_registry."""
        now = time.time()
        entry_k = norm_coord(entry_pos) if entry_pos is not None else None
        rad = int(max_radius) if max_radius is not None else None

        def _within_bounds(p: Coord3D) -> bool:
            if entry_k is None or rad is None:
                return True
            return abs(p[0] - entry_k[0]) <= rad and abs(p[2] - entry_k[2]) <= rad

        # Prune covered/invalidated frontiers already present
        for fkey in list(self.open_frontiers):
            if (
                fkey in self.scanned_cells
                or fkey in self.visited_stances
                or fkey in self.placed_scaffolds
                or self.open_frontiers[fkey].fail_count >= 2
            ):
                self.open_frontiers.pop(fkey, None)
                continue
            fl = self.get_block_cached((fkey[0], fkey[1] - 1, fkey[2]))
            if fl is not None and not is_solid_floor(fl):
                self.open_frontiers.pop(fkey, None)

        for row in graph.get("frontiers") or ():
            raw_pos = row.get("pos")
            if not isinstance(raw_pos, (list, tuple)) or len(raw_pos) != 3:
                continue
            fk = norm_coord(raw_pos)
            if fk in self.scanned_cells or fk in self.visited_stances or fk in self.placed_scaffolds:
                continue
            if not _within_bounds(fk):
                continue
            path = row.get("path") or []
            req_op = str(path[0].get("mode") or "walk") if path else "walk"
            prev = self.open_frontiers.get(fk)
            if prev is None:
                self.open_frontiers[fk] = FrontierNode(
                    pos=fk,
                    unseen_neighbors=1,
                    clearance=0,
                    requires_op=req_op,
                    fail_count=0,
                    discovered_at=now,
                    last_score=float(row.get("score", 0.0)),
                )
            else:
                prev.last_score = float(row.get("score", prev.last_score))

        for orow in graph.get("ore_targets") or ():
            raw_pos = orow.get("ore_pos")
            if not isinstance(raw_pos, (list, tuple)) or len(raw_pos) != 3:
                continue
            ok = norm_coord(raw_pos)
            oname = str(orow.get("name") or "")
            existing = self.ore_registry.get(ok)
            if existing is None:
                self.ore_registry[ok] = OreNode(pos=ok, name=oname, status="pending")
            elif existing.status == "unreachable":
                existing.status = "pending"

    def select_best_frontiers(
        self,
        cur_pos: Sequence[int],
        *,
        entry_pos: Optional[Sequence[int]] = None,
        max_radius: Optional[int] = None,
        goal_hints: Optional[Sequence[Sequence[int]]] = None,
        limit: int = 16,
    ) -> List[FrontierNode]:
        """Rank open persistent 3D frontiers from closest unexplored regions outward."""
        cx, cy, cz = norm_coord(cur_pos)
        entry_k = norm_coord(entry_pos) if entry_pos is not None else None
        rad = int(max_radius) if max_radius is not None else None
        hints = [norm_coord(h) for h in (goal_hints or ()) if len(h) == 3]

        candidates: List[Tuple[float, FrontierNode]] = []
        for fk, fnode in list(self.open_frontiers.items()):
            if fk in self.scanned_cells or fk in self.visited_stances or fk in self.placed_scaffolds:
                self.open_frontiers.pop(fk, None)
                continue
            if fnode.fail_count >= 2:
                continue
            if entry_k is not None and rad is not None:
                if abs(fk[0] - entry_k[0]) > rad or abs(fk[2] - entry_k[2]) > rad:
                    continue
            dist = abs(fk[0] - cx) + 1.4 * abs(fk[1] - cy) + abs(fk[2] - cz)
            hint_pen = 0.0
            if hints:
                hint_pen = 0.25 * min(abs(fk[0] - hx) + abs(fk[1] - hy) + abs(fk[2] - hz) for hx, hy, hz in hints)
            score = dist - 1.2 * fnode.unseen_neighbors - 0.1 * fnode.clearance + hint_pen + 8.0 * fnode.fail_count
            candidates.append((score, fnode))
        candidates.sort(key=lambda pair: (pair[0], abs(pair[1].pos[0] - cx) + abs(pair[1].pos[2] - cz)))
        return [f for _, f in candidates[: max(1, int(limit))]]

    def query_pending_ores(
        self,
        cur_pos: Sequence[int],
        targets: Optional[Mapping[str, Mapping[str, Any]]],
        *,
        best_tool_tier: int = 0,
        harvested_counts: Optional[Mapping[str, int]] = None,
        entry_pos: Optional[Sequence[int]] = None,
        max_radius: Optional[int] = None,
    ) -> List[OreNode]:
        """Return known mineable ore blocks stored in the 3D registry ordered by 3D distance."""
        cx, cy, cz = norm_coord(cur_pos)
        entry_k = norm_coord(entry_pos) if entry_pos is not None else None
        rad = int(max_radius) if max_radius is not None else None
        out: List[Tuple[float, OreNode]] = []
        for ok, ore in list(self.ore_registry.items()):
            if ore.status != "pending" or ore.fail_count >= 2:
                continue
            cur_name = self.voxels.get(ok)
            if cur_name is not None and cur_name not in ORE_DROP_MAP:
                ore.status = "mined"
                continue
            if entry_k is not None and rad is not None:
                if abs(ok[0] - entry_k[0]) > rad or abs(ok[2] - entry_k[2]) > rad:
                    continue
            if targets is not None:
                act, _, _ = evaluate_ore_block(
                    ore.name,
                    targets,
                    harvested_counts=harvested_counts or {},
                    best_tool_tier=best_tool_tier,
                )
                if act != "mine":
                    continue
            if self.block_cache:
                if not has_exposed_air_face(ok, lambda p: self.read_block(p), block_cache=self.block_cache):
                    continue
                if not ore_cell_hazard_free(ok, lambda p: self.read_block(p), block_cache=self.block_cache):
                    ore.status = "skipped_hazard"
                    continue
            dist = abs(ok[0] - cx) + 1.4 * abs(ok[1] - cy) + abs(ok[2] - cz)
            out.append((dist, ore))
        out.sort(key=lambda pair: pair[0])
        return [o for _, o in out]

    def ingest_around(
        self,
        center: Sequence[int],
        read_fn: Callable[[Sequence[int]], Dict[str, Any]],
        *,
        radius: int = 4,
        targets: Optional[Mapping[str, Mapping[str, Any]]] = None,
        harvested_counts: Optional[Mapping[str, int]] = None,
        best_tool_tier: int = 0,
    ) -> int:
        """Scan blocks inside local box around center into 3D memory and register exposed ores."""
        cx, cy, cz = norm_coord(center)
        r = max(1, int(radius))
        new_ores = 0
        for dx in range(-r, r + 1):
            for dz in range(-r, r + 1):
                if abs(dx) + abs(dz) > r + 2:
                    continue
                for dy in range(-2, 5):
                    p = (cx + dx, cy + dy, cz + dz)
                    blk = self.read_block(p, read_fn)
                    bname = str(blk.get("name") or "air")
                    if bname in ORE_DROP_MAP and p not in self.ore_registry:
                        if has_exposed_air_face(p, lambda q: self.read_block(q, read_fn), block_cache=self.block_cache):
                            if not ore_cell_hazard_free(p, lambda q: self.read_block(q, read_fn), block_cache=self.block_cache):
                                self.ore_registry[p] = OreNode(pos=p, name=bname, status="skipped_hazard")
                            elif targets is not None:
                                act, _, _ = evaluate_ore_block(
                                    bname,
                                    targets,
                                    harvested_counts=harvested_counts or {},
                                    best_tool_tier=best_tool_tier,
                                )
                                st = "pending" if act == "mine" else act
                                self.ore_registry[p] = OreNode(pos=p, name=bname, status=st)
                                if st == "pending":
                                    new_ores += 1
                            else:
                                self.ore_registry[p] = OreNode(pos=p, name=bname, status="pending")
                                new_ores += 1
        return new_ores

    def stats(self) -> Dict[str, int]:
        pending_ores = sum(1 for o in self.ore_registry.values() if o.status == "pending")
        mined_ores = sum(1 for o in self.ore_registry.values() if o.status == "mined")
        return {
            "voxels": len(self.voxels),
            "visited_stances": len(self.visited_stances),
            "scanned_cells": len(self.scanned_cells),
            "open_frontiers": len(self.open_frontiers),
            "ores_pending": pending_ores,
            "ores_mined": mined_ores,
            "blocked_edges": len(self.blocked_edges),
        }

    def save(self) -> None:
        """Persist voxels (chunk-compressed), visited stances, scanned coverage, open frontiers, ores & blocked edges."""
        if self.db_path is None:
            return
        now = time.time()
        chunk_map: Dict[Coord3D, Dict[str, str]] = {}
        for ckey in self._dirty_chunks:
            chunk_map[ckey] = {}
        if self._dirty_chunks:
            for (x, y, z), bname in self.voxels.items():
                ck = _chunk_key(x, y, z)
                if ck in self._dirty_chunks:
                    chunk_map[ck][f"{x},{y},{z}"] = bname

        state_payloads = {
            "visited_stances": [[list(k), cnt] for k, cnt in self.visited_stances.items()],
            "scanned_cells": [list(k) for k in self.scanned_cells],
            "open_frontiers": [asdict(fn) for fn in self.open_frontiers.values()],
            "ore_registry": [asdict(on) for on in self.ore_registry.values()],
            "blocked_edges": [[list(e[0]), list(e[1]), cnt] for e, cnt in self.blocked_edges.items()],
            "placed_scaffolds": [list(k) for k in self.placed_scaffolds],
        }

        with closing(self._connect()) as conn:
            for ck, mapping in chunk_map.items():
                blob = zlib.compress(json.dumps(mapping, separators=(",", ":")).encode("utf-8"), level=3)
                conn.execute(
                    "INSERT OR REPLACE INTO chunks (cx, cy, cz, payload, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (ck[0], ck[1], ck[2], sqlite3.Binary(blob), now),
                )
            for s_key, obj in state_payloads.items():
                blob = zlib.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), level=3)
                conn.execute(
                    "INSERT OR REPLACE INTO state_blobs (key, payload, updated_at) VALUES (?, ?, ?)",
                    (s_key, sqlite3.Binary(blob), now),
                )
            conn.commit()
        self._dirty_chunks.clear()

    def load(self) -> None:
        """Restore persisted voxels and exploration topology from SQLite."""
        if self.db_path is None or not self.db_path.exists():
            return
        with closing(self._connect()) as conn:
            chunk_rows = conn.execute("SELECT payload FROM chunks").fetchall()
            for (blob,) in chunk_rows:
                raw_map = json.loads(zlib.decompress(bytes(blob)).decode("utf-8"))
                for coord_str, bname in raw_map.items():
                    xs, ys, zs = coord_str.split(",")
                    coord = (int(xs), int(ys), int(zs))
                    if coord not in self.voxels:
                        self.voxels[coord] = str(bname)
                        self.block_cache[coord] = {"name": str(bname), "properties": {}, "pos": [coord[0], coord[1], coord[2]]}

            blob_rows = {
                k: json.loads(zlib.decompress(bytes(payload)).decode("utf-8"))
                for k, payload in conn.execute("SELECT key, payload FROM state_blobs").fetchall()
            }

        for pos_list, cnt in blob_rows.get("visited_stances", []):
            k = norm_coord(pos_list)
            self.visited_stances[k] = max(self.visited_stances.get(k, 0), int(cnt))
        for pos_list in blob_rows.get("scanned_cells", []):
            self.scanned_cells.add(norm_coord(pos_list))
        for fdict in blob_rows.get("open_frontiers", []):
            pos = norm_coord(fdict["pos"])
            if pos not in self.scanned_cells and pos not in self.visited_stances:
                self.open_frontiers[pos] = FrontierNode(
                    pos=pos,
                    unseen_neighbors=int(fdict.get("unseen_neighbors", 1)),
                    clearance=int(fdict.get("clearance", 0)),
                    requires_op=str(fdict.get("requires_op", "walk")),
                    fail_count=int(fdict.get("fail_count", 0)),
                    discovered_at=float(fdict.get("discovered_at", 0.0)),
                    last_score=float(fdict.get("last_score", 0.0)),
                )
        for odict in blob_rows.get("ore_registry", []):
            pos = norm_coord(odict["pos"])
            if pos not in self.ore_registry:
                self.ore_registry[pos] = OreNode(
                    pos=pos,
                    name=str(odict.get("name") or "ore"),
                    status=str(odict.get("status") or "pending"),
                    fail_count=int(odict.get("fail_count", 0)),
                )
        for u_list, v_list, cnt in blob_rows.get("blocked_edges", []):
            e = (norm_coord(u_list), norm_coord(v_list))
            self.blocked_edges[e] = max(self.blocked_edges.get(e, 0), int(cnt))
        for pos_list in blob_rows.get("placed_scaffolds", []):
            self.placed_scaffolds.add(norm_coord(pos_list))
        self._dirty_chunks.clear()

    def reset(self) -> None:
        """Clear both in-memory 3D world state and on-disk SQLite tables."""
        self.block_cache.clear()
        self.voxels.clear()
        self._dirty_chunks.clear()
        self.visited_stances.clear()
        self.scanned_cells.clear()
        self.open_frontiers.clear()
        self.ore_registry.clear()
        self.blocked_edges.clear()
        self.placed_scaffolds.clear()
        if self.db_path is not None and self.db_path.exists():
            with closing(self._connect()) as conn:
                conn.execute("DELETE FROM chunks")
                conn.execute("DELETE FROM state_blobs")
                conn.commit()
