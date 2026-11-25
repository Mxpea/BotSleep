"""L1 VirtualWorldGateway: 3-source write-through 3D voxel cache + exact upstream bucket LOS solver.

Three-source voxel fusion:
1. EXACT: direct `get_block` reads and confirmed `dig`/`place` write-through updates.
2. SCAN: typed `find_blocks` exposed-block results.
3. INFERRED_PASSABLE: player feet & head cells whenever `status.alive` and not suffocating.

Safety freshness redline:
- Unknown writes invalidate a 3x3x3 neighborhood (`must_reverify=True`).
- Safety-critical queries near gravity blocks (sand/gravel) or fluids force fresh `get_block`
  if cache age exceeds `SAFETY_TTL_SECONDS` (3.0s).
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import math
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from v2.domain import (
    AIR_BLOCKS,
    Coord3,
    GRAVITY_BLOCKS,
    LIQUID_BLOCKS,
    NON_SOLID_BLOCKS,
    SOFT_OCCLUDERS,
    block_coord,
    dist3,
)
from v2.gateway.unreachable import BlockUnreachableTracker
from v2.kernel.client import APIError, MinecraftAgentClient
from v2.kernel.storage import UnifiedWorldDB


def ray_cells(start: Sequence[float], end: Sequence[float]) -> List[Coord3]:
    """Supercover DDA matching upstream mc-agent-api / block_geometry ray traversal."""
    cell = [math.floor(float(v)) for v in start]
    final = (math.floor(float(end[0])), math.floor(float(end[1])), math.floor(float(end[2])))
    out: List[Coord3] = [(cell[0], cell[1], cell[2])]
    delta = [float(end[i]) - float(start[i]) for i in range(3)]
    step = [1 if v > 0 else -1 if v < 0 else 0 for v in delta]
    interval = [abs(1.0 / v) if v else math.inf for v in delta]
    crossing = [
        ((cell[i] + (1 if step[i] > 0 else 0) - float(start[i])) / delta[i]) if step[i] else math.inf
        for i in range(3)
    ]
    for _ in range(256):
        if (cell[0], cell[1], cell[2]) == final:
            break
        nearest = min(crossing)
        if nearest > 1.0 + 1e-10:
            break
        axes = [i for i in range(3) if abs(crossing[i] - nearest) <= 1e-10]
        for length in range(1, len(axes) + 1):
            for subset in itertools.combinations(axes, length):
                touched = cell.copy()
                for axis in subset:
                    touched[axis] += step[axis]
                out.append((touched[0], touched[1], touched[2]))
        for axis in axes:
            cell[axis] += step[axis]
            crossing[axis] += interval[axis]
    return out


@dataclass
class VoxelEntry:
    name: str
    properties: Dict[str, Any]
    source: str  # "EXACT" | "SCAN" | "INFERRED_PASSABLE"
    observed_at: float
    must_reverify: bool = False


@dataclass(frozen=True)
class BucketSightPlan:
    status: str  # "READY" | "CLEAR_OCCLUDER" | "UNREACHABLE"
    target_pos: Coord3
    stand_pos: Optional[Coord3] = None
    aim_point: Optional[Tuple[float, float, float]] = None
    occluder_pos: Optional[Coord3] = None
    occluder_name: Optional[str] = None
    reason: str = ""


class VirtualWorldGateway:
    """Write-through 3D voxel proxy over MinecraftAgentClient + UnifiedWorldDB."""

    DEFAULT_TTL_SECONDS = 25.0
    SAFETY_TTL_SECONDS = 3.0

    def __init__(
        self,
        client: MinecraftAgentClient,
        *,
        db: Optional[UnifiedWorldDB] = None,
        unreachable: Optional[BlockUnreachableTracker] = None,
    ) -> None:
        self.client = client
        self.db = db
        self.unreachable = unreachable or BlockUnreachableTracker(db=db)
        self._voxels: Dict[Tuple[str, Coord3], VoxelEntry] = {}
        self.cache_hits = 0
        self.cache_misses = 0

    @property
    def cache_hit_ratio(self) -> float:
        total = self.cache_hits + self.cache_misses
        return (self.cache_hits / total) if total > 0 else 0.0

    # ---- Three-Source Ingestion & Write-Through Updates ----

    def put_voxel(
        self,
        dimension: str,
        coord: Coord3,
        name: str,
        *,
        properties: Optional[Dict[str, Any]] = None,
        source: str = "EXACT",
        now: Optional[float] = None,
    ) -> None:
        ts = time.monotonic() if now is None else now
        c = (int(coord[0]), int(coord[1]), int(coord[2]))
        self._voxels[(dimension, c)] = VoxelEntry(
            name=name,
            properties=dict(properties or {}),
            source=source,
            observed_at=ts,
            must_reverify=False,
        )

    def ingest_status_stance(self, status: Dict[str, Any]) -> None:
        """Source 3 (INFERRED_PASSABLE): mark feet & head cells passable when alive and not in lava."""
        if not status.get("alive", False) or status.get("in_lava", False):
            return
        raw_pos = status.get("pos")
        if not isinstance(raw_pos, (list, tuple)) or len(raw_pos) != 3:
            return
        dim = str(status.get("dimension") or "overworld")
        fx, fy, fz = block_coord(raw_pos)
        feet_name = "water" if status.get("in_water") else "air"
        head_name = "water" if status.get("submerged") else "air"
        now = time.monotonic()
        for coord, nm in (((fx, fy, fz), feet_name), ((fx, fy + 1, fz), head_name)):
            existing = self._voxels.get((dim, coord))
            if existing is None or existing.source != "EXACT" or existing.must_reverify:
                self.put_voxel(dim, coord, nm, source="INFERRED_PASSABLE", now=now)

    def ingest_find_blocks(self, dimension: str, rows: Sequence[Dict[str, Any]]) -> None:
        """Source 2 (SCAN): ingest exposed blocks returned by typed find_blocks."""
        now = time.monotonic()
        for row in rows:
            raw_pos = row.get("pos")
            name = row.get("name")
            if not isinstance(raw_pos, (list, tuple)) or len(raw_pos) != 3 or not isinstance(name, str):
                continue
            coord = (int(math.floor(raw_pos[0])), int(math.floor(raw_pos[1])), int(math.floor(raw_pos[2])))
            props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
            self.put_voxel(dimension, coord, name, properties=props, source="SCAN", now=now)

    def write_through_dig(self, dimension: str, coord: Coord3) -> None:
        """Confirmed dig sets the target voxel to air and flags blocks directly above for gravity re-check."""
        self.put_voxel(dimension, coord, "air", source="EXACT")
        above = (coord[0], coord[1] + 1, coord[2])
        entry = self._voxels.get((dimension, above))
        if entry is not None and entry.name in GRAVITY_BLOCKS:
            entry.must_reverify = True

    def write_through_place(self, dimension: str, coord: Coord3, block_name: str) -> None:
        self.put_voxel(dimension, coord, block_name, source="EXACT")

    def invalidate_unknown_radius(self, dimension: str, center: Coord3, radius: int = 1) -> None:
        """Invalidate 3x3x3 neighborhood when a write returns action_state=unknown."""
        cx, cy, cz = int(center[0]), int(center[1]), int(center[2])
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    entry = self._voxels.get((dimension, (cx + dx, cy + dy, cz + dz)))
                    if entry is not None:
                        entry.must_reverify = True

    # ---- Cached & Safety-Critical Block Queries ----

    def _has_hazard_or_gravity_neighbor(self, dimension: str, coord: Coord3) -> bool:
        cx, cy, cz = coord
        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            e = self._voxels.get((dimension, (cx + dx, cy + dy, cz + dz)))
            if e is not None and (e.name in GRAVITY_BLOCKS or e.name in LIQUID_BLOCKS):
                return True
        return False

    def peek_block(self, dimension: str, coord: Coord3) -> Optional[VoxelEntry]:
        return self._voxels.get((dimension, (int(coord[0]), int(coord[1]), int(coord[2]))))

    def get_block(
        self,
        dimension: str,
        coord: Coord3,
        *,
        max_age: Optional[float] = None,
        safety_critical: bool = False,
        allow_fetch: bool = True,
    ) -> Optional[VoxelEntry]:
        """Return cached voxel if fresh and safe, otherwise fetch from API and cache."""
        c = (int(coord[0]), int(coord[1]), int(coord[2]))
        now = time.monotonic()
        ttl = self.DEFAULT_TTL_SECONDS if max_age is None else float(max_age)
        entry = self._voxels.get((dimension, c))

        needs_refresh = entry is None or entry.must_reverify or (now - entry.observed_at) > ttl
        if not needs_refresh and safety_critical:
            if (now - entry.observed_at) > self.SAFETY_TTL_SECONDS:
                if entry.name in GRAVITY_BLOCKS or entry.name in LIQUID_BLOCKS or self._has_hazard_or_gravity_neighbor(dimension, c):
                    needs_refresh = True

        if not needs_refresh:
            self.cache_hits += 1
            return entry

        if not allow_fetch:
            return entry

        self.cache_misses += 1
        try:
            env = self.client.get_block(c)
            blk = env.get("result")
        except APIError as err:
            if err.code == "not_loaded":
                return None
            raise
        if not isinstance(blk, dict) or not isinstance(blk.get("name"), str):
            return None
        self.put_voxel(
            dimension,
            c,
            blk["name"],
            properties=blk.get("properties") if isinstance(blk.get("properties"), dict) else {},
            source="EXACT",
            now=time.monotonic(),
        )
        return self._voxels.get((dimension, c))

    def prefetch_blocks(
        self,
        dimension: str,
        coords: Iterable[Coord3],
        *,
        max_age: Optional[float] = None,
    ) -> Dict[Coord3, Optional[VoxelEntry]]:
        """Batch-fetch only cache-missed or stale coordinates via AsyncReadPool."""
        now = time.monotonic()
        ttl = self.DEFAULT_TTL_SECONDS if max_age is None else float(max_age)
        result: Dict[Coord3, Optional[VoxelEntry]] = {}
        missing: List[Coord3] = []
        for raw in coords:
            c = (int(raw[0]), int(raw[1]), int(raw[2]))
            e = self._voxels.get((dimension, c))
            if e is not None and not e.must_reverify and (now - e.observed_at) <= ttl:
                self.cache_hits += 1
                result[c] = e
            else:
                missing.append(c)

        if missing:
            unique_missing = list(dict.fromkeys(missing))
            self.cache_misses += len(unique_missing)
            fetched = self.client.batch_get_blocks(unique_missing)
            ts = time.monotonic()
            for c, blk in fetched.items():
                if isinstance(blk, dict) and isinstance(blk.get("name"), str):
                    self.put_voxel(
                        dimension,
                        c,
                        blk["name"],
                        properties=blk.get("properties") if isinstance(blk.get("properties"), dict) else {},
                        source="EXACT",
                        now=ts,
                    )
                    result[c] = self._voxels[(dimension, c)]
                else:
                    result[c] = None
        return result

    def scan_blocks(
        self,
        dimension: str,
        types: List[str],
        *,
        radius: int = 24,
        limit: int = 64,
        properties: Optional[Dict[str, Any]] = None,
        mature: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Run typed find_blocks (optionally filtered by properties/mature) and populate the voxel cache."""
        rows = self.client.find_blocks(types, radius=radius, limit=limit, properties=properties, mature=mature)
        self.ingest_find_blocks(dimension, rows)
        return rows

    def is_solid(self, dimension: str, coord: Coord3, *, allow_fetch: bool = False) -> bool:
        e = self.get_block(dimension, coord, allow_fetch=allow_fetch)
        return e is not None and e.name not in NON_SOLID_BLOCKS

    def is_passable(self, dimension: str, coord: Coord3, *, allow_fetch: bool = False) -> bool:
        e = self.get_block(dimension, coord, allow_fetch=allow_fetch)
        return e is not None and e.name in NON_SOLID_BLOCKS and e.name != "lava"

    # ---- Exact Upstream useBucket LOS Solver ----

    def plan_liquid_interaction(
        self,
        dimension: str,
        target: Coord3,
        player_pos: Sequence[float],
        *,
        mode: str = "pickup",  # "pickup" (aim y+0.85) | "pour" (aim y-0.01)
    ) -> BucketSightPlan:
        """Find a safe solid stand_pos with clear ray to upstream bucket aim point, or 1 removable occluder."""
        tx, ty, tz = int(target[0]), int(target[1]), int(target[2])
        aim = (tx + 0.5, ty + (0.85 if mode == "pickup" else -0.01), tz + 0.5)

        # Verify target (or support below target for pour)
        verify_coords: List[Coord3] = [(tx, ty, tz)]
        if mode == "pour":
            verify_coords.append((tx, ty - 1, tz))

        # Enumerate candidate solid stand positions around target.
        # Pouring/picking up on upper portal blocks is reachable from below because eye_y = sy + 1.62.
        dy_options = (0, 1, 2, -1, -2, -3) if mode == "pickup" else (0, -1, -2, -3, -4, 1)
        candidates: List[Coord3] = []
        for dx in range(-3, 4):
            for dz in range(-3, 4):
                horiz = math.hypot(dx, dz)
                if horiz < 0.9 or horiz > 2.9:
                    continue
                for dy in dy_options:
                    sx, sy, sz = tx + dx, ty + dy, tz + dz
                    feet = (sx + 0.5, float(sy), sz + 0.5)
                    eye = (sx + 0.5, sy + 1.62, sz + 0.5)
                    if mode == "pickup":
                        if dist3(feet, (tx + 0.5, ty + 0.5, tz + 0.5)) <= 3.6 and dist3(eye, aim) <= 4.45:
                            candidates.append((sx, sy, sz))
                    else:
                        if dist3(eye, aim) <= 4.45:
                            candidates.append((sx, sy, sz))

        py_floor = math.floor(float(player_pos[1]))
        if mode == "pour":
            # For pouring, prefer downward-LOS stances (sy near ty-1 so eye_y = sy+1.62 > ty clears rims/molds)
            candidates.sort(
                key=lambda c: (
                    abs(c[1] - (ty - 1)),
                    abs(c[1] - py_floor),
                    dist3((c[0] + 0.5, float(c[1]), c[2] + 0.5), player_pos),
                )
            )
        else:
            candidates.sort(
                key=lambda c: (
                    abs(c[1] - py_floor),
                    dist3((c[0] + 0.5, float(c[1]), c[2] + 0.5), player_pos),
                )
            )
        # Prefetch stance cells + ray cells for top 56 candidates in ONE batch call!
        top_candidates = candidates[:56]
        needed: List[Coord3] = list(verify_coords)
        ray_map: Dict[Coord3, List[Coord3]] = {}
        for c in top_candidates:
            sx, sy, sz = c
            needed.extend([(sx, sy - 1, sz), (sx, sy, sz), (sx, sy + 1, sz)])
            eye = (sx + 0.5, sy + 1.62, sz + 0.5)
            cells = ray_cells(eye, aim)
            ray_map[c] = cells
            needed.extend(cells)

        fetched = self.prefetch_blocks(dimension, needed, max_age=self.SAFETY_TTL_SECONDS)

        if mode == "pickup":
            t_blk = fetched.get((tx, ty, tz))
            if t_blk is None or t_blk.name not in LIQUID_BLOCKS or str(t_blk.properties.get("level", "0")) != "0":
                return BucketSightPlan("UNREACHABLE", (tx, ty, tz), reason="not_liquid_source")
        else:
            sup = fetched.get((tx, ty - 1, tz))
            if sup is None or sup.name in NON_SOLID_BLOCKS:
                return BucketSightPlan("UNREACHABLE", (tx, ty, tz), reason="no_solid_support")

        best_occluder_plan: Optional[BucketSightPlan] = None
        for c in top_candidates:
            sx, sy, sz = c
            floor_b = fetched.get((sx, sy - 1, sz))
            feet_b = fetched.get((sx, sy, sz))
            head_b = fetched.get((sx, sy + 1, sz))
            if floor_b is None or floor_b.name in NON_SOLID_BLOCKS:
                continue
            if feet_b is None or head_b is None:
                continue
            # Allow standing if feet is air/soft grass (which could also be cleared) and head is air
            if head_b.name not in AIR_BLOCKS:
                continue
            if feet_b.name not in (AIR_BLOCKS | SOFT_OCCLUDERS):
                continue

            blockers: List[Tuple[Coord3, str]] = []
            if feet_b.name not in AIR_BLOCKS:
                blockers.append(((sx, sy, sz), feet_b.name))
            for rcell in ray_map[c]:
                if rcell == (tx, ty, tz) or (mode == "pour" and rcell == (tx, ty - 1, tz)):
                    break
                rb = fetched.get(rcell)
                if rb is None:
                    blockers.append((rcell, "unknown"))
                    break
                if rb.name not in AIR_BLOCKS:
                    if (rcell, rb.name) not in blockers:
                        blockers.append((rcell, rb.name))

            if not blockers:
                return BucketSightPlan("READY", (tx, ty, tz), stand_pos=c, aim_point=aim)

            if len(blockers) == 1 and best_occluder_plan is None:
                bpos, bname = blockers[0]
                if bname in SOFT_OCCLUDERS and not self.unreachable.is_blocked(dimension, bpos, "dig"):
                    best_occluder_plan = BucketSightPlan(
                        "CLEAR_OCCLUDER",
                        (tx, ty, tz),
                        stand_pos=c,
                        aim_point=aim,
                        occluder_pos=bpos,
                        occluder_name=bname,
                        reason=f"occluded_by_{bname}",
                    )

        if best_occluder_plan is not None:
            return best_occluder_plan
        return BucketSightPlan("UNREACHABLE", (tx, ty, tz), reason="no_clear_or_clearable_stance")

    # ---- Persistence Flush to UnifiedWorldDB ----

    def flush_chunk(self, dimension: str, cx: int, cy: int, cz: int) -> None:
        if self.db is None:
            return
        payload: Dict[str, Any] = {}
        x0, y0, z0 = cx * 16, cy * 16, cz * 16
        for (dim, (x, y, z)), entry in self._voxels.items():
            if dim == dimension and x0 <= x < x0 + 16 and y0 <= y < y0 + 16 and z0 <= z < z0 + 16:
                payload[f"{x - x0},{y - y0},{z - z0}"] = entry.name
        if payload:
            self.db.save_voxel_chunk(dimension, cx, cy, cz, payload)
