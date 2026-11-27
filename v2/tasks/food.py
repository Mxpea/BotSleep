"""L3b HuntFoodTask: lean daytime passive-animal hunting and meat pickup."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Set, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import FOOD_ITEMS, PASSIVE_ANIMALS, dist3
from v2.primitives.base import PrimitiveContext
from v2.primitives.combat import CombatPrimitive
from v2.primitives.navigate import NavigatePrimitive
from v2.tasks.base import Task


def _edible_food_count(counts: Dict[str, int]) -> int:
    return sum(int(counts.get(name, 0)) for name in FOOD_ITEMS if name != "rotten_flesh")


def _extract_food_drops(bb: Blackboard, ignored: Set[int], radius: float = 8.0) -> List[Tuple[float, int, List[float]]]:
    drops: List[Tuple[float, int, List[float]]] = []
    for ent in bb.entities:
        if not isinstance(ent, dict):
            continue
        is_item = str(ent.get("type") or "") == "item" or str(ent.get("name") or "") == "item"
        if not is_item:
            continue
        eid = int(ent.get("id", -1))
        if eid in ignored:
            continue
        item_field = ent.get("item")
        if isinstance(item_field, str) and item_field:
            item_name = item_field.split("x", 1)[0]
            if item_name not in FOOD_ITEMS:
                continue
        elif str(ent.get("name") or "") not in ("", "item") and str(ent.get("name")) not in FOOD_ITEMS:
            continue
        epos = ent.get("pos")
        if not isinstance(epos, (list, tuple)) or len(epos) != 3:
            continue
        coords = [float(epos[0]), float(epos[1]), float(epos[2])]
        d = dist3(bb.pos, coords)
        if d <= radius:
            drops.append((d, eid, coords))
    drops.sort(key=lambda row: row[0])
    return drops


class HuntFoodTask(Task):
    """Collects nearby meat drops or hunts overworld daytime passive animals."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        *,
        min_food_items: int = 4,
        max_steps: int = 16,
    ) -> None:
        super().__init__("hunt_food")
        self.ctx = ctx
        self.min_food_items = max(1, int(min_food_items))
        self.max_steps = max(1, int(max_steps))
        self._combat = CombatPrimitive(ctx)
        self._nav = NavigatePrimitive(ctx)
        self._steps: int = 0
        self._ignored_drops: Set[int] = set()

    def stop(self, bb: Blackboard) -> None:
        self._steps = 0
        self._ignored_drops.clear()
        super().stop(bb)

    def is_finished(self, bb: Blackboard) -> bool:
        return _edible_food_count(bb.inventory_counts) >= self.min_food_items

    def on_tick(self, bb: Blackboard) -> str:
        if _edible_food_count(bb.inventory_counts) >= self.min_food_items:
            return "COMPLETED"

        tod = int(bb.status.get("time_of_day", 1000)) % 24000
        if bb.dimension != "overworld" or (12541 <= tod <= 23458):
            bb.last_failure_reason = "hunt_food:not_overworld_daytime"
            return "FAILED_LOCAL"

        if self._steps >= self.max_steps:
            bb.last_failure_reason = "hunt_food:max_steps_exhausted"
            return "NEEDS_AI"
        self._steps += 1

        drops = _extract_food_drops(bb, self._ignored_drops, radius=8.0)
        if drops:
            d, eid, dpos = drops[0]
            out = self._nav.step_to(bb, dpos, tolerance=1.0)
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            if out.status == "FAILED_LOCAL" or (out.status == "OK" and bb.writes_this_tick == 0 and d <= 1.2):
                self._ignored_drops.add(eid)
            return "RUNNING"

        now = time.time()
        cd: Dict[int, float] = bb.get("failed_combat_entities") or {}
        animals = [
            e for e in bb.entities
            if str(e.get("name") or "") in PASSIVE_ANIMALS
            and isinstance(e.get("pos"), list)
            and len(e["pos"]) == 3
            and dist3(bb.pos, e["pos"]) <= 16.0
            and cd.get(int(e.get("id", -1)), 0.0) <= now
        ]
        if not animals:
            bb.last_failure_reason = "hunt_food:no_animals_or_drops"
            return "FAILED_LOCAL"

        animals.sort(key=lambda e: dist3(bb.pos, e["pos"]))
        target = animals[0]
        epos = [float(v) for v in target["pos"]]
        if dist3(bb.pos, epos) > 3.0:
            out = self._nav.step_to(bb, epos, tolerance=2.5)
            return "UNKNOWN" if out.status == "UNKNOWN" else "RUNNING"

        out = self._combat.melee_step(bb, int(target["id"]), epos, max_hits=4)
        return "UNKNOWN" if out.status == "UNKNOWN" else "RUNNING"
