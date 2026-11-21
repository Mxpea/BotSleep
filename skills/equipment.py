"""Unified equipment & offhand loadout skill: armor slots, shield/offhand, and best-tier auto-equip."""

import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core.client import APIError
from skills.action_workflow import WorkflowGuard, WorkflowStepError, WorkflowUncertainError
from . import BaseSkill, SkillResult


ARMOR_SLOTS: Tuple[str, ...] = ("head", "torso", "legs", "feet")
SUPPORTED_SLOTS: frozenset = frozenset({"head", "torso", "legs", "feet", "off-hand", "hand"})
SLOT_ALIASES: Dict[str, str] = {
    "head": "head",
    "helmet": "head",
    "torso": "torso",
    "chest": "torso",
    "chestplate": "torso",
    "legs": "legs",
    "leggings": "legs",
    "feet": "feet",
    "boots": "feet",
    "offhand": "off-hand",
    "off-hand": "off-hand",
    "off_hand": "off-hand",
    "hand": "hand",
    "mainhand": "hand",
}
INVENTORY_ARMOR_KEYS: Dict[str, Tuple[str, ...]] = {
    "head": ("head",),
    "torso": ("chest", "torso"),
    "legs": ("legs",),
    "feet": ("feet",),
}
SLOT_SUFFIX: Dict[str, str] = {
    "head": "helmet",
    "torso": "chestplate",
    "legs": "leggings",
    "feet": "boots",
}
ARMOR_TIER_ORDER: Tuple[str, ...] = (
    "netherite",
    "diamond",
    "iron",
    "chainmail",
    "copper",
    "golden",
    "leather",
)
ARMOR_SLOT_ITEMS: Dict[str, Tuple[str, ...]] = {
    slot: tuple(f"{tier}_{suffix}" for tier in ARMOR_TIER_ORDER)
    for slot, suffix in SLOT_SUFFIX.items()
}


def armor_rank(item_name: Optional[str], slot: str) -> int:
    """Return tier score (higher is better, -1 if item is not valid armor for slot)."""
    if not isinstance(item_name, str) or slot not in ARMOR_SLOT_ITEMS:
        return -1
    ordered = ARMOR_SLOT_ITEMS[slot]
    if item_name in ordered:
        return len(ordered) - ordered.index(item_name)
    if slot == "head" and item_name == "turtle_helmet":
        return len(ordered) - ordered.index("iron_helmet")
    return -1


def normalize_slot(slot: Optional[str]) -> Optional[str]:
    """Normalize slot alias to the mc-agent-api /equip destination string."""
    if not isinstance(slot, str):
        return None
    return SLOT_ALIASES.get(slot.strip().lower())


def read_worn_slot_item(inventory_payload: Mapping[str, Any], slot: str) -> Optional[Dict[str, Any]]:
    """Extract worn item dict for a normalized equipment slot from /inventory payload."""
    if slot == "off-hand":
        raw = inventory_payload.get("offhand")
        return dict(raw) if isinstance(raw, dict) and isinstance(raw.get("name"), str) else None
    if slot == "hand":
        raw = inventory_payload.get("held")
        return dict(raw) if isinstance(raw, dict) and isinstance(raw.get("name"), str) else None
    armor = inventory_payload.get("armor")
    if not isinstance(armor, Mapping):
        return None
    for key in INVENTORY_ARMOR_KEYS.get(slot, (slot,)):
        raw = armor.get(key)
        if isinstance(raw, dict) and isinstance(raw.get("name"), str):
            return dict(raw)
    return None


