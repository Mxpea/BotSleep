"""L2 Tick0 SyncBlackboard: 1-RTT concurrent observation + /events cursor + VoxelWorld3D ingest."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from v2.bt.blackboard import Blackboard
from v2.bt.kernel import Node, Status
from v2.domain import dist2_xz, dist3
from v2.gateway.world_proxy import VirtualWorldGateway
from v2.kernel.client import APIError

_HIGH_VALUE_EXACT = frozenset({
    "shield",
    "bucket",
    "water_bucket",
    "lava_bucket",
    "flint_and_steel",
    "obsidian",
    "blaze_rod",
    "ender_pearl",
    "ender_eye",
    "golden_apple",
    "enchanted_golden_apple",
    "enchanting_table",
})
_HIGH_VALUE_PREFIXES = ("iron_", "diamond_", "netherite_")
_IRREVERSIBLE_CAUSES = ("fell out of the world", "tried to swim in lava")


def _snapshot_inventory(bb: Blackboard) -> Dict[str, Any]:
    armor_raw = bb.inventory.get("armor")
    armor: List[str] = []
    if isinstance(armor_raw, dict):
        for slot_val in armor_raw.values():
            if isinstance(slot_val, dict) and slot_val.get("name"):
                armor.append(str(slot_val["name"]))
    elif isinstance(armor_raw, list):
        for slot_val in armor_raw:
            if isinstance(slot_val, dict) and slot_val.get("name"):
                armor.append(str(slot_val["name"]))
            elif isinstance(slot_val, str) and slot_val:
                armor.append(slot_val)
    off_raw = bb.inventory.get("offhand") or bb.status.get("offhand")
    offhand: Optional[str] = (
        str(off_raw["name"])
        if isinstance(off_raw, dict) and off_raw.get("name")
        else (off_raw if isinstance(off_raw, str) and off_raw else None)
    )
    return {"counts": dict(bb.inventory_counts), "armor": armor, "offhand": offhand}


def _classify_high_value(inv_snapshot: Dict[str, Any]) -> List[str]:
    combined: Dict[str, int] = {}
    for name, cnt in (inv_snapshot.get("counts") or {}).items():
        if int(cnt) > 0:
            combined[str(name)] = combined.get(str(name), 0) + int(cnt)
    for name in inv_snapshot.get("armor") or ():
        if name:
            combined[str(name)] = combined.get(str(name), 0) + 1
    offhand = inv_snapshot.get("offhand")
    if isinstance(offhand, str) and offhand:
        combined[offhand] = combined.get(offhand, 0) + 1

    key_items: List[str] = []
    for name, cnt in sorted(combined.items()):
        if cnt <= 0:
            continue
        if name in _HIGH_VALUE_EXACT:
            key_items.append(name)
        elif name == "raw_iron" and cnt >= 4:
            key_items.append(name)
        elif name == "iron_ingot" and cnt >= 2:
            key_items.append(name)
        elif name not in {"iron_ingot", "iron_nugget"} and name.startswith(_HIGH_VALUE_PREFIXES):
            key_items.append(name)
    return key_items


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
        self._last_observed_pos: Optional[Tuple[float, float, float]] = None
        self._last_observed_dim: Optional[str] = None
        self._respawn_settle_until: float = 0.0
        self._recent_death_records: List[Tuple[float, str, List[float]]] = []

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
        died_this_tick = False
        for evt in bb.recent_events:
            etype = evt.get("type")
            if etype == "hurt":
                bb.last_hurt_at = now
                bb.last_hurt_source = str(evt.get("source") or evt.get("source_entity") or "unknown")
            elif etype == "death":
                died_this_tick = True
                dpos = evt.get("dropped_items_at") or evt.get("pos")
                cause = str(evt.get("cause") or "")
                if isinstance(dpos, list) and len(dpos) == 3:
                    fpos = [float(v) for v in dpos]
                    bb.death_positions.append((bb.dimension, fpos))
                    if self.gateway.db is not None:
                        self.gateway.db.save_landmark(
                            bb.dimension,
                            "death_drop",
                            f"death_{int(now)}",
                            (int(dpos[0]), int(dpos[1]), int(dpos[2])),
                            {"cause": cause},
                        )
                    cause_l = cause.lower()
                    if not any(tag in cause_l for tag in _IRREVERSIBLE_CAUSES):
                        corpse_active = isinstance(bb.get("active_corpse_drop"), dict)
                        inv_snap = bb.get("last_alive_inv")
                        if not isinstance(inv_snap, dict):
                            inv_snap = _snapshot_inventory(bb)
                        key_items = _classify_high_value(inv_snap)
                        if key_items or corpse_active:
                            repeat_zone = sum(
                                1
                                for d_ts, d_dim, d_pos in self._recent_death_records
                                if (now - d_ts) <= 600.0 and d_dim == bb.dimension and dist3(d_pos, fpos) <= 24.0
                            )
                            self._recent_death_records.append((now, bb.dimension, fpos))
                            if len(self._recent_death_records) > 20:
                                self._recent_death_records = self._recent_death_records[-20:]
                            if repeat_zone >= 1:
                                bb.set("active_corpse_drop", None, writer=self.name)
                                bb.log_event(self.name, "corpse_zone_abandoned", f"repeat_deaths={repeat_zone + 1}")
                            elif key_items:
                                bb.set(
                                    "active_corpse_drop",
                                    {
                                        "id": f"death_{int(now * 1000)}_{bb.tick_index}",
                                        "dim": bb.dimension,
                                        "pos": fpos,
                                        "created_at": now,
                                        "loaded_elapsed": 0.0,
                                        "last_tick_ts": now,
                                        "key_items": key_items,
                                        "cause": cause,
                                    },
                                    writer=self.name,
                                )

        drop = bb.get("active_corpse_drop")
        if isinstance(drop, dict):
            last_ts = float(drop.get("last_tick_ts", now))
            dpos = drop.get("pos")
            if (
                bb.dimension == drop.get("dim")
                and isinstance(dpos, (list, tuple))
                and len(dpos) == 3
                and dist2_xz(bb.pos, dpos) <= 160.0
            ):
                drop["loaded_elapsed"] = float(drop.get("loaded_elapsed", 0.0)) + min(
                    5.0, max(0.0, now - last_ts)
                )
            drop["last_tick_ts"] = now
            if float(drop.get("loaded_elapsed", 0.0)) >= 300.0:
                bb.set("active_corpse_drop", None, writer=self.name)

        if not died_this_tick and bool(bb.status.get("alive", True)) and bb.health > 0:
            snap = _snapshot_inventory(bb)
            if snap["counts"] or snap["armor"] or snap["offhand"] or bb.get("last_alive_inv") is None:
                bb.set("last_alive_inv", snap, writer=self.name)
            elif isinstance(bb.get("last_alive_inv"), dict):
                # Player respawned alive with empty inventory -> clear stale pre-death cache
                bb.set("last_alive_inv", snap, writer=self.name)

        now_mono = time.monotonic()
        respawn_jumped = (
            self._last_observed_pos is not None
            and self._last_observed_dim == bb.dimension
            and dist3(bb.pos, self._last_observed_pos) > 40.0
        )
        if died_this_tick or respawn_jumped:
            self._respawn_settle_until = now_mono + 2.5
        self._last_observed_pos = bb.pos
        self._last_observed_dim = bb.dimension

        self.gateway.ingest_status_stance(bb.status)
        if now_mono < self._respawn_settle_until and not bool(bb.status.get("on_ground", True)):
            bb.last_action_outcome = "respawn_world_loading"
            return Status.RUNNING
        return Status.SUCCESS
