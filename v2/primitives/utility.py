"""L3a Primitive 8: UtilityPrimitive (eat, auto-equip best armor/shield, sleep, drop junk, ender_eye)."""

from __future__ import annotations

from typing import Optional

from v2.bt.blackboard import Blackboard
from v2.domain import (
    ARMOR_EQUIP_SLOT_BY_INVENTORY_KEY,
    ARMOR_RANK,
    FOOD_ITEMS,
    JUNK_DROP_PRIORITY,
)
from v2.primitives.base import ActionOutcome, PrimitiveContext


class UtilityPrimitive:
    def __init__(self, ctx: PrimitiveContext) -> None:
        self.ctx = ctx

    def eat_best(self, bb: Blackboard) -> ActionOutcome:
        avail = [(val, nm) for nm, val in FOOD_ITEMS.items() if bb.inventory_counts.get(nm, 0) > 0]
        if not avail:
            return ActionOutcome("FAILED_LOCAL", "eat", reason="no_food")
        avail.sort(reverse=True)
        food = avail[0][1]
        return self.ctx.execute_write(bb, "use_item", {"item": food}, lambda: self.ctx.client.use_item(food))

    def equip_best_armor_or_shield(self, bb: Blackboard) -> ActionOutcome:
        """Equip one better armor piece (chest->torso mapped) or offhand shield."""
        armor_state = bb.inventory.get("armor") if isinstance(bb.inventory.get("armor"), dict) else {}
        suffix_by_inv_key = {"head": "_helmet", "chest": "_chestplate", "legs": "_leggings", "feet": "_boots"}

        for inv_key, suffix in suffix_by_inv_key.items():
            cur_piece = armor_state.get(inv_key)
            cur_name = cur_piece.get("name", "") if isinstance(cur_piece, dict) else ""
            cur_rank = next((r for mat, r in ARMOR_RANK.items() if cur_name.startswith(mat + "_")), 0)
            best_name: Optional[str] = None
            best_rank = cur_rank
            for item_name, cnt in bb.inventory_counts.items():
                if cnt > 0 and item_name.endswith(suffix):
                    rank = next((r for mat, r in ARMOR_RANK.items() if item_name.startswith(mat + "_")), 0)
                    if rank > best_rank:
                        best_rank = rank
                        best_name = item_name
            if best_name is not None:
                equip_slot = ARMOR_EQUIP_SLOT_BY_INVENTORY_KEY[inv_key]
                return self.ctx.execute_write(
                    bb, "equip", {"item": best_name, "slot": equip_slot},
                    lambda bn=best_name, es=equip_slot: self.ctx.client.equip(bn, slot=es),
                )

        offhand = bb.inventory.get("offhand")
        off_name = offhand.get("name", "") if isinstance(offhand, dict) else ""
        if off_name != "shield" and bb.inventory_counts.get("shield", 0) > 0:
            return self.ctx.execute_write(
                bb, "equip", {"item": "shield", "slot": "off-hand"},
                lambda: self.ctx.client.equip("shield", slot="off-hand"),
            )
        return ActionOutcome("OK", "equip", reason="already_optimal")

    def drop_one_junk_stack(self, bb: Blackboard) -> ActionOutcome:
        for junk in JUNK_DROP_PRIORITY:
            cnt = bb.inventory_counts.get(junk, 0)
            if cnt > 0:
                return self.ctx.execute_write(
                    bb, "drop", {"item": junk, "count": cnt},
                    lambda j=junk, c=cnt: self.ctx.client.drop(j, count=c),
                )
        return ActionOutcome("FAILED_LOCAL", "drop", reason="no_droppable_junk")

    def throw_ender_eye(self, bb: Blackboard) -> ActionOutcome:
        if bb.inventory_counts.get("ender_eye", 0) < 1:
            return ActionOutcome("FAILED_LOCAL", "throw_eye", reason="missing_ender_eye")
        return self.ctx.execute_write(
            bb, "use_item", {"item": "ender_eye"},
            lambda: self.ctx.client.use_item("ender_eye"),
        )
