"""L2 HardSafety branches: PendingUnknownGuard + Lava/Fire/Drown/Threat preemption.

Includes v22/v31 battlefield rule:
- Canopy & sealed burrow ground-mob exemption: when elevated `|dy| >= 3` above a ground melee
  mob (zombie/spider) and NOT recently hurt, do NOT infinitely preempt canopy descent or crafting.
  Ranged mobs (skeleton/pillager) or actual `hurt` events always trigger threat response.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.bt.kernel import Node, Status
from v2.domain import HOSTILE_MOBS, RANGED_MOBS, block_coord, dist2_xz, dist3
from v2.gateway.surface_swim import SurfaceSwimExecutor
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.client import APIError
from v2.kernel.journal import PendingTransactionJournal


class PendingUnknownGuard(Node):
    """Highest-priority BT branch: freezes ALL writes whenever a pending unknown transaction exists."""

    def __init__(self, journal: PendingTransactionJournal) -> None:
        super().__init__("PendingUnknownGuard")
        self.journal = journal

    def update(self, bb: Blackboard) -> Status:
        if self.journal.has_pending_unknown():
            tx = self.journal.get_pending()
            bb.set("pending_frozen", True, writer=self.name)
            bb.set("pending_tx", tx, writer=self.name)
            bb.last_action_outcome = "frozen_pending_unknown"
            return Status.RUNNING
        bb.set("pending_frozen", False, writer=self.name)
        return Status.FAILURE


class HardSafetyBranch(Node):
    """Reactive survival reflexes: lava/fire escape, low-oxygen surfacing, and reachable threat evasion."""

    def __init__(
        self,
        gateway: VirtualWorldGateway,
        journal: PendingTransactionJournal,
        *,
        threat_radius: float = 8.0,
        low_oxygen_threshold: int = 10,
    ) -> None:
        super().__init__("HardSafetyBranch")
        self.gateway = gateway
        self.journal = journal
        self.threat_radius = float(threat_radius)
        self.low_oxygen_threshold = int(low_oxygen_threshold)
        self._swimmer = SurfaceSwimExecutor(gateway.client, journal)
        self._douse_cooldown_until: float = 0.0

    def _find_actionable_threat(self, bb: Blackboard) -> Optional[Dict[str, Any]]:
        """Select closest hostile that is actually reachable or ranged, exempting canopy ground melee mobs."""
        px, py, pz = bb.pos
        recently_hurt = (time.time() - bb.last_hurt_at) <= 6.0
        in_burrow = bool(bb.get("in_sealed_burrow", False))

        candidates: List[Tuple[float, Dict[str, Any]]] = []
        for ent in bb.entities:
            name = str(ent.get("name") or "")
            etype = str(ent.get("type") or "")
            if name not in HOSTILE_MOBS and etype != "hostile":
                continue
            epos = ent.get("pos")
            if not isinstance(epos, list) or len(epos) != 3:
                continue
            d3 = dist3((px, py, pz), epos)
            if d3 > self.threat_radius:
                continue
            dy = py - float(epos[1])
            is_ranged = name in RANGED_MOBS

            # Sealed burrow exemption unless actually hurt
            if in_burrow and not recently_hurt:
                continue
            # High canopy / elevation exemption: ground melee mob >= 3 blocks below and not recently hurt
            if not is_ranged and dy >= 3.0 and not recently_hurt:
                continue
            candidates.append((d3, ent))

        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0])
        return candidates[0][1]

    def _write_action(self, bb: Blackboard, endpoint: str, payload: Dict[str, Any], fn: Any) -> Tuple[Status, Optional[Dict[str, Any]], bool]:
        tx_id = self.journal.begin_write(endpoint, payload, dimension=bb.dimension, pos=bb.pos)
        bb.writes_this_tick += 1
        try:
            env = fn()
        except APIError as err:
            if err.details.get("action_state") == "unknown":
                self.journal.resolve_write(tx_id, "unknown", details={"code": err.code, "message": err.message})
                self.gateway.invalidate_unknown_radius(bb.dimension, block_coord(bb.pos))
                bb.set("pending_frozen", True, writer=self.name)
                bb.last_action_outcome = "safety_unknown"
                return Status.RUNNING, None, False
            self.journal.resolve_write(tx_id, "failed_known", details={"code": err.code})
            bb.last_action_outcome = f"safety_failed:{err.code}"
            return Status.RUNNING, None, False
        self.journal.resolve_write(tx_id, "confirmed")
        res = env.get("result") if isinstance(env, dict) else None
        return Status.RUNNING, (res if isinstance(res, dict) else {}), True

    @staticmethod
    def _has_melee_weapon(counts: Dict[str, int]) -> bool:
        return any(k.endswith(("_sword", "_axe")) and v > 0 for k, v in counts.items())

    def update(self, bb: Blackboard) -> Status:
        st = bb.status
        if not st.get("alive", True):
            return Status.RUNNING

        # 1) In lava or on fire -> immediate water bucket douse (with cooldown on failure) or jump-escape
        if st.get("in_lava") or st.get("on_fire"):
            px, py, pz = bb.pos
            now = time.monotonic()
            if bb.inventory_counts.get("water_bucket", 0) > 0 and now >= self._douse_cooldown_until:
                feet = block_coord((px, py, pz))
                bb.last_action_outcome = "safety_douse_water"
                status_ret, res, ok = self._write_action(
                    bb, "interact", {"pos": list(feet), "item": "water_bucket"},
                    lambda: self.gateway.client.interact(pos=list(feet), item="water_bucket"),
                )
                if not ok or (res is not None and res.get("changed") is False and not res.get("used")):
                    self._douse_cooldown_until = now + 4.0
                return status_ret
            bb.last_action_outcome = "safety_escape_lava"
            status_ret, _, _ = self._write_action(
                bb, "control", {"forward": True, "jump": True, "ms": 400},
                lambda: self.gateway.client.control(forward=True, jump=True, ms=400),
            )
            return status_ret

        # 2) Low oxygen / drowning -> pure surface buoyancy pulse
        if st.get("submerged") and bb.oxygen <= self.low_oxygen_threshold:
            res = self._swimmer.swim_step((bb.pos[0], bb.pos[1] + 2.0, bb.pos[2]), st)
            bb.writes_this_tick += 1
            if res.status == "UNKNOWN":
                bb.set("pending_frozen", True, writer=self.name)
            bb.last_action_outcome = f"safety_surface:{res.status.lower()}"
            return Status.RUNNING

        # 3) Actionable nearby hostile threat
        threat = self._find_actionable_threat(bb)
        if threat is None:
            return Status.FAILURE

        epos = threat["pos"]
        px, py, pz = bb.pos
        d = dist3((px, py, pz), epos)
        # Compute retreat vector away from threat (6 blocks)
        vx, vz = px - float(epos[0]), pz - float(epos[2])
        norm = max(1e-6, math.hypot(vx, vz))
        rx, rz = round(px + (vx / norm) * 6.0, 1), round(pz + (vz / norm) * 6.0, 1)

        # If healthy and melee-range with an actual sword/axe and not a creeper, strike once; otherwise retreat!
        tname = str(threat.get("name") or "")
        if d <= 3.5 and bb.health >= 12.0 and tname != "creeper" and self._has_melee_weapon(bb.inventory_counts):
            bb.last_action_outcome = f"safety_strike:{tname}"
            status_ret, _, _ = self._write_action(
                bb, "attack", {"entity_id": threat["id"]},
                lambda: self.gateway.client.attack(int(threat["id"]), timeout_ms=2500, max_hits=2),
            )
            return status_ret

        bb.last_action_outcome = f"safety_retreat:{tname}"
        status_ret, _, _ = self._write_action(
            bb, "move_to", {"x": rx, "z": rz, "range": 1.5, "timeout_ms": 4000},
            lambda: self.gateway.client.move_to(x=rx, z=rz, range=1.5, timeout_ms=4000),
        )
        return status_ret
