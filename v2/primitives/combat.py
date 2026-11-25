"""L3a Primitive 7: CombatPrimitive (short-step melee, shield guard, bow/crossbow, passive hunting)."""

from __future__ import annotations

from typing import Optional

from v2.bt.blackboard import Blackboard
from v2.domain import PASSIVE_ANIMALS, dist3
from v2.primitives.base import ActionOutcome, PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive


class CombatPrimitive:
    def __init__(self, ctx: PrimitiveContext) -> None:
        self.ctx = ctx
        self._nav = NavigatePrimitive(ctx)

    def melee_step(self, bb: Blackboard, entity_id: int, entity_pos: list[float], *, max_hits: int = 3) -> ActionOutcome:
        if dist3(bb.pos, entity_pos) > 3.8:
            return self._nav.step_to(bb, entity_pos, tolerance=2.8)
        return self.ctx.execute_write(
            bb,
            "attack",
            {"entity_id": int(entity_id), "max_hits": int(max_hits)},
            lambda: self.ctx.client.attack(int(entity_id), timeout_ms=4000, max_hits=int(max_hits)),
        )

    def shoot_step(self, bb: Blackboard, entity_id: int, *, weapon: str = "bow") -> ActionOutcome:
        if bb.inventory_counts.get(weapon, 0) < 1:
            return ActionOutcome("FAILED_LOCAL", "shoot", reason=f"missing_{weapon}")
        return self.ctx.execute_write(
            bb,
            "shoot",
            {"entity_id": int(entity_id), "weapon": weapon},
            lambda: self.ctx.client.shoot(int(entity_id), weapon=weapon),
        )

    def shield_posture(self, bb: Blackboard, *, active: bool) -> ActionOutcome:
        return self.ctx.execute_write(
            bb,
            "posture",
            {"shield": bool(active), "sneak": False, "jump": False},
            lambda: self.ctx.client.posture(sneak=False, shield=bool(active), jump=False),
        )

    def hunt_nearest_animal(self, bb: Blackboard, *, radius: float = 20.0) -> ActionOutcome:
        animals = [
            e for e in bb.entities
            if str(e.get("name") or "") in PASSIVE_ANIMALS and isinstance(e.get("pos"), list) and dist3(bb.pos, e["pos"]) <= radius
        ]
        if not animals:
            return ActionOutcome("FAILED_LOCAL", "hunt", reason="no_passive_animal_nearby")
        animals.sort(key=lambda e: dist3(bb.pos, e["pos"]))
        target = animals[0]
        return self.melee_step(bb, int(target["id"]), target["pos"], max_hits=4)
