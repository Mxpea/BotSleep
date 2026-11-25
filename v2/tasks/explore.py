"""L3b Voyager-style ExploreUntilTask: spiral/sector exploration terminating as soon as predicate holds."""

from __future__ import annotations

import math
import time
from typing import Callable, Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.domain import dist2_xz
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.primitives.base import PrimitiveContext
from v2.primitives.navigate import NavigatePrimitive
from v2.tasks.base import Task


class ExploreUntilTask(Task):
    """Explores outward in bounded cardinal/diagonal waypoints until predicate(bb, gw) is True."""

    DIRECTIONS: Tuple[Tuple[float, float], ...] = (
        (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0),
        (0.707, 0.707), (-0.707, 0.707), (-0.707, -0.707), (0.707, -0.707),
    )

    def __init__(
        self,
        ctx: PrimitiveContext,
        predicate: Callable[[Blackboard, VirtualWorldGateway], bool],
        *,
        name: str = "explore_until",
        step_distance: float = 20.0,
        max_steps: int = 8,
    ) -> None:
        super().__init__(name)
        self.ctx = ctx
        self.predicate = predicate
        self.step_distance = float(step_distance)
        self.max_steps = max(1, int(max_steps))
        self._nav = NavigatePrimitive(ctx)
        self._origin: Optional[Tuple[float, float, float]] = None
        self._sector_idx: int = 0
        self._steps_taken: int = 0

    def is_finished(self, bb: Blackboard) -> bool:
        return bool(self.predicate(bb, self.ctx.gateway))

    def on_tick(self, bb: Blackboard) -> str:
        if self._origin is None:
            self._origin = bb.pos
        if self._steps_taken >= self.max_steps:
            bb.last_failure_reason = f"{self.name}:explore_budget_exhausted"
            return "NEEDS_AI"

        excluded: Dict[Tuple[int, int], float] = bb.get("excluded_sectors") or {}
        now = time.time()
        wp: Optional[Tuple[float, float, float]] = None
        for _ in range(len(self.DIRECTIONS)):
            dx, dz = self.DIRECTIONS[self._sector_idx % len(self.DIRECTIONS)]
            ring = 1 + (self._sector_idx // len(self.DIRECTIONS))
            cand = (
                self._origin[0] + dx * self.step_distance * ring,
                self._origin[1],
                self._origin[2] + dz * self.step_distance * ring,
            )
            sec = (math.floor(cand[0] / 16.0), math.floor(cand[2] / 16.0))
            if excluded.get(sec, 0.0) > now:
                self._sector_idx += 1
                continue
            wp = cand
            break
        if wp is None:
            dx, dz = self.DIRECTIONS[self._sector_idx % len(self.DIRECTIONS)]
            ring = 1 + (self._sector_idx // len(self.DIRECTIONS))
            wp = (
                self._origin[0] + dx * self.step_distance * ring,
                self._origin[1],
                self._origin[2] + dz * self.step_distance * ring,
            )
        if dist2_xz(bb.pos, wp) <= 3.0:
            self._sector_idx += 1
            self._steps_taken += 1
            return "RUNNING"

        out = self._nav.step_to(bb, wp, tolerance=3.0, xz_only=True)
        if out.status == "OK":
            self._sector_idx += 1
            self._steps_taken += 1
            return "RUNNING"
        if out.status == "FAILED_LOCAL":
            self._sector_idx += 1
            self._steps_taken += 1
            return "RUNNING"
        return "RUNNING"

    def stop(self, bb: Blackboard) -> None:
        super().stop(bb)
