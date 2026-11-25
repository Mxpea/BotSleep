"""L2 LocalRecoveryBranch: urgent eat, canopy descent, water shore escape, and opportunistic auto-sleep."""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from v2.bt.blackboard import Blackboard
from v2.bt.kernel import Node, Status
from v2.domain import FOOD_ITEMS, LEAF_BLOCKS, block_coord
from v2.gateway.surface_swim import SurfaceSwimExecutor
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.client import APIError
from v2.kernel.journal import PendingTransactionJournal


class LocalRecoveryBranch(Node):
    """Handles low-priority local self-maintenance before active long-horizon goals."""

    def __init__(
        self,
        gateway: VirtualWorldGateway,
        journal: PendingTransactionJournal,
        *,
        food_threshold: int = 14,
        low_health_threshold: float = 10.0,
        enable_auto_sleep: bool = True,
        sleep_radius: int = 16,
    ) -> None:
        super().__init__("LocalRecoveryBranch")
        self.gateway = gateway
        self.journal = journal
        self.food_threshold = int(food_threshold)
        self.low_health_threshold = float(low_health_threshold)
        self.enable_auto_sleep = bool(enable_auto_sleep)
        self.sleep_radius = int(sleep_radius)
        self._swimmer = SurfaceSwimExecutor(gateway.client, journal)

    def _best_food(self, counts: Dict[str, int]) -> Optional[str]:
        avail = [(val, name) for name, val in FOOD_ITEMS.items() if counts.get(name, 0) > 0]
        if not avail:
            return None
        avail.sort(reverse=True)
        return avail[0][1]

    def _write_action(self, bb: Blackboard, endpoint: str, payload: Dict[str, Any], fn: Any) -> Status:
        tx_id = self.journal.begin_write(endpoint, payload, dimension=bb.dimension, pos=bb.pos)
        bb.writes_this_tick += 1
        try:
            fn()
        except APIError as err:
            if err.details.get("action_state") == "unknown":
                self.journal.resolve_write(tx_id, "unknown", details={"code": err.code, "message": err.message})
                self.gateway.invalidate_unknown_radius(bb.dimension, block_coord(bb.pos))
                bb.last_action_outcome = "recovery_unknown"
                return Status.RUNNING
            self.journal.resolve_write(tx_id, "failed_known", details={"code": err.code})
            bb.last_action_outcome = f"recovery_failed:{err.code}"
            return Status.FAILURE
        self.journal.resolve_write(tx_id, "confirmed")
        return Status.RUNNING

    def update(self, bb: Blackboard) -> Status:
        st = bb.status
        # 1) Urgent eating when hungry or low health
        if (bb.food <= self.food_threshold or (bb.health <= self.low_health_threshold and bb.food < 20)):
            food = self._best_food(bb.inventory_counts)
            if food is not None:
                bb.last_action_outcome = f"recovery_eat:{food}"
                return self._write_action(
                    bb, "use_item", {"item": food},
                    lambda: self.gateway.client.use_item(food),
                )

        # 2) Canopy descent: if standing directly on tree leaves high off ground, step/drop down safely
        fx, fy, fz = block_coord(bb.pos)
        below = self.gateway.get_block(bb.dimension, (fx, fy - 1, fz), allow_fetch=True)
        if below is not None and below.name in LEAF_BLOCKS:
            bb.last_action_outcome = "recovery_canopy_descend"
            return self._write_action(
                bb, "dig", {"pos": [fx, fy - 1, fz]},
                lambda: self.gateway.client.dig([fx, fy - 1, fz], collect=False),
            )

        # 3) Opportunistic auto-sleep at night when a bed is nearby and not already sleeping
        if self.enable_auto_sleep and not st.get("is_sleeping", False):
            tod = int(st.get("time_of_day", 1000)) % 24000
            if 12541 <= tod <= 23458:
                beds = [
                    b for b in self.gateway.client.get_blocks(radius=self.sleep_radius)
                    if str(b.get("name", "")).endswith("_bed") and isinstance(b.get("pos"), list)
                ]
                if beds:
                    beds.sort(key=lambda b: float(b.get("dist", 999.0)))
                    bpos = beds[0]["pos"]
                    dist = math.dist(bb.pos, (bpos[0] + 0.5, bpos[1] + 0.5, bpos[2] + 0.5))
                    if dist > 2.5:
                        bb.last_action_outcome = "recovery_approach_bed"
                        return self._write_action(
                            bb, "move_to", {"x": bpos[0], "y": bpos[1], "z": bpos[2], "range": 2.0},
                            lambda: self.gateway.client.move_to(bpos[0], bpos[1], bpos[2], range=2.0, timeout_ms=8000),
                        )
                    bb.last_action_outcome = "recovery_sleep"
                    return self._write_action(
                        bb, "interact", {"pos": bpos},
                        lambda: self.gateway.client.interact(pos=bpos),
                    )

        return Status.FAILURE
