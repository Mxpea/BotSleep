"""L3b MineResourceTask: cave-first ore harvesting + step-driven descent to Y=16 / Y=-54 + surface return."""

from __future__ import annotations

from typing import Optional

from v2.bt.blackboard import Blackboard
from v2.domain import block_coord
from v2.primitives.base import PrimitiveContext
from v2.primitives.dig_collect import DigCollectPrimitive
from v2.primitives.navigate import NavigatePrimitive
from v2.tasks.acquire import AcquireItemTask
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue


class MineResourceTask(Task):
    """Cave-first resource mining with step-driven Y-band descent (<= 1 write per tick)."""

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

    def is_finished(self, bb: Blackboard) -> bool:
        return bb.inventory_counts.get(self.item, 0) >= self.count

    def on_tick(self, bb: Blackboard) -> str:
        gather = self.catalogue.gathering.get(self.item)
        if gather is None:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, self.item, self.count))

        # 1) Ensure pickaxe tier first
        if gather.min_pickaxe:
            have_tool = bb.inventory_counts.get(gather.min_pickaxe, 0) > 0
            if not have_tool:
                return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, gather.min_pickaxe, 1))

        # 2) Cave-first: scan exposed ore veins within 32m
        out = self._dig.dig_nearest_of_types(bb, gather.block_types, radius=32, collect=True)
        if out.status in {"OK", "PROGRESSED"}:
            return "RUNNING"

        # 3) Step-driven staircase descent toward target_y (1 dig/walk step per tick)
        fx, fy, fz = block_coord(bb.pos)
        if fy > self.target_y:
            step_ahead = (fx + 1, fy, fz)
            step_below = (fx + 1, fy - 1, fz)
            e_ahead = self.ctx.gateway.get_block(bb.dimension, step_ahead, safety_critical=True)
            if e_ahead is not None and e_ahead.name != "air":
                d_out = self._dig.dig_block(bb, step_ahead, collect=True)
                return "RUNNING" if d_out.status in {"OK", "PROGRESSED"} else d_out.status
            e_below = self.ctx.gateway.get_block(bb.dimension, step_below, safety_critical=True)
            if e_below is not None and e_below.name != "air":
                d_out = self._dig.dig_block(bb, step_below, collect=True)
                return "RUNNING" if d_out.status in {"OK", "PROGRESSED"} else d_out.status
            n_out = self._nav.step_to(bb, (fx + 1.5, float(fy - 1), fz + 0.5), tolerance=0.8)
            return "RUNNING" if n_out.status in {"OK", "PROGRESSED"} else n_out.status

        # Already at target Y-band -> tunnel forward 1 block
        tunnel_cell = (fx + 1, fy, fz)
        d_out = self._dig.dig_block(bb, tunnel_cell, collect=True)
        return "RUNNING" if d_out.status in {"OK", "PROGRESSED"} else d_out.status
