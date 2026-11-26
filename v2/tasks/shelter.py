"""L3b BuildShelterTask: idempotent 4-orientation StructureSpec block-by-block builder (<= 1 write/tick)."""

from __future__ import annotations

from v2.bt.blackboard import Blackboard
from v2.domain import AIR_BLOCKS, StructureSpec
from v2.primitives.base import PrimitiveContext
from v2.primitives.place_build import PlaceBuildPrimitive
from v2.tasks.acquire import AcquireItemTask
from v2.tasks.base import Task
from v2.tasks.catalogue import TaskCatalogue


class BuildShelterTask(Task):
    """Stateless idempotent shelter builder: inspects missing StructureSpec blocks and places ONE per tick."""

    def __init__(
        self,
        ctx: PrimitiveContext,
        catalogue: TaskCatalogue,
        spec: StructureSpec,
    ) -> None:
        super().__init__(f"shelter:{spec.name}:{spec.facing}")
        self.ctx = ctx
        self.catalogue = catalogue
        self.spec = spec
        self._place = PlaceBuildPrimitive(ctx)

    def _next_missing(self, bb: Blackboard):
        world_blocks = self.spec.world_blocks()
        coords = [pos for pos, _, _ in world_blocks]
        fetched = self.ctx.gateway.prefetch_blocks(bb.dimension, coords)
        for pos, mat, role in world_blocks:
            ent = fetched.get(pos)
            if ent is None or ent.name in AIR_BLOCKS:
                return (pos, mat, role)
        return None

    def is_finished(self, bb: Blackboard) -> bool:
        return self._next_missing(bb) is None

    def on_tick(self, bb: Blackboard) -> str:
        nxt = self._next_missing(bb)
        if nxt is None:
            return "COMPLETED"
        pos, mat, _role = nxt
        if bb.inventory_counts.get(mat, 0) < 1:
            return self.run_child(bb, AcquireItemTask(self.ctx, self.catalogue, mat, 16))
        out = self._place.place_at(bb, mat, pos)
        if out.status in {"OK", "PROGRESSED"}:
            return "RUNNING"
        bb.last_failure_reason = f"shelter_place:{out.reason}"
        return out.status
