"""L3a Primitive 1: NavigatePrimitive (multi-hop land, 3D micro-dig/pillar/bridge, surface swim)."""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3
from v2.primitives.base import ActionOutcome, PrimitiveContext


class NavigatePrimitive:
    def __init__(self, ctx: PrimitiveContext) -> None:
        self.ctx = ctx

    def step_to(
        self,
        bb: Blackboard,
        target: Sequence[float],
        *,
        tolerance: float = 2.0,
        avoid_block_breaking: Optional[Callable[[Coord3], bool]] = None,
        prefer_3d: bool = False,
        xz_only: bool = False,
        avoid_hard_rock_without_pick: Optional[bool] = None,
    ) -> ActionOutcome:
        protected = bb.get("protected_cells")
        if protected:
            base_avoid = avoid_block_breaking
            avoid_fn: Optional[Callable[[Coord3], bool]] = (
                (lambda c, _p=protected, _b=base_avoid: (c in _p) or _b(c))
                if base_avoid is not None
                else (lambda c, _p=protected: c in _p)
            )
        else:
            avoid_fn = avoid_block_breaking

        out = self.ctx.navigator.step_toward(
            target,
            bb.status,
            tolerance=tolerance,
            inventory_counts=bb.inventory_counts,
            avoid_block_breaking=avoid_fn,
            prefer_3d=prefer_3d,
            xz_only=xz_only,
            threat_positions=bb.get("active_threat_positions"),
            nearby_entities=bb.entities,
            avoid_hard_rock_without_pick=avoid_hard_rock_without_pick,
        )
        if out.action_taken != "none":
            bb.writes_this_tick += 1
        bb.set("fast_tick_once", out.reason == "pillar_jump_launch", writer="navigate")
        bb.last_action_outcome = f"nav:{out.status.lower()}:{out.action_taken}"
        if out.status == "REACHED":
            return ActionOutcome("OK", "navigate", reason="reached", details=out.details, verified=True)
        if out.status == "PROGRESSED":
            return ActionOutcome("PROGRESSED", "navigate", reason=out.reason, details=out.details)
        if out.status == "UNKNOWN":
            return ActionOutcome("UNKNOWN", "navigate", reason=out.reason, details=out.details)
        bb.last_failure_reason = f"navigate:{out.reason}"
        return ActionOutcome("FAILED_LOCAL", "navigate", reason=out.reason, details=out.details)
