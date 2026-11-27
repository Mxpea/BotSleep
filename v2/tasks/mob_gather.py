"""L3b GatherMobDropTask: acquire mob/spider/squid drops via shear, cobweb dig, or safe combat."""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import HOSTILE_MOBS, dist3
from v2.primitives.base import PrimitiveContext
from v2.primitives.combat import CombatPrimitive
from v2.primitives.dig_collect import DigCollectPrimitive
from v2.primitives.navigate import NavigatePrimitive
from v2.tasks.base import Task
from v2.tasks.surface import ReturnToSurfaceTask, is_underground


class GatherMobDropTask(Task):
    """Acquire animal/mob drops (wool, string, bone, feather, leather, glow_ink_sac) with bounded retries."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        item: str,
        count: int,
        mob_names: Sequence[str],
        *,
        max_steps: int = 28,
    ) -> None:
        super().__init__(f"gather_mob:{item}x{count}")
        self.ctx = ctx
        self.item = str(item)
        self.count = max(1, int(count))
        self.mob_names = tuple(mob_names)
        self.max_steps = max(6, int(max_steps))
        self._combat = CombatPrimitive(ctx)
        self._dig = DigCollectPrimitive(ctx)
        self._nav = NavigatePrimitive(ctx)
        self._steps: int = 0
        self._explore_steps: int = 0
        self._wander_heading: int = 0

    def stop(self, bb: Blackboard) -> None:
        self._steps = 0
        self._explore_steps = 0
        super().stop(bb)

    def is_finished(self, bb: Blackboard) -> bool:
        return bb.inventory_counts.get(self.item, 0) >= self.count

    def _find_target_mob(self, bb: Blackboard) -> Optional[dict]:
        cands = [
            e for e in bb.entities
            if isinstance(e, dict)
            and str(e.get("name") or "") in self.mob_names
            and isinstance(e.get("pos"), list)
            and len(e["pos"]) == 3
        ]
        if not cands:
            return None
        cands.sort(key=lambda e: dist3(bb.pos, e["pos"]))
        return cands[0]

    def _find_recon_pos(self, bb: Blackboard) -> Optional[Tuple[float, float, float]]:
        recon = bb.get("island_recon")
        if not isinstance(recon, dict):
            return None
        for m in self.mob_names:
            rows = recon.get(m)
            if isinstance(rows, list) and rows:
                pos = rows[-1].get("pos") if isinstance(rows[-1], dict) else None
                if isinstance(pos, (list, tuple)) and len(pos) == 3:
                    return (float(pos[0]), float(pos[1]), float(pos[2]))
        return None

    def on_tick(self, bb: Blackboard) -> str:
        if bb.inventory_counts.get(self.item, 0) >= self.count:
            return "COMPLETED"
        self._steps += 1
        if self._steps > self.max_steps:
            bb.last_failure_reason = f"gather_mob_timeout:{self.item}"
            return "NEEDS_AI"

        # 1) Pick up any dropped item right nearby first
        drop_out = self._dig.pickup_pending_drop(bb)
        if drop_out is not None and drop_out.status in {"OK", "PROGRESSED", "UNKNOWN"}:
            return "UNKNOWN" if drop_out.status == "UNKNOWN" else "RUNNING"

        # 2) Safe cobweb harvest shortcut for string
        if self.item == "string":
            web_out = self._dig.dig_nearest_of_types(bb, ("cobweb",), radius=16, collect=True)
            if web_out.status in {"OK", "PROGRESSED", "UNKNOWN"}:
                return "UNKNOWN" if web_out.status == "UNKNOWN" else "RUNNING"

        # 3) If hunting surface passive mobs while underground, return to surface first
        need_surface_passive = all(m not in HOSTILE_MOBS and m != "glow_squid" for m in self.mob_names)
        if need_surface_passive and is_underground(bb, self.ctx.gateway):
            return self.run_child(bb, ReturnToSurfaceTask(self.ctx))

        ent = self._find_target_mob(bb)
        if ent is None:
            recon_pos = self._find_recon_pos(bb)
            if recon_pos is not None and dist3(bb.pos, recon_pos) > 8.0:
                nav = self._nav.step_to(bb, recon_pos, tolerance=6.0)
                if nav.status == "UNKNOWN":
                    return "UNKNOWN"
                if nav.status in {"OK", "PROGRESSED"}:
                    return "RUNNING"

            self._explore_steps += 1
            if self._explore_steps > 8:
                bb.last_failure_reason = f"mob_not_found:{self.item}"
                return "NEEDS_AI"
            dirs = ((18.0, 0.0), (0.0, 18.0), (-18.0, 0.0), (0.0, -18.0))
            dx, dz = dirs[self._wander_heading % 4]
            self._wander_heading += 1
            tgt = (bb.pos[0] + dx, bb.pos[1], bb.pos[2] + dz)
            nav = self._nav.step_to(bb, tgt, tolerance=4.0, xz_only=True)
            if nav.status == "UNKNOWN":
                return "UNKNOWN"
            return "RUNNING"

        eid = int(ent.get("id", -1))
        epos = [float(v) for v in ent["pos"]]
        ename = str(ent.get("name") or "")

        # 4) Safety abort before picking a fight with a hostile while low HP
        if ename in HOSTILE_MOBS and bb.health < 10.0:
            bb.last_failure_reason = f"low_hp_for_mob:{ename}"
            return "NEEDS_AI"

        # 5) Non-lethal shears shortcut for sheep wool
        if self.item == "white_wool" and ename == "sheep" and bb.inventory_counts.get("shears", 0) > 0:
            if dist3(bb.pos, epos) > 3.2:
                nav = self._nav.step_to(bb, epos, tolerance=2.5)
                if nav.status == "UNKNOWN":
                    return "UNKNOWN"
                return "RUNNING"
            out = self.ctx.execute_write(
                bb,
                "interact",
                {"entity_id": eid, "item": "shears"},
                lambda _id=eid: self.ctx.client.interact(entity_id=_id, item="shears"),
            )
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            return "RUNNING"

        # 6) Approach & melee strike
        out = self._combat.melee_step(bb, eid, epos, max_hits=3)
        if out.status == "UNKNOWN":
            return "UNKNOWN"
        if out.status == "FAILED_LOCAL":
            self._explore_steps += 2
        return "RUNNING"
