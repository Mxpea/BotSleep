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
        eye = (bb.pos[0], bb.pos[1] + 1.62, bb.pos[2])
        if dist3(eye, center) > 4.4:
            nav_out = self._nav.step_to(bb, center, tolerance=2.0)
            if nav_out.status == "FAILED_LOCAL":
                # Blacklist unreachable target (e.g. sealed underground cave pocket) so next tick picks another!
                self.ctx.gateway.unreachable.record_failure(bb.dimension, target, "dig", nav_out.reason)
                self.ctx.gateway.unreachable.record_failure(bb.dimension, target, "dig", nav_out.reason)
                return nav_out
            # Only return early if navigation actually moved/acted; if REACHED (none) with eye still > 4.4,
            # let upstream dig either approach closer or return out_of_reach so target gets cleanly blacklisted!
            if nav_out.action != "none" or nav_out.status != "OK":
                return nav_out

        out = self.ctx.execute_write(
            bb,
            "dig",
            {"pos": list(target), "collect": collect},
            lambda: self.ctx.client.dig(list(target), collect=collect),
            unknown_center=target,
            record_unreachable_action="dig",
        )
        if out.status == "OK":
            self.ctx.gateway.write_through_dig(bb.dimension, target)
            return out
        return out

    def dig_nearest_of_types(
        self,
        bb: Blackboard,
        block_types: Sequence[str],
        *,
        radius: int = 24,
        collect: bool = True,
        max_dy: Optional[float] = None,
    ) -> ActionOutcome:
        """Scan exposed blocks of given types, skip blacklisted/deep-cave cells, and dig/approach nearest (<= 1 write)."""
        rows = self.ctx.gateway.scan_blocks(bb.dimension, list(block_types), radius=radius, limit=64)
        py = bb.pos[1]
        fx, fy, fz = int(bb.pos[0] // 1), int(bb.pos[1] // 1), int(bb.pos[2] // 1)
        under_feet = (fx, fy - 1, fz)
        candidates: List[Coord3] = []
        for row in rows:
            raw_p = row.get("pos")
            if isinstance(raw_p, list) and len(raw_p) == 3:
                c = (int(raw_p[0]), int(raw_p[1]), int(raw_p[2]))
                # Minecraft Rule #1: never dig directly under own feet into a 1x1 pit!
                if c == under_feet:
                    continue
                if max_dy is not None:
                    # Never dig blocks >= 2 below feet through a solid floor (drops fall into sealed caves!)
                    dy = c[1] - py
                    if dy < -1.0 or dy > max_dy:
                        continue
                if not self.ctx.gateway.unreachable.is_blocked(bb.dimension, c, "dig"):
                    candidates.append(c)
        if not candidates:
            return ActionOutcome("FAILED_LOCAL", "dig_collect", reason="no_exposed_block_found")
        # Strongly prefer same-elevation surface blocks over deep underground cave pockets!
        candidates.sort(key=lambda c: dist3(bb.pos, (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)) + abs(c[1] - py) * 2.5)
        return self.dig_block(bb, candidates[0], collect=collect)
