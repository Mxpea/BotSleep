"""L3a Primitive 5: ContainerPrimitive (multi-furnace parallel batch smelt + chest deposit/withdraw)."""

from __future__ import annotations

from typing import Dict, List, Optional

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, dist3
from v2.primitives.base import ActionOutcome, PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive


class ContainerPrimitive:
    def __init__(self, ctx: PrimitiveContext) -> None:
        self.ctx = ctx
        self._nav = NavigatePrimitive(ctx)

    def smelt_batch_step(
        self,
        bb: Blackboard,
        furnace_pos: Coord3,
        *,
        input_item: str,
        input_count: int,
        fuel_item: Optional[str] = None,
        fuel_count: int = 1,
        harvest_output: bool = True,
    ) -> ActionOutcome:
        """Load input + fuel and/or harvest smelted output in ONE bounded window_act call."""
        center = (furnace_pos[0] + 0.5, furnace_pos[1] + 0.5, furnace_pos[2] + 0.5)
        if dist3(bb.pos, center) > 4.5:
            return self._nav.step_to(bb, center, tolerance=3.2)

        ops: List[Dict[str, object]] = []
        if input_count > 0 and bb.inventory_counts.get(input_item, 0) > 0:
            ops.append({"op": "put", "slot": "input", "item": input_item, "count": min(input_count, bb.inventory_counts[input_item])})
        if fuel_item and fuel_count > 0 and bb.inventory_counts.get(fuel_item, 0) > 0:
            ops.append({"op": "put", "slot": "fuel", "item": fuel_item, "count": min(fuel_count, bb.inventory_counts[fuel_item])})
        if harvest_output:
            ops.append({"op": "take", "slot": "output", "count": 64})
        if not ops:
            return ActionOutcome("FAILED_LOCAL", "window_act", reason="nothing_to_smelt_or_harvest")

        return self.ctx.execute_write(
            bb,
            "window/act",
            {"pos": list(furnace_pos), "ops": ops},
            lambda: self.ctx.client.window_act(list(furnace_pos), ops),
            unknown_center=furnace_pos,
        )

    def chest_transfer(
        self,
        bb: Blackboard,
        chest_pos: Coord3,
        *,
        op: str,  # "deposit" | "withdraw"
        item: str,
        count: int,
    ) -> ActionOutcome:
        center = (chest_pos[0] + 0.5, chest_pos[1] + 0.5, chest_pos[2] + 0.5)
        if dist3(bb.pos, center) > 4.5:
            return self._nav.step_to(bb, center, tolerance=3.2)
        ops = [{"op": op, "item": item, "count": int(count)}]
        return self.ctx.execute_write(
            bb,
            "window/act",
            {"pos": list(chest_pos), "ops": ops},
            lambda: self.ctx.client.window_act(list(chest_pos), ops),
            unknown_center=chest_pos,
        )
