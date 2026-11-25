"""L3a Primitive 6: FluidPrimitive (exact y+0.85 pickup, y-0.01 pour, occluder clearing, ignition)."""

from __future__ import annotations

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, block_coord, dist3
from v2.primitives.base import ActionOutcome, PrimitiveContext
from v2.primitives.dig_collect import DigCollectPrimitive
from v2.primitives.navigate import NavigatePrimitive


class FluidPrimitive:
    def __init__(self, ctx: PrimitiveContext) -> None:
        self.ctx = ctx
        self._nav = NavigatePrimitive(ctx)
        self._dig = DigCollectPrimitive(ctx)

    def pickup_liquid(self, bb: Blackboard, source_pos: Coord3) -> ActionOutcome:
        """Pick up a water/lava source block using exact upstream y+0.85 LOS planning (<= 1 write)."""
        if bb.inventory_counts.get("bucket", 0) < 1:
            return ActionOutcome("FAILED_LOCAL", "fluid_pickup", reason="missing_bucket")
        target = (int(source_pos[0]), int(source_pos[1]), int(source_pos[2]))
        if self.ctx.gateway.unreachable.is_blocked(bb.dimension, target, "fluid_pickup"):
            return ActionOutcome("FAILED_LOCAL", "fluid_pickup", reason="blacklisted_unreachable")

        # Approach general neighborhood first if far (> 5m)
        if dist3(bb.pos, (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5)) > 5.0:
            return self._nav.step_to(bb, (target[0] + 0.5, target[1] + 1.0, target[2] + 0.5), tolerance=3.0)

        plan = self.ctx.gateway.plan_liquid_interaction(bb.dimension, target, bb.pos, mode="pickup")
        if plan.status == "CLEAR_OCCLUDER" and plan.occluder_pos is not None:
            out = self._dig.dig_block(bb, plan.occluder_pos, collect=False)
            return ActionOutcome("PROGRESSED" if out.status == "OK" else out.status, "fluid_clear_occluder", reason=plan.reason, details={"occluder": list(plan.occluder_pos)})

        if plan.status != "READY" or plan.stand_pos is None:
            self.ctx.gateway.unreachable.record_failure(bb.dimension, target, "fluid_pickup", plan.reason)
            return ActionOutcome("FAILED_LOCAL", "fluid_pickup", reason=plan.reason)

        cur_cell = block_coord(bb.pos)
        if cur_cell != plan.stand_pos:
            return self._nav.step_to(
                bb,
                (plan.stand_pos[0] + 0.5, float(plan.stand_pos[1]), plan.stand_pos[2] + 0.5),
                tolerance=0.35,
            )

        out = self.ctx.execute_write(
            bb,
            "interact",
            {"pos": list(target), "item": "bucket"},
            lambda: self.ctx.client.interact(pos=list(target), item="bucket"),
            unknown_center=target,
            record_unreachable_action="fluid_pickup",
        )
        if out.status == "OK":
            # Upstream useBucket returns ok=true even on silent miss (`changed: false`, empty `gained`)!
            gained = out.details.get("gained") if isinstance(out.details.get("gained"), dict) else {}
            filled = any(str(k).endswith("_bucket") and int(v) > 0 for k, v in gained.items()) or bool(out.details.get("filled"))
            if not filled and out.details.get("changed") is False:
                self.ctx.gateway.unreachable.record_failure(bb.dimension, target, "fluid_pickup", "bucket_no_change")
                self.ctx.gateway.invalidate_unknown_radius(bb.dimension, target, radius=0)
                bb.last_failure_reason = "fluid_pickup:bucket_no_change"
                return ActionOutcome("FAILED_LOCAL", "fluid_pickup", reason="bucket_no_change", details=out.details)
            # Invalidate rather than hard-setting air because 2x2 infinite water sources immediately reform!
            self.ctx.gateway.invalidate_unknown_radius(bb.dimension, target, radius=0)
        return out

    def pour_liquid(self, bb: Blackboard, target_pos: Coord3, *, bucket_item: str) -> ActionOutcome:
        """Pour water_bucket or lava_bucket at target air cell using exact y-0.01 aim planning."""
        if bb.inventory_counts.get(bucket_item, 0) < 1:
            return ActionOutcome("FAILED_LOCAL", "fluid_pour", reason=f"missing_{bucket_item}")
        target = (int(target_pos[0]), int(target_pos[1]), int(target_pos[2]))
        eye = (bb.pos[0], bb.pos[1] + 1.62, bb.pos[2])
        aim = (target[0] + 0.5, target[1] - 0.01, target[2] + 0.5)
        if dist3(eye, aim) > 4.45:
            return self._nav.step_to(bb, (target[0] + 0.5, float(target[1]), target[2] + 1.5), tolerance=2.5)

        plan = self.ctx.gateway.plan_liquid_interaction(bb.dimension, target, bb.pos, mode="pour")
        if plan.status == "CLEAR_OCCLUDER" and plan.occluder_pos is not None:
            out = self._dig.dig_block(bb, plan.occluder_pos, collect=False)
            return ActionOutcome("PROGRESSED" if out.status == "OK" else out.status, "fluid_clear_occluder", reason=plan.reason)
        if plan.status != "READY" or plan.stand_pos is None:
            self.ctx.gateway.unreachable.record_failure(bb.dimension, target, "fluid_pour", plan.reason)
            return ActionOutcome("FAILED_LOCAL", "fluid_pour", reason=plan.reason)
        if block_coord(bb.pos) != plan.stand_pos:
            return self._nav.step_to(
                bb,
                (plan.stand_pos[0] + 0.5, float(plan.stand_pos[1]), plan.stand_pos[2] + 0.5),
                tolerance=0.35,
            )

        out = self.ctx.execute_write(
            bb,
            "interact",
            {"pos": list(target), "item": bucket_item},
            lambda: self.ctx.client.interact(pos=list(target), item=bucket_item),
            unknown_center=target,
            record_unreachable_action="fluid_pour",
        )
        if out.status == "OK":
            used = out.details.get("used") if isinstance(out.details.get("used"), dict) else {}
            consumed = int(used.get(bucket_item, 0)) > 0 or bool(out.details.get("poured"))
            if not consumed and out.details.get("changed") is False:
                self.ctx.gateway.unreachable.record_failure(bb.dimension, target, "fluid_pour", "pour_no_change")
                self.ctx.gateway.invalidate_unknown_radius(bb.dimension, target, radius=1)
                bb.last_failure_reason = "fluid_pour:pour_no_change"
                return ActionOutcome("FAILED_LOCAL", "fluid_pour", reason="pour_no_change", details=out.details)
            # Invalidate 3x3x3 neighborhood because poured fluid flows and may cast obsidian on neighbors
            self.ctx.gateway.invalidate_unknown_radius(bb.dimension, target, radius=1)
        return out

    def ignite(self, bb: Blackboard, target_pos: Coord3) -> ActionOutcome:
        """Ignite portal frame or block with flint_and_steel."""
        if bb.inventory_counts.get("flint_and_steel", 0) < 1:
            return ActionOutcome("FAILED_LOCAL", "ignite", reason="missing_flint_and_steel")
        target = (int(target_pos[0]), int(target_pos[1]), int(target_pos[2]))
        if dist3(bb.pos, (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5)) > 4.2:
            return self._nav.step_to(bb, (target[0] + 0.5, float(target[1]) + 1.0, target[2] + 0.5), tolerance=2.5)
        return self.ctx.execute_write(
            bb,
            "interact",
            {"pos": list(target), "item": "flint_and_steel"},
            lambda: self.ctx.client.interact(pos=list(target), item="flint_and_steel"),
            unknown_center=target,
        )
