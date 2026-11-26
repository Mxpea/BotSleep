"""L3a Primitive 4: CraftPrimitive (2x2 hand-craft + 3x3 with 16m crafting_table reuse)."""

from __future__ import annotations

from typing import Optional

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, dist3
from v2.primitives.base import ActionOutcome, PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive
from v2.primitives.place_build import PlaceBuildPrimitive


class CraftPrimitive:
    TABLE_REUSE_RADIUS = 16

    def __init__(self, ctx: PrimitiveContext) -> None:
        self.ctx = ctx
        self._nav = NavigatePrimitive(ctx)
        self._place = PlaceBuildPrimitive(ctx)

    def find_nearby_table(self, bb: Blackboard) -> Optional[Coord3]:
        rows = self.ctx.gateway.scan_blocks(bb.dimension, ["crafting_table"], radius=self.TABLE_REUSE_RADIUS, limit=8)
        if not rows:
            return None
        rows.sort(key=lambda r: float(r.get("dist", 999.0)))
        for row in rows:
            pos = row.get("pos")
            if isinstance(pos, list) and len(pos) == 3:
                coord = (int(pos[0]), int(pos[1]), int(pos[2]))
                if not self.ctx.gateway.unreachable.is_blocked(bb.dimension, coord, "craft_table"):
                    return coord
        return None

    def craft(self, bb: Blackboard, item: str, count: int = 1, *, needs_table: bool = False) -> ActionOutcome:
        """Execute ONE crafting step (or approach/place table if 3x3 required). Never duplicates tables within 16m."""
        table_pos: Optional[Coord3] = None
        if needs_table:
            table_pos = self.find_nearby_table(bb)
            if table_pos is not None:
                t_center = (table_pos[0] + 0.5, table_pos[1] + 0.5, table_pos[2] + 0.5)
                if dist3(bb.pos, t_center) > 4.2:
                    nav = self._nav.step_to(bb, t_center, tolerance=3.0)
                    if nav.status == "FAILED_LOCAL":
                        self.ctx.gateway.unreachable.record_failure(bb.dimension, table_pos, "craft_table", nav.reason)
                        self.ctx.gateway.unreachable.record_failure(bb.dimension, table_pos, "craft_table", nav.reason)
                    return nav
            else:
                if bb.inventory_counts.get("crafting_table", 0) > 0:
                    return self._place.place_near_player(bb, "crafting_table")
                bb.last_failure_reason = "craft:no_crafting_table"
                return ActionOutcome("FAILED_LOCAL", "craft", reason="no_crafting_table")

        out = self.ctx.execute_write(
            bb,
            "craft",
            {"item": item, "count": int(count)},
            lambda: self.ctx.client.craft(item, int(count)),
        )
        if out.status == "FAILED_LOCAL" and table_pos is not None and out.reason in {"need_crafting_table", "path_not_found"}:
            self.ctx.gateway.unreachable.record_failure(bb.dimension, table_pos, "craft_table", out.reason)
            self.ctx.gateway.invalidate_unknown_radius(bb.dimension, table_pos, radius=0)
        return out