class EquipmentSkill(BaseSkill):
    """Standard armor, offhand, and held loadout skill matching ContainerSkill contracts."""

    def validate_preconditions(self, **kwargs) -> bool:
        """Verify bot is connected and ready."""
        try:
            raw = self.client.get_status()
            st = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
            return bool(isinstance(st, dict) and st.get("ready"))
        except Exception:
            return False

    def execute(
        self,
        action: str = "inspect",
        *,
        item: Optional[str] = None,
        slot: str = "hand",
        prefer_offhand: str = "shield",
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 30.0,
        **kwargs: Any,
    ) -> SkillResult:
        """Dispatch 'inspect', 'equip', or 'auto_equip_best'."""
        act = str(action or "inspect").strip().lower()
        if act == "inspect":
            return self.inspect(cancel_event=cancel_event, timeout_seconds=timeout_seconds)
        if act == "equip":
            if not isinstance(item, str) or not item.strip():
                return SkillResult(False, "Equipment item name is required.", error_code="bad_args")
            return self.equip(item.strip(), slot=slot, cancel_event=cancel_event, timeout_seconds=timeout_seconds)
        if act in {"auto_equip_best", "auto_equip", "upgrade"}:
            return self.auto_equip_best(
                prefer_offhand=prefer_offhand,
                cancel_event=cancel_event,
                timeout_seconds=timeout_seconds,
            )
        return SkillResult(False, f"Unsupported equipment action '{action}'.", error_code="bad_args")

    def inspect(
        self,
        *,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 15.0,
    ) -> SkillResult:
        """Read current armor slots, offhand, held item, and available inventory upgrades."""
        guard = WorkflowGuard(self.client, cancel_event=cancel_event, timeout_seconds=timeout_seconds, max_reads=40, allow_damage=True)
        try:
            guard.check_status(require_safe=False)
            payload = self._read_inventory(guard)
            worn = {s: read_worn_slot_item(payload, s) for s in ARMOR_SLOTS}
            offhand = read_worn_slot_item(payload, "off-hand")
            held = read_worn_slot_item(payload, "hand")
            counts = self._item_counts(payload)
            upgrades = self._compute_armor_upgrades(worn, counts)
            return SkillResult(
                True,
                "Equipment and offhand inspected.",
                data={
                    "armor": worn,
                    "offhand": offhand,
                    "held": held,
                    "counts": counts,
                    "available_upgrades": upgrades,
                    "executed": False,
                },
            )
        except WorkflowUncertainError as exc:
            return self._uncertain_result(exc, guard)
        except WorkflowStepError as exc:
            return SkillResult(False, str(exc), error_code=exc.code, data={"executed": guard.writes > 0})

    def equip(
        self,
        item: str,
        *,
        slot: str = "hand",
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 20.0,
    ) -> SkillResult:
        """Equip an item into a target armor/off-hand/hand slot and verify via /inventory."""
        norm_slot = normalize_slot(slot)
        if norm_slot not in SUPPORTED_SLOTS:
            return SkillResult(False, f"Unsupported equipment slot '{slot}'.", error_code="bad_args")
        if norm_slot in ARMOR_SLOTS and armor_rank(item, norm_slot) < 0:
            return SkillResult(
                False,
                f"Item '{item}' does not match armor slot '{norm_slot}'.",
                error_code="bad_args",
            )
        guard = WorkflowGuard(self.client, cancel_event=cancel_event, timeout_seconds=timeout_seconds, max_reads=60, allow_damage=True)
        try:
            guard.check_status(require_safe=False)
            before = self._read_inventory(guard)
            current = read_worn_slot_item(before, norm_slot)
            if current and current.get("name") == item:
                return SkillResult(
                    True,
                    f"{item} is already equipped in {norm_slot}.",
                    data={"item": item, "slot": norm_slot, "equipped": current, "already_equipped": True, "executed": False},
                )
            counts = self._item_counts(before)
            if counts.get(item, 0) <= 0:
                return SkillResult(
                    False,
                    f"Missing '{item}' in inventory to equip into '{norm_slot}'.",
                    error_code="missing_item",
                    data={"item": item, "slot": norm_slot, "executed": False},
                )
            self._perform_equip(guard, item, norm_slot)
            after = self._read_inventory(guard)
            equipped = read_worn_slot_item(after, norm_slot)
            if not equipped or equipped.get("name") != item:
                return SkillResult(
                    False,
                    f"Expected '{item}' in '{norm_slot}' after equip, observed {equipped.get('name') if equipped else None}.",
                    error_code="equip_unverified",
                    data={"item": item, "slot": norm_slot, "equipped": equipped, "executed": True},
                )
            return SkillResult(
                True,
                f"Equipped {item} into {norm_slot}.",
                data={"item": item, "slot": norm_slot, "equipped": equipped, "executed": True},
            )
        except WorkflowUncertainError as exc:
            return self._uncertain_result(exc, guard)
        except WorkflowStepError as exc:
            return SkillResult(False, str(exc), error_code=exc.code, data={"executed": guard.writes > 0})

    def auto_equip_best(
        self,
        *,
        prefer_offhand: Optional[str] = "shield",
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: float = 35.0,
    ) -> SkillResult:
        """Automatically wear highest-tier armor in inventory and fill off-hand (e.g. shield)."""
        guard = WorkflowGuard(self.client, cancel_event=cancel_event, timeout_seconds=timeout_seconds, max_reads=120, allow_damage=True)
        changes: List[Dict[str, str]] = []
        try:
            guard.check_status(require_safe=False)
            payload = self._read_inventory(guard)
            worn = {s: read_worn_slot_item(payload, s) for s in ARMOR_SLOTS}
            counts = self._item_counts(payload)
            upgrades = self._compute_armor_upgrades(worn, counts)

            for slot in ARMOR_SLOTS:
                target_item = upgrades.get(slot)
                if not target_item:
                    continue
                self._perform_equip(guard, target_item, slot)
                payload = self._read_inventory(guard)
                verified = read_worn_slot_item(payload, slot)
                if verified and verified.get("name") == target_item:
                    changes.append({"slot": slot, "item": target_item})
                    worn[slot] = verified
                    counts = self._item_counts(payload)

            offhand = read_worn_slot_item(payload, "off-hand")
            if (
                isinstance(prefer_offhand, str)
                and prefer_offhand
                and (offhand is None or offhand.get("name") != prefer_offhand)
                and counts.get(prefer_offhand, 0) > 0
            ):
                self._perform_equip(guard, prefer_offhand, "off-hand")
                payload = self._read_inventory(guard)
                offhand = read_worn_slot_item(payload, "off-hand")
                if offhand and offhand.get("name") == prefer_offhand:
                    changes.append({"slot": "off-hand", "item": prefer_offhand})

            worn = {s: read_worn_slot_item(payload, s) for s in ARMOR_SLOTS}
            return SkillResult(
                True,
                f"Loadout updated ({len(changes)} changes)." if changes else "Best available equipment already worn.",
                data={
                    "equipped_changes": changes,
                    "armor": worn,
                    "offhand": offhand,
                    "held": read_worn_slot_item(payload, "hand"),
                    "executed": bool(changes),
                },
            )
        except WorkflowUncertainError as exc:
            return self._uncertain_result(exc, guard, extra={"equipped_changes": changes})
        except WorkflowStepError as exc:
            return SkillResult(
                False,
                str(exc),
                error_code=exc.code,
                data={"equipped_changes": changes, "executed": guard.writes > 0},
            )

    def _perform_equip(self, guard: WorkflowGuard, item: str, slot: str) -> None:
        guard.write(self.client.equip, item=item, slot=slot, timeout_seconds=guard.remaining_timeout(10.0))

    def _read_inventory(self, guard: WorkflowGuard) -> Dict[str, Any]:
        raw = guard.read(self.client.inventory, timeout_seconds=guard.remaining_timeout(10.0))
        res = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
        if not isinstance(res, dict) or not isinstance(res.get("items"), list):
            raise WorkflowUncertainError("invalid_response", "Inventory payload missing items list")
        return res

    @staticmethod
    def _item_counts(payload: Mapping[str, Any]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in payload.get("items") or []:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                cnt = row.get("count", 0)
                if isinstance(cnt, int) and not isinstance(cnt, bool) and cnt > 0:
                    counts[row["name"]] = counts.get(row["name"], 0) + cnt
        return counts

    @staticmethod
    def _compute_armor_upgrades(
        worn: Mapping[str, Optional[Mapping[str, Any]]],
        counts: Mapping[str, int],
    ) -> Dict[str, str]:
        upgrades: Dict[str, str] = {}
        for slot in ARMOR_SLOTS:
            cur_name = (worn.get(slot) or {}).get("name")
            cur_rank = armor_rank(cur_name, slot)
            best_item = None
            best_rank = cur_rank
            for candidate, count in counts.items():
                if count <= 0:
                    continue
                rank = armor_rank(candidate, slot)
                if rank > best_rank:
                    best_rank = rank
                    best_item = candidate
            if best_item is not None:
                upgrades[slot] = best_item
        return upgrades

    @staticmethod
    def _uncertain_result(
        exc: WorkflowUncertainError,
        guard: WorkflowGuard,
        *,
        extra: Optional[Dict[str, Any]] = None,
    ) -> SkillResult:
        return SkillResult(
            False,
            str(exc),
            error_code=exc.code,
            data={
                **(extra or {}),
                **exc.data,
                "executed": guard.writes > 0,
                "completed": False,
                "action_state": "unknown",
                "cleanup_skipped": "workflow_unknown",
            },
        )
