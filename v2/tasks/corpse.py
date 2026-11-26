"""L3b RecoverDeathDropTask: chunk-load-aware persistent corpse recovery."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, dist2_xz, dist3
from v2.primitives.base import PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive
from v2.tasks.acquire import AcquireItemTask
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue
from v2.tasks.surface import load_anchor_landmark

_DECENT_PICKAXES = ("netherite_pickaxe", "diamond_pickaxe", "iron_pickaxe", "stone_pickaxe")


def _has_melee_weapon(counts: Dict[str, int]) -> bool:
    return any(cnt > 0 and name.endswith(("_sword", "_axe")) for name, cnt in counts.items())


def _has_decent_pickaxe(counts: Dict[str, int]) -> bool:
    return any(counts.get(p, 0) > 0 for p in _DECENT_PICKAXES)


def _nearby_drop_items(entities: List[Dict[str, Any]], drop_pos: List[float], radius: float = 8.0) -> List[Tuple[float, List[float]]]:
    items: List[Tuple[float, List[float]]] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        if str(ent.get("type") or "") != "item" and str(ent.get("name") or "") != "item":
            continue
        epos = ent.get("pos")
        if not isinstance(epos, (list, tuple)) or len(epos) != 3:
            continue
        coords = [float(epos[0]), float(epos[1]), float(epos[2])]
        if dist3(coords, drop_pos) <= radius:
            items.append((dist3((0.0, 0.0, 0.0), coords), coords))
    return items


class RecoverDeathDropTask(Task):
    """Recovers high-value death drops using chunk-load-aware urgency vs unloaded safe prep."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        catalogue: TaskCatalogue,
        *,
        loaded_radius: float = 160.0,
        despawn_seconds: float = 300.0,
    ) -> None:
        super().__init__("recover_death_drop")
        self.ctx = ctx
        self.catalogue = catalogue
        self.loaded_radius = float(loaded_radius)
        self.despawn_seconds = float(despawn_seconds)
        self._nav = NavigatePrimitive(ctx)
        self._prep_done: bool = False
        self._via_anchor_done: bool = False
        self._nav_stalls: int = 0
        self._pickup_stalls: int = 0
        self._last_pos: Optional[Tuple[float, float, float]] = None

    def stop(self, bb: Blackboard) -> None:
        self._prep_done = False
        self._via_anchor_done = False
        self._nav_stalls = 0
        self._pickup_stalls = 0
        self._last_pos = None
        super().stop(bb)

    def is_finished(self, bb: Blackboard) -> bool:
        return not isinstance(bb.get("active_corpse_drop"), dict)

    def _clear_drop(self, bb: Blackboard, outcome: str) -> str:
        bb.set("active_corpse_drop", None, writer="corpse")
        bb.last_action_outcome = outcome
        self._clear_child(bb)
        return "COMPLETED"

    def on_tick(self, bb: Blackboard) -> str:
        drop = bb.get("active_corpse_drop")
        if not isinstance(drop, dict):
            return "COMPLETED"

        if float(drop.get("loaded_elapsed", 0.0)) >= self.despawn_seconds:
            return self._clear_drop(bb, "corpse_despawned")

        raw_pos = drop.get("pos")
        if not isinstance(raw_pos, (list, tuple)) or len(raw_pos) != 3:
            return self._clear_drop(bb, "corpse_invalid_pos")
        drop_pos = [float(raw_pos[0]), float(raw_pos[1]), float(raw_pos[2])]
        same_dim = bb.dimension == str(drop.get("dim") or "overworld")
        in_loaded = same_dim and dist2_xz(bb.pos, drop_pos) <= self.loaded_radius

        # Mode A: abort any unloaded prep immediately upon entering loaded radius!
        if in_loaded and self._active_child is not None:
            self._clear_child(bb)
            self._prep_done = True

        # Arrival & item pickup verification
        if same_dim and dist3(bb.pos, drop_pos) <= 6.0:
            items = _nearby_drop_items(bb.entities, drop_pos, radius=8.0)
            if not items:
                return self._clear_drop(bb, "corpse_recovered")
            items.sort(key=lambda pair: dist3(bb.pos, pair[1]))
            nearest_item_pos = items[0][1]
            out = self._nav.step_to(bb, nearest_item_pos, tolerance=1.0)
            if out.status == "UNKNOWN":
                return "UNKNOWN"
            if out.status in {"OK", "PROGRESSED"} and bb.writes_this_tick > 0:
                self._pickup_stalls = 0
                return "RUNNING"
            self._pickup_stalls += 1
            if self._pickup_stalls >= 6:
                bb.last_failure_reason = f"corpse_pickup_stalled:{out.reason}"
                return "NEEDS_AI"
            return "RUNNING"

        # Mode B: Unloaded / Cross-Dimension Safe-Prep Mode (run ONCE until kit ready or loaded_radius entered)
        if not in_loaded and not self._prep_done:
            if not _has_decent_pickaxe(bb.inventory_counts):
                res = self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "stone_pickaxe", 1))
                if res in {"FAILED_LOCAL", "NEEDS_AI"}:
                    self._clear_child(bb)
                    self._prep_done = True
                else:
                    return res
            elif not _has_melee_weapon(bb.inventory_counts):
                res = self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "stone_sword", 1))
                if res in {"FAILED_LOCAL", "NEEDS_AI"}:
                    self._clear_child(bb)
                    self._prep_done = True
                else:
                    return res
            else:
                self._prep_done = True

        if not same_dim:
            portals = self.ctx.gateway.scan_blocks(bb.dimension, ["nether_portal"], radius=48, limit=4)
            if portals:
                portals.sort(key=lambda r: float(r.get("dist", 999.0)))
                ppos = portals[0]["pos"]
                out = self._nav.step_to(bb, (ppos[0] + 0.5, float(ppos[1]), ppos[2] + 0.5), tolerance=0.5)
                return "UNKNOWN" if out.status == "UNKNOWN" else "RUNNING"
            bb.last_failure_reason = f"corpse_wrong_dim:{bb.dimension}->{drop.get('dim')}"
            return "NEEDS_AI"

        # Mode A / Rush: route via surface_entry anchor when descending from surface to underground corpse
        nav_target: Tuple[float, float, float] = (drop_pos[0], drop_pos[1], drop_pos[2])
        if not self._via_anchor_done and drop_pos[1] < 55.0 and bb.pos[1] >= 58.0:
            anchor: Optional[Coord3] = load_anchor_landmark(
                self.ctx.gateway.db, bb, "last_surface_anchor", "surface_entry", writer="corpse"
            )
            if anchor is not None:
                anchor_pt = (anchor[0] + 0.5, float(anchor[1]), anchor[2] + 0.5)
                if dist3(bb.pos, anchor_pt) <= 2.5:
                    self._via_anchor_done = True
                elif dist3(bb.pos, anchor_pt) <= 48.0 and dist3(anchor_pt, drop_pos) < dist3(bb.pos, drop_pos):
                    nav_target = anchor_pt

        prev_pos = self._last_pos
        self._last_pos = bb.pos
        out = self._nav.step_to(bb, nav_target, tolerance=2.0)
        if out.status == "UNKNOWN":
            return "UNKNOWN"
        if out.status == "FAILED_LOCAL":
            self._nav_stalls += 1
        elif prev_pos is not None and dist3(prev_pos, bb.pos) < 0.35:
            self._nav_stalls += 1
        else:
            self._nav_stalls = 0

        if self._nav_stalls >= 6:
            bb.last_failure_reason = f"corpse_nav_stalled:{out.reason}"
            return "NEEDS_AI"
        return "RUNNING"
