"""L3a Primitive 7: CombatPrimitive (short-step melee, shield guard, bow/crossbow, passive hunting)."""

from __future__ import annotations

import time
from typing import Dict, Optional

from v2.bt.blackboard import Blackboard
from v2.domain import PASSIVE_ANIMALS, dist3
from v2.primitives.base import ActionOutcome, PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive


class CombatPrimitive:
    def __init__(self, ctx: PrimitiveContext) -> None:
        self.ctx = ctx
        self._nav = NavigatePrimitive(ctx)

    @staticmethod
    def _record_entity_cooldown(bb: Blackboard, entity_id: int, ttl: float = 20.0) -> None:
        now = time.time()
        cd: Dict[int, float] = {k: v for k, v in (bb.get("failed_combat_entities") or {}).items() if v > now}
        cd[int(entity_id)] = now + ttl
        bb.set("failed_combat_entities", cd, writer="CombatPrimitive")

    def melee_step(self, bb: Blackboard, entity_id: int, entity_pos: list[float], *, max_hits: int = 3) -> ActionOutcome:
        if dist3(bb.pos, entity_pos) > 3.8:
            nav = self._nav.step_to(bb, entity_pos, tolerance=2.8)
            if nav.status == "FAILED_LOCAL":
                self._record_entity_cooldown(bb, entity_id)
            return nav
        out = self.ctx.execute_write(
            bb,
            "attack",
            {"entity_id": int(entity_id), "max_hits": int(max_hits)},
            lambda: self.ctx.client.attack(int(entity_id), timeout_ms=4000, max_hits=int(max_hits)),
        )
        if out.status == "FAILED_LOCAL":
            self._record_entity_cooldown(bb, entity_id)
        return out

    def shoot_step(
        self,
        bb: Blackboard,
        entity_id: Optional[int] = None,
        *,
        pos: Optional[list[float]] = None,
        weapon: str = "bow",
    ) -> ActionOutcome:
        if bb.inventory_counts.get(weapon, 0) < 1:
            bb.last_failure_reason = f"shoot:missing_{weapon}"
            return ActionOutcome("FAILED_LOCAL", "shoot", reason=f"missing_{weapon}")
        payload = {"entity_id": int(entity_id), "weapon": weapon} if entity_id is not None else {"pos": list(pos or []), "weapon": weapon}
        out = self.ctx.execute_write(
            bb,
            "shoot",
            payload,
            lambda: self.ctx.client.shoot(entity_id, pos=pos, weapon=weapon),
        )
        if out.status == "FAILED_LOCAL" and entity_id is not None:
            self._record_entity_cooldown(bb, entity_id)
        return out

    def shield_posture(self, bb: Blackboard, *, active: bool) -> ActionOutcome:
        return self.ctx.execute_write(
            bb,
            "posture",
            {"shield": bool(active), "sneak": False, "jump": False},
            lambda: self.ctx.client.posture(sneak=False, shield=bool(active), jump=False),
        )

    def hunt_nearest_animal(self, bb: Blackboard, *, radius: float = 20.0) -> ActionOutcome:
        now = time.time()
        cd: Dict[int, float] = bb.get("failed_combat_entities") or {}
        animals = [
            e for e in bb.entities
            if str(e.get("name") or "") in PASSIVE_ANIMALS
            and isinstance(e.get("pos"), list)
            and dist3(bb.pos, e["pos"]) <= radius
            and cd.get(int(e.get("id", -1)), 0.0) <= now
        ]
        if not animals:
            bb.last_failure_reason = "hunt:no_passive_animal_nearby"
            return ActionOutcome("FAILED_LOCAL", "hunt", reason="no_passive_animal_nearby")
        animals.sort(key=lambda e: dist3(bb.pos, e["pos"]))
        target = animals[0]
        return self.melee_step(bb, int(target["id"]), target["pos"], max_hits=4)
