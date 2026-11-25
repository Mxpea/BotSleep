"""L3a Primitive 3: PlaceBuildPrimitive (workstations, beds, doors, single-block structure completion)."""

from __future__ import annotations

from typing import Optional

from v2.bt.blackboard import Blackboard
from v2.domain import AIR_BLOCKS, Coord3, NON_SOLID_BLOCKS, block_coord, dist3
from v2.primitives.base import ActionOutcome, PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive


class PlaceBuildPrimitive:
    def __init__(self, ctx: PrimitiveContext) -> None:
        self.ctx = ctx
        self._nav = NavigatePrimitive(ctx)

    def place_at(self, bb: Blackboard, item: str, target: Coord3) -> ActionOutcome:
        """Approach if needed, otherwise place ONE block and update voxel cache write-through."""
        if bb.inventory_counts.get(item, 0) < 1:
            return ActionOutcome("FAILED_LOCAL", "place", reason=f"missing_{item}")
        t = (int(target[0]), int(target[1]), int(target[2]))
        center = (t[0] + 0.5, t[1] + 0.5, t[2] + 0.5)
        if dist3(bb.pos, center) > 4.5:
            return self._nav.step_to(bb, center, tolerance=3.2)

        out = self.ctx.execute_write(
            bb,
            "place",
            {"item": item, "pos": list(t)},
            lambda: self.ctx.client.place_block(item, t[0], t[1], t[2]),
            unknown_center=t,
        )
        if out.status == "OK":
            self.ctx.gateway.write_through_place(bb.dimension, t, item)
            if item in {"crafting_table", "furnace", "chest"} and self.ctx.gateway.db is not None:
                self.ctx.gateway.db.save_landmark(bb.dimension, item, f"{item}_{t[0]}_{t[1]}_{t[2]}", t)
        return out

    def place_near_player(self, bb: Blackboard, item: str) -> ActionOutcome:
        """Find a safe adjacent empty cell over solid ground within 2-3 blocks (at fy, fy+1, or fy-1) and place item."""
        if bb.inventory_counts.get(item, 0) < 1:
            return ActionOutcome("FAILED_LOCAL", "place", reason=f"missing_{item}")
        fx, fy, fz = block_coord(bb.pos)
        offsets = [(2, 0), (-2, 0), (0, 2), (0, -2), (1, 1), (-1, 1), (1, -1), (-1, -1), (1, 0), (-1, 0), (0, 1), (0, -1)]
        dy_order = (0, 1, -1)
        coords = []
        for oy in dy_order:
            for ox, oz in offsets:
                coords.append((fx + ox, fy + oy, fz + oz))
                coords.append((fx + ox, fy + oy - 1, fz + oz))
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, coords)
        for oy in dy_order:
            for ox, oz in offsets:
                cell = (fx + ox, fy + oy, fz + oz)
                below = (fx + ox, fy + oy - 1, fz + oz)
                c_ent = fetched.get(cell)
                b_ent = fetched.get(below)
                if c_ent is not None and c_ent.name in AIR_BLOCKS and b_ent is not None and b_ent.name not in NON_SOLID_BLOCKS:
                    return self.place_at(bb, item, cell)
        return ActionOutcome("FAILED_LOCAL", "place", reason="no_adjacent_clear_spot")
