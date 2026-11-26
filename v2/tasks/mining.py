"""L3b MineResourceTask: cave-first ore harvesting + safe 3-high staircase descent to Y=16 / Y=-54 + 2-high tunneling."""

from __future__ import annotations

from typing import Optional

from v2.bt.blackboard import Blackboard
from v2.domain import AIR_BLOCKS, Coord3, HAZARD_BLOCKS, LIQUID_BLOCKS, NON_SOLID_BLOCKS, block_coord
from v2.primitives.base import PrimitiveContext
from v2.primitives.dig_collect import DigCollectPrimitive
from v2.primitives.navigate import NavigatePrimitive
from v2.tasks.acquire import AcquireItemTask, _has_pickaxe_tier
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue


class MineResourceTask(Task):
    """Cave-first resource mining with safe step-driven Y-band descent and 2-high branch tunneling (<= 1 write/tick)."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        catalogue: TaskCatalogue,
        item: str,
        count: int,
        *,
        target_y: Optional[int] = None,
    ) -> None:
        super().__init__(f"mine:{item}x{count}")
        self.ctx = ctx
        self.catalogue = catalogue
        self.item = item
        self.count = max(1, int(count))
        gather = catalogue.gathering.get(item)
        self.target_y = target_y if target_y is not None else (gather.preferred_y if gather and gather.preferred_y is not None else 16)
        self._dig = DigCollectPrimitive(ctx)
        self._nav = NavigatePrimitive(ctx)
        self._dir_idx: int = 0
        self._dirs = ((1, 0), (0, 1), (-1, 0), (0, -1))

    def is_finished(self, bb: Blackboard) -> bool:
        return bb.inventory_counts.get(self.item, 0) >= self.count

    PROTECTED_STATIONS = frozenset({
        "crafting_table", "furnace", "chest", "blast_furnace", "smoker",
        "obsidian", "nether_portal",
    })

    def _is_safe_to_dig(self, bb: Blackboard, cell: Coord3) -> bool:
        cx, cy, cz = cell
        check = [(cx, cy, cz), (cx, cy + 1, cz), (cx + 1, cy, cz), (cx - 1, cy, cz), (cx, cy, cz + 1), (cx, cy, cz - 1)]
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, check, max_age=3.0)
        target_ent = fetched.get((cx, cy, cz))
        if target_ent is not None and (target_ent.name in self.PROTECTED_STATIONS or target_ent.name.endswith("_bed")):
            return False
        for c in check:
            ent = fetched.get(c)
            if ent is not None and (ent.name in HAZARD_BLOCKS or ent.name in LIQUID_BLOCKS):
                return False
        return True

    def on_tick(self, bb: Blackboard) -> str:
        gather = self.catalogue.gathering.get(self.item)
        if gather is None:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, self.item, self.count))

        # 1) Ensure pickaxe tier (>= min_pickaxe, never downgrade iron_pickaxe -> stone_pickaxe!)
        if gather.min_pickaxe and not _has_pickaxe_tier(bb.inventory_counts, gather.min_pickaxe):
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, gather.min_pickaxe, 1))

        # 2) Cave-first: scan exposed ore veins within 32m near current elevation (ignore sealed far-Y pockets)
        out = self._dig.dig_nearest_of_types(bb, gather.block_types, radius=24, collect=True, max_dy=4.0)
        if out.status in {"OK", "PROGRESSED"}:
            return "RUNNING"

        fx, fy, fz = block_coord(bb.pos)

        # 3) Step-driven staircase descent toward target_y: try all 4 cardinal directions in 1 tick (never spin in place!)
        if fy > self.target_y:
            for _ in range(len(self._dirs)):
                dx, dz = self._dirs[self._dir_idx % len(self._dirs)]
                stair_cells = [(fx + dx, fy + 1, fz + dz), (fx + dx, fy, fz + dz), (fx + dx, fy - 1, fz + dz)]
                floor_after = (fx + dx, fy - 2, fz + dz)
                fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, stair_cells + [floor_after], max_age=3.0)
                floor_ent = fetched.get(floor_after)
                if floor_ent is None or floor_ent.name in NON_SOLID_BLOCKS or floor_ent.name in HAZARD_BLOCKS:
                    self._dir_idx += 1
                    continue
                blocked_by_hazard = False
                for cell in stair_cells:
                    ent = fetched.get(cell)
                    if ent is not None and ent.name not in AIR_BLOCKS:
                        if not self._is_safe_to_dig(bb, cell):
                            blocked_by_hazard = True
                            break
                        d_out = self._dig.dig_block(bb, cell, collect=True)
                        return "RUNNING" if d_out.status in {"OK", "PROGRESSED"} else d_out.status
                if blocked_by_hazard:
                    self._dir_idx += 1
                    continue
                n_out = self._nav.step_to(bb, (fx + dx + 0.5, float(fy - 1), fz + dz + 0.5), tolerance=0.6)
                return "RUNNING" if n_out.status in {"OK", "PROGRESSED"} else n_out.status
            # All 4 staircase directions blocked by drops/hazards -> try horizontal 2-high tunnel instead of spinning!

        # 4) Horizontal 2-high tunnel (head then feet) trying all 4 directions in 1 tick
        for _ in range(len(self._dirs)):
            dx, dz = self._dirs[self._dir_idx % len(self._dirs)]
            head_cell = (fx + dx, fy + 1, fz + dz)
            feet_cell = (fx + dx, fy, fz + dz)
            floor_cell = (fx + dx, fy - 1, fz + dz)
            fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, [head_cell, feet_cell, floor_cell], max_age=3.0)
            floor_ent = fetched.get(floor_cell)
            if floor_ent is None or floor_ent.name in NON_SOLID_BLOCKS or floor_ent.name in HAZARD_BLOCKS:
                self._dir_idx += 1
                continue
            blocked_by_hazard = False
            for cell in (head_cell, feet_cell):
                ent = fetched.get(cell)
                if ent is not None and ent.name not in AIR_BLOCKS:
                    if not self._is_safe_to_dig(bb, cell):
                        blocked_by_hazard = True
                        break
                    d_out = self._dig.dig_block(bb, cell, collect=True)
                    return "RUNNING" if d_out.status in {"OK", "PROGRESSED"} else d_out.status
            if blocked_by_hazard:
                self._dir_idx += 1
                continue
            n_out = self._nav.step_to(bb, (fx + dx + 0.5, float(fy), fz + dz + 0.5), tolerance=0.6)
            return "RUNNING" if n_out.status in {"OK", "PROGRESSED"} else n_out.status

        bb.last_failure_reason = "mine:all_directions_blocked"
        return "NEEDS_AI"
