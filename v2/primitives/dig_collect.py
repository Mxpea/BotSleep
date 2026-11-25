"""L3a Primitive 2: DigCollectPrimitive (logs, ores, gravel->flint, occluders, vein completion)."""

from __future__ import annotations

from typing import List, Optional, Sequence

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, dist3
from v2.primitives.base import ActionOutcome, PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive


class DigCollectPrimitive:
    def __init__(self, ctx: PrimitiveContext) -> None:
        self.ctx = ctx
        self._nav = NavigatePrimitive(ctx)

    def dig_block(self, bb: Blackboard, pos: Coord3, *, collect: bool = True) -> ActionOutcome:
        """Approach if out of dig reach, otherwise dig ONE block and update voxel cache write-through."""
        target = (int(pos[0]), int(pos[1]), int(pos[2]))
        if self.ctx.gateway.unreachable.is_blocked(bb.dimension, target, "dig"):
            return ActionOutcome("FAILED_LOCAL", "dig", reason="blacklisted_unreachable", details={"pos": list(target)})

        center = (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5)
        if dist3(bb.pos, center) > 4.4:
            return self._nav.step_to(bb, center, tolerance=3.5)

        out = self.ctx.execute_write(
            bb,
            "dig",
            {"pos": list(target), "collect": collect},
            lambda: self.ctx.client.dig(list(target), collect=collect),
            unknown_center=target,
        )
        if out.status == "OK":
            self.ctx.gateway.write_through_dig(bb.dimension, target)
            return out
        if out.status == "FAILED_LOCAL":
            self.ctx.gateway.unreachable.record_failure(bb.dimension, target, "dig", out.reason)
        return out

    def dig_nearest_of_types(
        self,
        bb: Blackboard,
        block_types: Sequence[str],
        *,
        radius: int = 24,
        collect: bool = True,
    ) -> ActionOutcome:
        """Scan exposed blocks of given types, skip blacklisted cells, and dig/approach nearest (<= 1 write)."""
        rows = self.ctx.gateway.scan_blocks(bb.dimension, list(block_types), radius=radius, limit=32)
        candidates: List[Coord3] = []
        for row in rows:
            raw_p = row.get("pos")
            if isinstance(raw_p, list) and len(raw_p) == 3:
                c = (int(raw_p[0]), int(raw_p[1]), int(raw_p[2]))
                if not self.ctx.gateway.unreachable.is_blocked(bb.dimension, c, "dig"):
                    candidates.append(c)
        if not candidates:
            return ActionOutcome("FAILED_LOCAL", "dig_collect", reason="no_exposed_block_found")
        candidates.sort(key=lambda c: dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)))
        return self.dig_block(bb, candidates[0], collect=collect)
