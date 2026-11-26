"""L3b Deterministic ReturnToSurfaceTask: safely staircases out of caves/mines back to open sky."""

from __future__ import annotations

from v2.bt.blackboard import Blackboard
from v2.domain import (
    AIR_BLOCKS,
    Coord3,
    HAZARD_BLOCKS,
    LEAF_BLOCKS,
    LIQUID_BLOCKS,
    NON_SOLID_BLOCKS,
    SCAFFOLD_BLOCKS,
    block_coord,
    pick_best_item,
)
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.primitives.base import PrimitiveContext
from v2.primitives.dig_collect import DigCollectPrimitive
from v2.primitives.navigate import NavigatePrimitive
from v2.primitives.place_build import PlaceBuildPrimitive
from v2.tasks.base import Task


_PROTECTED_SURFACE_BLOCKS = frozenset({
    "bedrock", "obsidian", "crafting_table", "furnace", "chest",
    "blast_furnace", "smoker", "nether_portal",
})


def is_underground(bb: Blackboard, gateway: VirtualWorldGateway, *, min_surface_y: int = 58) -> bool:
    """Return True when overworld bot sits below min_surface_y beneath a solid ceiling."""
    if bb.dimension != "overworld":
        return False
    fx, fy, fz = block_coord(bb.pos)
    if fy >= min_surface_y:
        return False
    column = [(fx, fy + dy, fz) for dy in range(2, 8)]
    fetched = gateway.prefetch_blocks(bb.dimension, column, max_age=4.0)
    return any(
        (ent := fetched.get(c)) is not None and ent.name not in (AIR_BLOCKS | LEAF_BLOCKS | NON_SOLID_BLOCKS)
        for c in column
    )


class ReturnToSurfaceTask(Task):
    """Steps up a safe 3-high staircase until reaching surface Y or continuous open sky."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        *,
        target_y: int = 62,
        max_steps: int = 96,
    ) -> None:
        super().__init__("return_to_surface")
        self.ctx = ctx
        self.target_y = int(target_y)
        self.max_steps = max(1, int(max_steps))
        self._dig = DigCollectPrimitive(ctx)
        self._nav = NavigatePrimitive(ctx)
        self._place = PlaceBuildPrimitive(ctx)
        self._dir_idx: int = 0
        self._steps: int = 0
        self._dirs = ((1, 0), (0, 1), (-1, 0), (0, -1))

    def is_finished(self, bb: Blackboard) -> bool:
        if bb.dimension != "overworld":
            return True
        fx, fy, fz = block_coord(bb.pos)
        if fy >= self.target_y:
            return True
        sky_col = [(fx, fy + dy, fz) for dy in range(2, 12)]
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, sky_col, max_age=3.0)
        return all(
            (ent := fetched.get(c)) is not None and ent.name in (AIR_BLOCKS | LEAF_BLOCKS)
            for c in sky_col
        )

    def _is_safe_to_dig(self, bb: Blackboard, cell: Coord3) -> bool:
        cx, cy, cz = cell
        probe = [(cx, cy, cz), (cx, cy + 1, cz), (cx + 1, cy, cz), (cx - 1, cy, cz), (cx, cy, cz + 1), (cx, cy, cz - 1)]
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, probe, max_age=3.0)
        target_ent = fetched.get((cx, cy, cz))
        if target_ent is not None and (
            target_ent.name in _PROTECTED_SURFACE_BLOCKS or target_ent.name.endswith("_bed")
        ):
            return False
        return not any(
            (ent := fetched.get(c)) is not None and (ent.name in HAZARD_BLOCKS or ent.name in LIQUID_BLOCKS)
            for c in probe
        )

    def on_tick(self, bb: Blackboard) -> str:
        self._steps += 1
        if self._steps > self.max_steps:
            bb.last_failure_reason = "surface_escape_budget_exhausted"
            return "NEEDS_AI"

        fx, fy, fz = block_coord(bb.pos)
        for _ in range(len(self._dirs)):
            dx, dz = self._dirs[self._dir_idx % len(self._dirs)]
            # Clear overhead at current column + headroom at next elevated step (fy+1, fy+2)
            clearance = [
                (fx, fy + 2, fz),
                (fx + dx, fy + 2, fz + dz),
                (fx + dx, fy + 1, fz + dz),
            ]
            floor_cell = (fx + dx, fy, fz + dz)
            fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, clearance + [floor_cell], max_age=3.0)

            blocked = False
            for cell in clearance:
                ent = fetched.get(cell)
                if ent is not None and ent.name not in (AIR_BLOCKS | LEAF_BLOCKS):
                    if ent.name in (HAZARD_BLOCKS | LIQUID_BLOCKS) or not self._is_safe_to_dig(bb, cell):
                        blocked = True
                        break
                    if self.ctx.gateway.unreachable.is_blocked(bb.dimension, cell, "dig"):
                        blocked = True
                        break
                    d_out = self._dig.dig_block(bb, cell, collect=False)
                    return "RUNNING" if d_out.status in {"OK", "PROGRESSED"} else d_out.status
            if blocked:
                self._dir_idx += 1
                continue

            floor_ent = fetched.get(floor_cell)
            if floor_ent is not None and floor_ent.name in HAZARD_BLOCKS:
                self._dir_idx += 1
                continue
            if floor_ent is None or floor_ent.name in NON_SOLID_BLOCKS:
                scaffold = pick_best_item(bb.inventory_counts, SCAFFOLD_BLOCKS)
                if scaffold is None or self.ctx.gateway.unreachable.is_blocked(bb.dimension, floor_cell, "place"):
                    self._dir_idx += 1
                    continue
                p_out = self._place.place_at(bb, scaffold, floor_cell)
                return "RUNNING" if p_out.status in {"OK", "PROGRESSED"} else p_out.status

            n_out = self._nav.step_to(bb, (fx + dx + 0.5, float(fy + 1), fz + dz + 0.5), tolerance=0.6)
            if n_out.status == "FAILED_LOCAL":
                self._dir_idx += 1
                continue
            return "RUNNING" if n_out.status in {"OK", "PROGRESSED"} else n_out.status

        bb.last_failure_reason = "surface_escape:all_directions_blocked"
        return "NEEDS_AI"
