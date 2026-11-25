"""L3a Primitive 5: ContainerPrimitive (upstream mc-agent-api window/view & window/act for furnaces and chests).

Exact upstream `mc-agent-api/src/session.js` schema:
- Furnace sections: `input`, `fuel`, `output`; info: `{burning, progress, fuel_left}`.
- Chest/barrel/shulker sections: `container`.
- Ops: `{"op": "put", "to": <section>, "item": ..., "count": ...}` and
       `{"op": "take", "from": <section>, "item": ..., "count": ...}`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, dist3
from v2.primitives.base import ActionOutcome, PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive


class ContainerPrimitive:
    def __init__(self, ctx: PrimitiveContext) -> None:
        self.ctx = ctx
        self._nav = NavigatePrimitive(ctx)

    def inspect_window(self, bb: Blackboard, pos: Coord3) -> ActionOutcome:
        """Open and inspect a furnace/chest via `window/view` (<= 1 write-locked action)."""
        center = (pos[0] + 0.5, pos[1] + 0.5, pos[2] + 0.5)
        if dist3(bb.pos, center) > 4.5:
            return self._nav.step_to(bb, center, tolerance=3.2)
        return self.ctx.execute_write(
            bb,
            "window/view",
            {"pos": list(pos)},
            lambda: self.ctx.client.window_view(list(pos)),
            unknown_center=pos,
        )

    def load_furnace(
        self,
        bb: Blackboard,
        furnace_pos: Coord3,
        *,
        input_item: Optional[str] = None,
        input_count: int = 0,
        fuel_item: Optional[str] = None,
        fuel_count: int = 0,
    ) -> ActionOutcome:
        """Put raw input and/or fuel into furnace using exact upstream `{op: put, to: input|fuel}` schema."""
        center = (furnace_pos[0] + 0.5, furnace_pos[1] + 0.5, furnace_pos[2] + 0.5)
        if dist3(bb.pos, center) > 4.5:
            return self._nav.step_to(bb, center, tolerance=3.2)

        ops: List[Dict[str, Any]] = []
        if input_item and input_count > 0 and bb.inventory_counts.get(input_item, 0) > 0:
            ops.append({
                "op": "put",
                "to": "input",
                "item": input_item,
                "count": min(int(input_count), bb.inventory_counts[input_item]),
            })
        if fuel_item and fuel_count > 0 and bb.inventory_counts.get(fuel_item, 0) > 0:
            ops.append({
                "op": "put",
                "to": "fuel",
                "item": fuel_item,
                "count": min(int(fuel_count), bb.inventory_counts[fuel_item]),
            })
        if not ops:
            return ActionOutcome("FAILED_LOCAL", "window/act", reason="nothing_to_load")

        return self.ctx.execute_write(
            bb,
            "window/act",
            {"pos": list(furnace_pos), "ops": ops},
            lambda: self.ctx.client.window_act(list(furnace_pos), ops),
            unknown_center=furnace_pos,
        )

    def harvest_furnace(self, bb: Blackboard, furnace_pos: Coord3, *, count: int = 64) -> ActionOutcome:
        """Take smelted output from furnace using exact upstream `{op: take, from: output}` schema."""
        center = (furnace_pos[0] + 0.5, furnace_pos[1] + 0.5, furnace_pos[2] + 0.5)
        if dist3(bb.pos, center) > 4.5:
            return self._nav.step_to(bb, center, tolerance=3.2)

        ops: List[Dict[str, Any]] = [{"op": "take", "from": "output", "count": int(count)}]
        return self.ctx.execute_write(
            bb,
            "window/act",
            {"pos": list(furnace_pos), "ops": ops},
            lambda: self.ctx.client.window_act(list(furnace_pos), ops),
            unknown_center=furnace_pos,
        )

    def smelt_batch_step(
        self,
        bb: Blackboard,
        furnace_pos: Coord3,
        *,
        input_item: str,
        input_count: int,
        fuel_item: Optional[str] = None,
        fuel_count: int = 1,
        harvest_output: bool = False,
    ) -> ActionOutcome:
        """Load input + fuel (or harvest output when requested) using exact upstream schema."""
        if harvest_output and input_count <= 0 and not fuel_item:
            return self.harvest_furnace(bb, furnace_pos)
        return self.load_furnace(
            bb,
            furnace_pos,
            input_item=input_item,
            input_count=input_count,
            fuel_item=fuel_item,
            fuel_count=fuel_count,
        )

    def chest_transfer(
        self,
        bb: Blackboard,
        chest_pos: Coord3,
        *,
        op: str,  # "deposit" | "withdraw" | "put" | "take"
        item: str,
        count: int,
    ) -> ActionOutcome:
        """Deposit or withdraw items from a chest using exact upstream `container` section ops."""
        center = (chest_pos[0] + 0.5, chest_pos[1] + 0.5, chest_pos[2] + 0.5)
        if dist3(bb.pos, center) > 4.5:
            return self._nav.step_to(bb, center, tolerance=3.2)
        if op in {"deposit", "put"}:
            ops: List[Dict[str, Any]] = [{"op": "put", "to": "container", "item": item, "count": int(count)}]
        elif op in {"withdraw", "take"}:
            ops = [{"op": "take", "from": "container", "item": item, "count": int(count)}]
        else:
            return ActionOutcome("FAILED_LOCAL", "window/act", reason=f"unsupported_chest_op:{op}")
        return self.ctx.execute_write(
            bb,
            "window/act",
            {"pos": list(chest_pos), "ops": ops},
            lambda: self.ctx.client.window_act(list(chest_pos), ops),
            unknown_center=chest_pos,
        )
