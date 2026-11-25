"""L3b StrongholdLocateTask & EndCampaignTask (controlled ender-eye sampling & navigation)."""

from __future__ import annotations

from typing import List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import Coord3, dist2_xz
from v2.primitives.base import PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive
from v2.primitives.utility import UtilityPrimitive
from v2.tasks.acquire import AcquireItemTask
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue


class StrongholdLocateTask(Task):
    """Performs bounded ender_eye triangulation sampling and navigates toward estimated stronghold."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        catalogue: TaskCatalogue,
        *,
        estimated_coords: Optional[Tuple[float, float]] = None,
    ) -> None:
        super().__init__("locate_stronghold")
        self.ctx = ctx
        self.catalogue = catalogue
        self.estimated_coords = estimated_coords
        self._util = UtilityPrimitive(ctx)
        self._nav = NavigatePrimitive(ctx)
        self.throws: List[Tuple[float, float, float]] = []

    def is_finished(self, bb: Blackboard) -> bool:
        if bb.dimension == "the_end":
            return True
        if self.estimated_coords is not None:
            return dist2_xz(bb.pos, (self.estimated_coords[0], 0.0, self.estimated_coords[1])) <= 8.0
        return len(self.throws) >= 2

    def on_tick(self, bb: Blackboard) -> str:
        if self.estimated_coords is not None:
            tx, tz = self.estimated_coords
            out = self._nav.step_to(bb, (tx, bb.pos[1], tz), tolerance=8.0, xz_only=True)
            return "RUNNING" if out.status in {"OK", "PROGRESSED"} else out.status
        if bb.inventory_counts.get("ender_eye", 0) < 1:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, "ender_eye", 2))
        out = self._util.throw_ender_eye(bb)
        if out.status == "OK":
            self.throws.append((bb.pos[0], bb.pos[2], float(bb.status.get("yaw", 0.0))))
            return "RUNNING"
        return out.status
