"""Offline unit tests for EquipmentSkill (skills/equipment.py)."""

import copy
import unittest

from core.client import APIError
from skills.equipment import EquipmentSkill


class SimEquipWorld:
    """In-memory inventory/armor/offhand simulator matching mc-agent-api payloads."""

    def __init__(self):
        self.status = {"ready": True, "alive": True, "busy": False, "health": 20, "food": 20}
        self.items = {
            "iron_helmet": 1,
            "iron_chestplate": 1,
            "diamond_chestplate": 1,
            "leather_boots": 1,
            "shield": 1,
        }
        self.armor = {
            "head": None,
            "chest": {"name": "leather_chestplate", "count": 1},
            "legs": None,
            "feet": None,
        }
        self.offhand = None
        self.held = None
        self.equip_calls = []

    def get_status(self, **kwargs):
        return {"ok": True, "result": copy.deepcopy(self.status)}

    def inventory(self, **kwargs):
        return {
            "ok": True,
            "result": {
                "items": [{"name": k, "count": v} for k, v in self.items.items() if v > 0],
                "armor": copy.deepcopy(self.armor),
                "offhand": copy.deepcopy(self.offhand),
                "held": copy.deepcopy(self.held),
                "empty_slots": 20,
            },
        }

    def equip(self, *, item, slot="hand", **kwargs):
        self.equip_calls.append((item, slot))
        if self.items.get(item, 0) <= 0:
            raise APIError("no_such_item", f"No {item} in inventory")
        self.items[item] -= 1
        if self.items[item] <= 0:
            self.items.pop(item, None)
        row = {"name": item, "count": 1}
        if slot == "off-hand":
            self.offhand = row
        elif slot == "hand":
            self.held = row
        else:
            key = "chest" if slot == "torso" else slot
            old = self.armor.get(key)
            if isinstance(old, dict) and old.get("name"):
                self.items[old["name"]] = self.items.get(old["name"], 0) + 1
            self.armor[key] = row
        return {"ok": True, "result": {"held": copy.deepcopy(self.held)}}


class EquipmentSkillTests(unittest.TestCase):
    def test_inspect_reads_armor_chest_offhand_and_available_upgrades(self):
        world = SimEquipWorld()
        skill = EquipmentSkill(world)
        res = skill.execute("inspect")
        self.assertTrue(res.success, res)
        self.assertEqual(res.data["armor"]["torso"]["name"], "leather_chestplate")
        self.assertEqual(res.data["available_upgrades"], {
            "head": "iron_helmet",
            "torso": "diamond_chestplate",
            "feet": "leather_boots",
        })
        self.assertFalse(res.data["executed"])

    def test_equip_handles_torso_to_armor_chest_and_offhand_shield(self):
        world = SimEquipWorld()
        skill = EquipmentSkill(world)
        res_chest = skill.execute("equip", item="iron_chestplate", slot="chest")
        self.assertTrue(res_chest.success, res_chest)
        self.assertEqual(world.armor["chest"]["name"], "iron_chestplate")
        res_shield = skill.execute("equip", item="shield", slot="offhand")
        self.assertTrue(res_shield.success, res_shield)
        self.assertEqual(world.offhand["name"], "shield")
        # Already worn check is idempotent with zero writes.
        before_calls = len(world.equip_calls)
        again = skill.execute("equip", item="shield", slot="off-hand")
        self.assertTrue(again.success, again)
        self.assertTrue(again.data["already_equipped"])
        self.assertEqual(len(world.equip_calls), before_calls)

    def test_auto_equip_best_upgrades_to_strongest_armor_and_equips_offhand_shield(self):
        world = SimEquipWorld()
        skill = EquipmentSkill(world)
        res = skill.execute("auto_equip_best", prefer_offhand="shield")
        self.assertTrue(res.success, res)
        self.assertTrue(res.data["executed"])
        self.assertEqual(res.data["armor"]["head"]["name"], "iron_helmet")
        self.assertEqual(res.data["armor"]["torso"]["name"], "diamond_chestplate")
        self.assertEqual(res.data["armor"]["feet"]["name"], "leather_boots")
        self.assertEqual(res.data["offhand"]["name"], "shield")
        self.assertEqual(world.items.get("iron_chestplate", 0), 1)  # Did not downgrade diamond for iron

    def test_rejects_mismatched_armor_slot_and_preserves_unknown_on_disconnect(self):
        world = SimEquipWorld()
        skill = EquipmentSkill(world)
        bad = skill.execute("equip", item="iron_helmet", slot="feet")
        self.assertFalse(bad.success)
        self.assertEqual(bad.error_code, "bad_args")
        self.assertEqual(world.equip_calls, [])

        def fail_equip(**kwargs):
            raise APIError("request_timeout", "Lost connection after equip sent")

        world.equip = fail_equip
        unknown = skill.execute("equip", item="iron_helmet", slot="head")
        self.assertFalse(unknown.success)
        self.assertEqual(unknown.error_code, "request_timeout")
        self.assertTrue(unknown.data["executed"])
        self.assertEqual(unknown.data["action_state"], "unknown")
        self.assertEqual(unknown.data["cleanup_skipped"], "workflow_unknown")


if __name__ == "__main__":
    unittest.main()
