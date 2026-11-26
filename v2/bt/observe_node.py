"""L2 Tick0 SyncBlackboard: 1-RTT concurrent observation + /events cursor + VoxelWorld3D ingest."""

from __future__ import annotations

import time
from typing import Any, Dict

from v2.bt.blackboard import Blackboard
from v2.bt.kernel import Node, Status
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.client import APIError


class SyncBlackboardNode(Node):
    """Fetches status, inventory, entities, and incremental events in 1 RTT via AsyncReadPool."""

    TRANSIENT_READ_CODES = frozenset({"not_ready", "wrong_server", "request_timeout", "backend_unavailable"})

    def __init__(
        self,
        gateway: VirtualWorldGateway,
        *,
        entity_radius: int = 32,
        max_transient_errors: int = 10,
    ) -> None:
        super().__init__("SyncBlackboard")
        self.gateway = gateway
        self.entity_radius = int(entity_radius)
        self.max_transient_errors = max(1, int(max_transient_errors))
        self.consecutive_read_errors: int = 0

    def update(self, bb: Blackboard) -> Status:
        try:
            bundle = self.gateway.client.observe_tick_bundle(
                last_event_seq=bb.last_event_seq,
                entity_radius=self.entity_radius,
            )
        except APIError as err:
            if err.code in self.TRANSIENT_READ_CODES and self.consecutive_read_errors + 1 < self.max_transient_errors:
                self.consecutive_read_errors += 1
                bb.last_action_outcome = f"observe_transient:{err.code}"
                bb.log_event(self.name, "transient_read_error", f"{err.code} ({self.consecutive_read_errors})")
                return Status.RUNNING
            raise
        self.consecutive_read_errors = 0
        bb.status = bundle["status"]
        bb.inventory = bundle["inventory"]
        counts: Dict[str, int] = {}
        for item in bb.inventory.get("items", []):
            nm = item.get("name")
            if nm:
                counts[nm] = counts.get(nm, 0) + int(item.get("count", 1))
        bb.inventory_counts = counts
        bb.entities = bundle["entities"]
        bb.recent_events = bundle["events"]
        bb.last_event_seq = bundle["last_event_seq"]

        now = time.time()
        for evt in bb.recent_events:
            etype = evt.get("type")
            if etype == "hurt":
                bb.last_hurt_at = now
                bb.last_hurt_source = str(evt.get("source") or evt.get("source_entity") or "unknown")
            elif etype == "death":
                dpos = evt.get("dropped_items_at") or evt.get("pos")
                if isinstance(dpos, list) and len(dpos) == 3:
                    bb.death_positions.append((bb.dimension, [float(v) for v in dpos]))
                    if self.gateway.db is not None:
                        self.gateway.db.save_landmark(
                            bb.dimension,
                            "death_drop",
                            f"death_{int(now)}",
                            (int(dpos[0]), int(dpos[1]), int(dpos[2])),
                            {"cause": evt.get("cause", "")},
                        )

        self.gateway.ingest_status_stance(bb.status)
        return Status.SUCCESS
