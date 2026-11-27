"""L3b Voyager-style ExploreUntilTask: spiral/sector exploration terminating as soon as predicate holds."""

from __future__ import annotations

import math
import time
from typing import Callable, Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import (
    HAZARD_BLOCKS,
    NON_SOLID_BLOCKS,
    PASS_THROUGH_BLOCKS,
    WATER_BLOCKS,
    block_coord,
    dist2_xz,
)
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.primitives.base import PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive
from v2.tasks.base import Task
from v2.tasks.surface import ReturnToSurfaceTask, is_underground


class ExploreUntilTask(Task):
    """Explores outward in bounded cardinal/diagonal waypoints until predicate(bb, gw) is True."""

    DIRECTIONS: Tuple[Tuple[float, float], ...] = (
        (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0),
        (0.707, 0.707), (-0.707, 0.707), (-0.707, -0.707), (0.707, -0.707),
    )

    def __init__(
        self,
        ctx: PrimitiveContext,
        predicate: Callable[[Blackboard, VirtualWorldGateway], bool],
        *,
        name: str = "explore_until",
        step_distance: float = 20.0,
        max_steps: int = 8,
        prefer_surface: bool = True,
    ) -> None:
        super().__init__(name)
        self.ctx = ctx
        self.predicate = predicate
        self.step_distance = float(step_distance)
        self.max_steps = max(1, int(max_steps))
        self.prefer_surface = bool(prefer_surface)
        self._nav = NavigatePrimitive(ctx)
        self._origin: Optional[Tuple[float, float, float]] = None
        self._sector_idx: int = 0
        self._sector_initialized: bool = False
        self._steps_taken: int = 0
        self._active_wp: Optional[Tuple[float, float, float]] = None

    def is_finished(self, bb: Blackboard) -> bool:
        return bool(self.predicate(bb, self.ctx.gateway))

    def _mark_sector_excluded(self, bb: Blackboard, x: float, z: float, *, ttl: float = 60.0) -> None:
        now = time.time()
        excluded: Dict[Tuple[int, int], float] = {
            k: v for k, v in (bb.get("excluded_sectors") or {}).items() if v > now
        }
        sec = (math.floor(float(x) / 16.0), math.floor(float(z) / 16.0))
        excluded[sec] = now + ttl
        bb.set("excluded_sectors", excluded, writer=self.name)

    def _select_waypoint(self, bb: Blackboard) -> Tuple[int, Tuple[float, float, float]]:
        assert self._origin is not None
        excluded: Dict[Tuple[int, int], float] = bb.get("excluded_sectors") or {}
        now = time.time()
        candidates: List[Tuple[int, float, float, Tuple[float, float, float]]] = []
        max_scan = len(self.DIRECTIONS) * 2
        for offset in range(max_scan):
            idx = self._sector_idx + offset
            dx, dz = self.DIRECTIONS[idx % len(self.DIRECTIONS)]
            ring = 1 + (idx // len(self.DIRECTIONS))
            cand = (
                self._origin[0] + dx * self.step_distance * ring,
                self._origin[1],
                self._origin[2] + dz * self.step_distance * ring,
            )
            sec = (math.floor(cand[0] / 16.0), math.floor(cand[2] / 16.0))
            if excluded.get(sec, 0.0) > now:
                continue
            candidates.append((idx, dx, dz, cand))
            if len(candidates) >= len(self.DIRECTIONS):
                break

        if not candidates:
            dx, dz = self.DIRECTIONS[self._sector_idx % len(self.DIRECTIONS)]
            ring = 1 + (self._sector_idx // len(self.DIRECTIONS))
            return (
                self._sector_idx,
                (
                    self._origin[0] + dx * self.step_distance * ring,
                    self._origin[1],
                    self._origin[2] + dz * self.step_distance * ring,
                ),
            )

        fx, fy, fz = block_coord(bb.pos)
        probe_coords = []
        for _, dx, dz, _ in candidates:
            for dist in (8.0, 16.0):
                px = math.floor(bb.pos[0] + dx * dist)
                pz = math.floor(bb.pos[2] + dz * dist)
                for dy in (-1, 0, 1):
                    probe_coords.append((px, fy + dy, pz))

        fetched = (
            self.ctx.gateway.prefetch_blocks(bb.dimension, probe_coords)
            if hasattr(self.ctx.gateway, "prefetch_blocks")
            else {}
        )
        if not isinstance(fetched, dict):
            fetched = {}

        best_full_dry: Optional[Tuple[int, Tuple[float, float, float]]] = None
        best_partial_dry: Optional[Tuple[int, Tuple[float, float, float]]] = None
        best_neutral: Optional[Tuple[int, Tuple[float, float, float]]] = None
        fallback_cand = (candidates[0][0], candidates[0][3])

        for idx, dx, dz, cand in candidates:
            water_hits = 0
            dry_hits = 0
            for dist in (8.0, 16.0):
                px = math.floor(bb.pos[0] + dx * dist)
                pz = math.floor(bb.pos[2] + dz * dist)
                b_below = fetched.get((px, fy - 1, pz))
                b_feet = fetched.get((px, fy, pz))
                b_head = fetched.get((px, fy + 1, pz))
                has_water = any(
                    ent is not None and getattr(ent, "name", None) in WATER_BLOCKS
                    for ent in (b_below, b_feet, b_head)
                )
                if has_water:
                    water_hits += 1
                    continue
                standable_low = (
                    b_below is not None
                    and getattr(b_below, "name", None) not in NON_SOLID_BLOCKS
                    and getattr(b_below, "name", None) not in HAZARD_BLOCKS
                    and (b_feet is None or getattr(b_feet, "name", None) in PASS_THROUGH_BLOCKS)
                )
                standable_high = (
                    b_feet is not None
                    and getattr(b_feet, "name", None) not in NON_SOLID_BLOCKS
                    and getattr(b_feet, "name", None) not in HAZARD_BLOCKS
                    and (b_head is None or getattr(b_head, "name", None) in PASS_THROUGH_BLOCKS)
                )
                if standable_low or standable_high:
                    dry_hits += 1

            if water_hits > 0 and dry_hits == 0:
                self._mark_sector_excluded(bb, cand[0], cand[2], ttl=60.0)
                continue
            if dry_hits == 2 and water_hits == 0 and best_full_dry is None:
                best_full_dry = (idx, cand)
            elif dry_hits >= 1 and water_hits == 0 and best_partial_dry is None:
                best_partial_dry = (idx, cand)
            elif best_neutral is None:
                best_neutral = (idx, cand)

        return best_full_dry or best_partial_dry or best_neutral or fallback_cand

    def on_tick(self, bb: Blackboard) -> str:
        if self.prefer_surface and is_underground(bb, self.ctx.gateway):
            return self.run_child(bb, ReturnToSurfaceTask(self.ctx))
        if self._origin is None:
            self._origin = bb.pos
        if not self._sector_initialized:
            self._sector_idx = int(bb.get("explore_sector_cursor", 0) or 0)
            self._sector_initialized = True
        if self._steps_taken >= self.max_steps:
            bb.last_failure_reason = f"{self.name}:explore_budget_exhausted"
            return "NEEDS_AI"

        if self._active_wp is None:
            chosen_idx, wp = self._select_waypoint(bb)
            if chosen_idx != self._sector_idx:
                self._sector_idx = chosen_idx
                bb.set("explore_sector_cursor", self._sector_idx + 1, writer=self.name)
            self._active_wp = wp
        else:
            wp = self._active_wp

        if dist2_xz(bb.pos, wp) <= 3.0:
            self._sector_idx += 1
            self._steps_taken += 1
            self._active_wp = None
            bb.set("explore_sector_cursor", self._sector_idx, writer=self.name)
            return "RUNNING"

        out = self._nav.step_to(bb, wp, tolerance=3.0, xz_only=True)
        if out.status in {"OK", "FAILED_LOCAL"}:
            self._sector_idx += 1
            self._steps_taken += 1
            self._active_wp = None
            bb.set("explore_sector_cursor", self._sector_idx, writer=self.name)
            return "RUNNING"
        bb.set("explore_sector_cursor", self._sector_idx + 1, writer=self.name)
        return "RUNNING"

    def stop(self, bb: Blackboard) -> None:
        self._active_wp = None
        super().stop(bb)
