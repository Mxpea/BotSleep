"""Evidence-based daily actions against an in-memory API, without Minecraft."""

import copy
import math
import threading
import unittest
from unittest.mock import patch

from core.client import APIError
from skills.daily_survival import DailySurvivalSkills
from skills.survival import SurvivalSkills


def envelope(result):
    return {"ok": True, "result": copy.deepcopy(result)}


class DailyWorld:
    def __init__(self):
        self.status = {"ready": True, "alive": True, "health": 20, "food": 20, "dimension": "overworld",
                       "gamemode": "survival", "pos": [.5, 64, .5], "busy": None, "on_ground": True}
        self.counts = {}
        self.blocks = {}
        self.entities = []
        self.calls = []
        self.armor = {}
        self.sections = {"container": []}
        self.window_type = "generic_9x3"
        self.info = {}
        self.effects = {}

    def get_status(self, **kwargs):
        self.calls.append(("status",))
        return envelope(self.status)

    def inventory(self, **kwargs):
        self.calls.append(("inventory",))
        return envelope({"items": [{"name": key, "count": value} for key, value in self.counts.items() if value > 0],
                         "armor": self.armor, "empty_slots": 20})

    def get_block(self, pos, **kwargs):
        key = tuple(math.floor(v) for v in pos)
        self.calls.append(("get_block", key))
        return envelope({"name": "stone" if key[1] < 64 else "air", "pos": list(key), "properties": {},
                         "diggable": True, "harvest_tool_ok": True, **self.blocks.get(key, {})})

    def block(self, pos, name, **extra):
        self.blocks[tuple(pos)] = {"name": name, **extra}

    def get_entities(self, **kwargs):
        self.calls.append(("entities",))
        return copy.deepcopy(self.entities)

    def find_blocks(self, types, **kwargs):
        return [{"pos": list(key), **value} for key, value in self.blocks.items() if value["name"] in types]

    def move_to(self, x, y, z, **kwargs):
        self.calls.append(("move", [x, y, z], kwargs))
        self.status["pos"] = [x, y, z]
        if "move" in self.effects:
            return self.effects["move"]()
        return envelope({"pos": [x, y, z]})

    def dig(self, pos, collect=True):
        self.calls.append(("dig", pos))
        if "dig" in self.effects:
            return self.effects["dig"]()
        name = self.blocks[tuple(pos)]["name"]
        item = {"iron_ore": "raw_iron", "deepslate_iron_ore": "raw_iron", "wheat": "wheat",
                "carrots": "carrot", "potatoes": "potato", "beetroots": "beetroot"}[name]
        self.block(pos, "air")
        self.counts[item] = self.counts.get(item, 0) + 1
        return envelope({"gained": {item: 1}})

    def interact(self, pos=None, item=None, **kwargs):
        self.calls.append(("interact", pos, item))
        if "interact" in self.effects:
            return self.effects["interact"]()
        if item.endswith("_hoe"):
            self.block(pos, "farmland")
        else:
            self.block([pos[0], pos[1] + 1, pos[2]], DailySurvivalSkills.SEEDS[item])
            self.counts[item] -= 1
        return envelope({"changed": True})

    def attack(self, entity_id, **kwargs):
        self.calls.append(("attack", entity_id, kwargs))
        if "attack" in self.effects:
            return self.effects["attack"]()
        self.entities = [e for e in self.entities if e["id"] != entity_id]
        self.counts["beef"] = self.counts.get("beef", 0) + 1
        return envelope({"killed": True})

    def equip(self, item, slot="hand"):
        self.calls.append(("equip", item, slot))
        if "equip" not in self.effects:
            self.armor["chest" if slot == "torso" else slot] = {"name": item, "count": 1}
        return envelope({"held": {"name": "stone_pickaxe"}})

    def window_view(self, pos, **kwargs):
        self.calls.append(("window_view", pos))
        if "view" in self.effects:
            return self.effects["view"]()
        return envelope({"window": self.window_type, "sections": self.sections, "info": self.info})

    def window_act(self, pos, ops, **kwargs):
        self.calls.append(("window_act", pos, ops))
        if "window" in self.effects:
            return self.effects["window"](ops)
        done = []
        for op in ops:
            item = op["item"]
            if op["op"] == "put":
                count = min(op["count"], self.counts.get(item, 0))
                self.counts[item] -= count
                rows = self.sections[op["to"]]
                row = next((r for r in rows if r["name"] == item), None)
                if row is None:
                    rows.append({"name": item, "count": count})
                else:
                    row["count"] += count
                done.append({"op": "put", "put": item, "count": count, "to": op["to"]})
            else:
                rows = self.sections[op["from"]]
                row = next(r for r in rows if r["name"] == item)
                # The official output slot always transfers its entire stack.
                count = row["count"] if op["from"] == "output" else min(row["count"], op["count"])
                row["count"] -= count
                self.counts[item] = self.counts.get(item, 0) + count
                if row["count"] == 0:
                    rows.remove(row)
                done.append({"op": "take", "took": {item: count}, "from": op["from"]})
        return envelope({"done": done, "window": {"sections": self.sections}})

    def place_block(self, x, y, z, item=None):
        self.calls.append(("place", [x, y, z], item))
        if "place" in self.effects:
            return self.effects["place"]()
        bed = item.endswith("_bed")
        facing, dx, dz = "south", 0, 1
        if bed:
            # Simulate the API's actual below-reference lookAt, independently
            # of the conservative Python interval helper under test.
            vx, vz = x + .5 - self.status["pos"][0], z + .5 - self.status["pos"][2]
            if abs(vx) > abs(vz):
                facing, dx, dz = ("east", 1, 0) if vx > 0 else ("west", -1, 0)
            else:
                facing, dx, dz = ("south", 0, 1) if vz > 0 else ("north", 0, -1)
        self.block([x, y, z], item, properties={"facing": facing, "part" if bed else "half": "foot" if bed else "lower"})
        self.block([x + dx if bed else x, y if bed else y + 1, z + dz if bed else z], item,
                   properties={"facing": facing, "part" if bed else "half": "head" if bed else "upper"})
        if not bed:
            for cell in ([x, y, z], [x, y + 1, z]):
                self.blocks[tuple(cell)]["properties"].update(open=False, hinge="left")
        self.counts[item] -= 1
        return envelope({"placed": item})

    def stop(self, **kwargs):
        self.calls.append(("stop",))
        return envelope({"stopped": True})


class DailySkillTests(unittest.TestCase):
    def setUp(self):
        self.world = DailyWorld()
        self.skills = DailySurvivalSkills(self.world)
        self.pos = [2, 64, 0]

    def act(self, name, **args):
        return self.skills.execute(name, args)

    def writes(self, name):
        return [call for call in self.world.calls if call[0] == name]

    def furnace(self):
        self.world.block(self.pos, "furnace")
        self.world.window_type = "furnace"
        self.world.sections = {"input": [], "fuel": [], "output": []}

    def test_iron_requires_correct_tier_and_actual_raw_iron_gain(self):
        self.world.block(self.pos, "iron_ore")
        self.world.counts["wooden_pickaxe"] = 1
        result = self.act("gather_resource", pos=self.pos, expected_name="iron_ore")
        self.assertEqual(result.error_code, "missing_tool")
        self.assertFalse(result.data["executed"])
        self.assertFalse(self.writes("dig"))
        self.world.counts["stone_pickaxe"] = 1
        result = self.act("gather_resource", pos=self.pos, expected_name="iron_ore")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], {"raw_iron": 1})
        self.assertEqual(len(self.writes("dig")), 1)
        self.assertNotIn("iron_ore", SurvivalSkills.GATHERABLE)

    def test_gold_pickaxe_does_not_unlock_iron(self):
        self.world.counts["golden_pickaxe"] = 1
        result = self.act("gather_resource", pos=self.pos, expected_name="iron_ore")
        self.assertEqual(result.error_code, "missing_tool")

    def test_crop_requires_fresh_maturity_then_food_gain(self):
        self.world.block(self.pos, "wheat", mature=False)
        self.assertEqual(self.act("harvest_crop", pos=self.pos, expected_name="wheat").error_code, "not_mature")
        self.assertFalse(self.writes("dig"))
        self.world.block(self.pos, "wheat", mature=True)
        result = self.act("harvest_crop", pos=self.pos, expected_name="wheat")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], {"wheat": 1})

    def test_crop_seed_only_or_backend_gained_claim_is_not_food_success(self):
        self.world.block(self.pos, "wheat", mature=True)
        def dig():
            self.world.block(self.pos, "air")
            self.world.counts["wheat_seeds"] = 1
            return envelope({"gained": {"wheat": 99}})
        self.world.effects["dig"] = dig
        result = self.act("harvest_crop", pos=self.pos, expected_name="wheat")
        self.assertEqual(result.error_code, "gather_unverified")
        self.assertTrue(result.data["block_removed"])

    def test_crop_blocking_wall_and_foreign_crop_are_rejected(self):
        self.assertEqual(self.act("harvest_crop", pos=self.pos, expected_name="melon_stem").error_code, "unsupported_crop")
        self.world.block([3, 64, 0], "carrots", mature=True)
        self.world.block([1, 65, 0], "stone")
        result = self.act("harvest_crop", pos=[3, 64, 0], expected_name="carrots")
        self.assertEqual(result.error_code, "target_occluded")
        self.assertFalse(self.writes("dig"))

    def test_plant_verifies_crop_and_seed_consumption(self):
        self.world.block(self.pos, "farmland")
        self.world.counts["carrot"] = 2
        result = self.act("plant_crop", pos=self.pos, item="carrot")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["planted"], "carrots")
        self.assertEqual(result.data["pos"], [2, 65, 0])

    def seed_plant(self, name="short_grass", count=0):
        self.world.block(self.pos, name, properties={"half": "lower"} if name == "tall_grass" else {})
        self.world.block([2, 63, 0], "grass_block")
        if name == "tall_grass":
            self.world.block([2, 65, 0], name, properties={"half": "upper"})
        def dig():
            self.world.block(self.pos, "air")
            if name == "tall_grass":
                self.world.block([2, 65, 0], "air")
            self.world.counts["wheat_seeds"] = self.world.counts.get("wheat_seeds", 0) + count
            return envelope({"gained": {"wheat_seeds": 99}})
        self.world.effects["dig"] = dig
        return dig

    def test_seed_grass_removed_with_zero_drop_is_not_seed_goal_completion(self):
        self.seed_plant()
        result = self.act("gather_seeds", pos=self.pos, expected_name="short_grass")
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["plant_removed"])
        self.assertFalse(result.data["completed"])
        self.assertEqual(result.data["gained"], 0)
        self.assertEqual(len(self.writes("dig")), 1)
        self.assertNotIn("short_grass", SurvivalSkills.GATHERABLE)

    def test_seed_gain_uses_inventory_delta_and_tall_upper_resolves_one_plant(self):
        self.world.counts["wheat_seeds"] = 4
        self.seed_plant("tall_grass", 1)
        result = self.act("gather_seeds", pos=[2, 65, 0], expected_name="tall_grass")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], 1)
        self.assertTrue(result.data["completed"])
        self.assertEqual(self.writes("dig"), [("dig", self.pos)])

    def test_seed_requires_whole_plant_removal(self):
        self.seed_plant("tall_grass")
        def partial():
            self.world.block(self.pos, "air")
            return envelope({})
        self.world.effects["dig"] = partial
        result = self.act("gather_seeds", pos=self.pos, expected_name="tall_grass")
        self.assertEqual(result.error_code, "plant_unverified")
        self.assertFalse(result.success)

    def test_seed_safety_refusals_do_not_dig(self):
        for problem in ("liquid", "column", "hostile", "airborne", "occluded", "foreign", "far", "support"):
            self.setUp()
            self.seed_plant()
            pos, name = self.pos, "short_grass"
            if problem == "liquid": self.world.block([3, 64, 0], "water")
            elif problem == "column": self.world.status["pos"] = [2.5, 64, .5]
            elif problem == "hostile": self.world.status["nearest_hostile"] = {"dist": 3}
            elif problem == "airborne": self.world.status["on_ground"] = False
            elif problem == "occluded": self.world.block([1, 65, 0], "stone")
            elif problem == "foreign": name = "fern"
            elif problem == "far": self.world.status["pos"] = [-5.5, 64, .5]
            elif problem == "support": self.world.block([2, 63, 0], "air")
            with self.subTest(problem=problem):
                result = self.act("gather_seeds", pos=pos, expected_name=name)
                self.assertFalse(result.success)
                self.assertFalse(self.writes("dig"))
                self.assertFalse(self.writes("move"))

    def test_seed_transport_unknown_has_no_reads_stop_or_retry_after_dig(self):
        for code in ("request_timeout", "network_error", "invalid_json"):
            self.setUp()
            self.seed_plant()
            def fail(): raise APIError(code, "uncertain")
            self.world.effects["dig"] = fail
            result = self.act("gather_seeds", pos=self.pos, expected_name="short_grass")
            with self.subTest(code=code):
                self.assertEqual(result.error_code, code)
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(self.world.calls[-1][0], "dig")
                self.assertEqual(len(self.writes("dig")), 1)

    @patch("skills.survival.time.sleep")
    def test_seed_unconfirmed_removal_zero_drop_or_inventory_gain_recovers_once(self, sleep):
        for count in (0, 1):
            self.setUp()
            remove = self.seed_plant(count=count)
            def fail():
                remove()
                raise APIError("dig_unconfirmed", "server confirmation missing", details={"confirmed": False})
            self.world.effects["dig"] = fail
            result = self.act("gather_seeds", pos=self.pos, expected_name="short_grass")
            with self.subTest(count=count):
                self.assertTrue(result.success, result)
                self.assertEqual(result.data["gained"], count)
                self.assertEqual(result.data["completed"], bool(count))
                self.assertEqual(result.data["recovered_from"], "dig_unconfirmed")
                self.assertEqual(len(self.writes("dig")), 1)
                self.assertEqual(len(self.writes("stop")), 1)

    @patch("skills.survival.time.sleep")
    def test_seed_unconfirmed_original_cache_never_becomes_safe_retry(self, sleep):
        self.seed_plant()
        def fail(): raise APIError("dig_unconfirmed", "missing confirmation")
        self.world.effects["dig"] = fail
        result = self.act("gather_seeds", pos=self.pos, expected_name="short_grass")
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(result.data["recovery_attempts"], 3)
        self.assertEqual(len(self.writes("dig")), 1)
        self.assertEqual(len(self.writes("stop")), 1)

    def test_seed_cancel_during_unconfirmed_dig_retains_unknown(self):
        self.seed_plant()
        event = threading.Event()
        def fail():
            event.set()
            raise APIError("dig_unconfirmed", "missing confirmation")
        self.world.effects["dig"] = fail
        result = self.skills.execute("gather_seeds", {"pos": self.pos, "expected_name": "short_grass"}, event)
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(len(self.writes("dig")), 1)
        self.assertEqual(len(self.writes("stop")), 1)

    def test_seed_unconfirmed_stop_failure_preserves_unknown(self):
        self.seed_plant()
        def dig(): raise APIError("dig_unconfirmed", "missing confirmation")
        def stop(**kwargs):
            self.world.calls.append(("stop",))
            raise APIError("network_error", "unreachable")
        self.world.effects["dig"] = dig
        self.world.stop = stop
        result = self.act("gather_seeds", pos=self.pos, expected_name="short_grass")
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(result.data["recovery_stage"], "stop")
        self.assertEqual(len(self.writes("dig")), 1)
        self.assertEqual(len(self.writes("stop")), 1)

    def test_seed_postwrite_inventory_failure_does_not_append_another_write(self):
        remove = self.seed_plant()
        def dig():
            result = remove()
            def inventory(**kwargs): raise APIError("request_timeout", "unknown read")
            self.world.inventory = inventory
            return result
        self.world.effects["dig"] = dig
        result = self.act("gather_seeds", pos=self.pos, expected_name="short_grass")
        self.assertEqual(result.error_code, "request_timeout")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(len(self.writes("dig")), 1)
        self.assertFalse(self.writes("stop"))

    def foliage(self, name="oak_leaves", pos=None):
        pos = pos or [1, 64, 0]
        self.world.status.update(is_day=True, time_of_day=3000)
        self.world.block(pos, name, properties={"waterlogged": False})
        def dig(target, collect=True):
            self.world.calls.append(("dig", target, collect))
            self.world.block(target, "air")
            return envelope({"gained": {}})
        self.world.dig = dig
        return pos

    def test_foliage_zero_drops_success_uses_air_and_never_collects_or_walks(self):
        pos = self.foliage()
        result = self.act("clear_foliage", pos=pos, expected_name="oak_leaves")
        self.assertTrue(result.success, result)
        self.assertTrue(result.data["block_removed"])
        self.assertEqual(result.data["remaining_block"], "air")
        self.assertFalse(result.data["collected"])
        self.assertEqual(self.writes("dig"), [("dig", pos, False)])
        self.assertFalse(self.writes("move"))
        self.assertFalse(self.writes("inventory"))
        self.assertFalse(self.writes("entities"))
        self.assertNotIn("oak_leaves", SurvivalSkills.GATHERABLE)

    def test_foliage_vine_and_azalea_are_supported(self):
        for name in ("vine", "azalea_leaves", "flowering_azalea_leaves"):
            self.setUp()
            pos = self.foliage(name, [1, 65, 0])
            with self.subTest(name=name):
                self.assertTrue(self.act("clear_foliage", pos=pos, expected_name=name).success)

    def test_foliage_refuses_own_floor_head_column_far_and_other_levels(self):
        for pos in ([0, 63, 0], [0, 64, 0], [0, 65, 0], [0, 66, 0], [4, 64, 0], [2, 64, 1], [1, 63, 0], [1, 66, 0]):
            self.setUp()
            self.foliage(pos=pos)
            with self.subTest(pos=pos):
                self.assertEqual(self.act("clear_foliage", pos=pos, expected_name="oak_leaves").error_code, "unsafe_target")
                self.assertFalse(self.writes("dig"))

    def test_foliage_live_respawn_diagonal_leaf_can_be_cleared_without_moving(self):
        # Real failed proposal: feet [1,122,0], visible diagonal [0,122,-1].
        # This fixture asserts local reach only, not a complete escape route.
        pos = self.foliage("birch_leaves", [0, 122, -1])
        self.world.status["pos"] = [1.5, 122, .5]
        self.world.block([1, 121, 0], "birch_leaves", properties={"waterlogged": False})
        result = self.act("clear_foliage", pos=pos, expected_name="birch_leaves")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["pos_after"], [1.5, 122, .5])
        self.assertEqual(self.writes("dig"), [("dig", pos, False)])
        self.assertFalse(self.writes("move"))
        self.assertEqual(self.world.blocks[(1, 121, 0)]["name"], "birch_leaves")

    def test_foliage_diagonal_still_requires_clear_voxel_sight(self):
        pos = self.foliage("birch_leaves", [0, 122, -1])
        self.world.status["pos"] = [1.5, 122, .5]
        self.world.block([0, 123, 0], "stone")
        self.world.block([1, 123, -1], "stone")
        result = self.act("clear_foliage", pos=pos, expected_name="birch_leaves")
        self.assertEqual(result.error_code, "target_occluded")
        self.assertFalse(self.writes("dig"))

    def test_foliage_diagonal_unknown_reply_never_adds_a_retry_or_stop(self):
        pos = self.foliage("birch_leaves", [0, 122, -1])
        self.world.status["pos"] = [1.5, 122, .5]
        def dig(target, collect=True):
            self.world.calls.append(("dig", target, collect))
            raise APIError("dig_unconfirmed", "missing acknowledgement")
        self.world.dig = dig
        result = self.act("clear_foliage", pos=pos, expected_name="birch_leaves")
        self.assertEqual(result.error_code, "dig_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(self.world.calls[-1], ("dig", pos, False))
        self.assertEqual(len(self.writes("dig")), 1)
        self.assertFalse(self.writes("stop"))

    def test_foliage_refuses_night_hunger_low_health_airborne_hazard_and_hostile(self):
        for change in ({"is_day": False}, {"time_of_day": 11000}, {"health": 11.9}, {"food": 7},
                       {"on_ground": False}, {"in_water": True}, {"thundering": True},
                       {"nearest_hostile": {"dist": 7.9}}):
            self.setUp()
            pos = self.foliage()
            self.world.status.update(change)
            with self.subTest(change=change):
                self.assertFalse(self.act("clear_foliage", pos=pos, expected_name="oak_leaves").success)
                self.assertFalse(self.writes("dig"))

    def test_foliage_wrong_type_waterlogged_and_adjacent_liquid_never_dig(self):
        for problem in ("foreign", "changed", "waterlogged", "unknown_waterlogged", "liquid"):
            self.setUp()
            pos = self.foliage()
            name = "stone" if problem == "foreign" else "oak_leaves"
            if problem == "changed": self.world.block(pos, "birch_leaves", properties={"waterlogged": False})
            elif problem == "waterlogged": self.world.block(pos, "oak_leaves", properties={"waterlogged": True})
            elif problem == "unknown_waterlogged": self.world.block(pos, "oak_leaves", properties={})
            elif problem == "liquid": self.world.block([2, 64, 0], "lava")
            with self.subTest(problem=problem):
                self.assertFalse(self.act("clear_foliage", pos=pos, expected_name=name).success)
                self.assertFalse(self.writes("dig"))

    def test_foliage_unclear_visibility_never_digs(self):
        pos = self.foliage()
        with patch("skills.daily_survival.trace_block_visibility", return_value={"visible": False, "state": "occluded"}):
            result = self.act("clear_foliage", pos=pos, expected_name="oak_leaves")
        self.assertEqual(result.error_code, "target_occluded")
        self.assertFalse(self.writes("dig"))

    def test_foliage_latest_changed_stance_prevents_write(self):
        pos = self.foliage()
        original = self.world.get_status
        def status(**kwargs):
            if len(self.writes("status")) >= 2:
                self.world.status["pos"] = [1.5, 64, .5]
            return original(**kwargs)
        self.world.get_status = status
        result = self.act("clear_foliage", pos=pos, expected_name="oak_leaves")
        self.assertEqual(result.error_code, "position_changed")
        self.assertFalse(self.writes("dig"))

    def test_foliage_unknown_reply_has_no_extra_action_or_read(self):
        for code in ("dig_unconfirmed", "request_timeout", "network_error", "invalid_json"):
            self.setUp()
            pos = self.foliage()
            def dig(target, collect=True):
                self.world.calls.append(("dig", target, collect))
                raise APIError(code, "unconfirmed")
            self.world.dig = dig
            result = self.act("clear_foliage", pos=pos, expected_name="oak_leaves")
            with self.subTest(code=code):
                self.assertEqual(result.error_code, code)
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertEqual(self.world.calls[-1][0], "dig")
                self.assertEqual(len(self.writes("dig")), 1)
                self.assertFalse(self.writes("stop"))

    def test_foliage_ack_without_air_does_not_claim_removal(self):
        pos = self.foliage()
        def dig(target, collect=True):
            self.world.calls.append(("dig", target, collect))
            return envelope({"dug": True})
        self.world.dig = dig
        result = self.act("clear_foliage", pos=pos, expected_name="oak_leaves")
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(len(self.writes("dig")), 1)
        self.assertFalse(self.writes("stop"))

    def test_foliage_cancel_after_write_remains_unknown_no_retry(self):
        pos = self.foliage()
        event = threading.Event()
        def dig(target, collect=True):
            self.world.calls.append(("dig", target, collect))
            event.set()
            return envelope({})
        self.world.dig = dig
        result = self.skills.execute("clear_foliage", {"pos": pos, "expected_name": "oak_leaves"}, event)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(len(self.writes("dig")), 1)
        self.assertFalse(self.writes("stop"))

    def test_plant_ack_without_world_change_is_not_success(self):
        self.world.block(self.pos, "farmland")
        self.world.counts["wheat_seeds"] = 1
        self.world.effects["interact"] = lambda: envelope({"changed": True})
        self.assertEqual(self.act("plant_crop", pos=self.pos, item="wheat_seeds").error_code, "plant_unverified")

    def test_till_needs_hoe_water_and_farmland_result(self):
        self.world.block(self.pos, "grass_block")
        self.world.counts["stone_hoe"] = 1
        self.assertEqual(self.act("till_soil", pos=self.pos).error_code, "no_water")
        self.world.block([4, 64, 0], "water")
        result = self.act("till_soil", pos=self.pos)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["tilled"], "farmland")

    def test_hunting_forbids_players_pets_and_unarmed_attack(self):
        for name in ("player", "wolf", "cat", "villager", "zombie"):
            result = self.act("hunt_animal", entity_id=1, expected_name=name)
            self.assertEqual(result.error_code, "unsafe_target")
        self.world.entities = [{"id": 1, "name": "cow", "type": "mob", "pos": self.pos}]
        self.assertEqual(self.act("hunt_animal", entity_id=1, expected_name="cow").error_code, "missing_tool")
        self.assertFalse(self.writes("attack"))

    def test_hunting_has_one_bounded_attack_and_verifies_inventory(self):
        self.world.entities = [{"id": 1, "name": "cow", "type": "mob", "pos": self.pos}]
        self.world.counts["stone_sword"] = 1
        result = self.act("hunt_animal", entity_id=1, expected_name="cow")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], {"beef": 1})
        self.assertEqual(self.writes("attack")[0][2], {"timeout_ms": 6000, "max_hits": 6})

    def test_hunting_killed_flag_without_loot_is_unverified(self):
        self.world.entities = [{"id": 1, "name": "cow", "type": "mob", "pos": self.pos}]
        self.world.counts["stone_sword"] = 1
        self.world.effects["attack"] = lambda: envelope({"killed": True})
        self.assertEqual(self.act("hunt_animal", entity_id=1, expected_name="cow").error_code, "hunt_unverified")

    def test_equipment_requires_actual_armor_slot_not_held_ack(self):
        self.world.counts["iron_chestplate"] = 1
        result = self.act("equip_armor", item="iron_chestplate", slot="torso")
        self.assertTrue(result.success, result)
        self.world.armor.clear()
        self.world.effects["equip"] = True
        self.assertEqual(self.act("equip_armor", item="iron_chestplate", slot="torso").error_code, "equip_unverified")

    def test_start_smelting_only_reports_loaded_job_never_finished_ingots(self):
        self.furnace()
        self.world.counts = {"raw_iron": 3, "coal": 1}
        result = self.act("smelt_start", pos=self.pos, input_item="raw_iron", count=3, fuel_item="coal", fuel_count=1)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["stage"], "started")
        self.assertFalse(result.data["completed"])
        self.assertEqual(result.data["expected_output"], "iron_ingot")
        self.assertNotIn("iron_ingot", self.world.counts)
        self.assertEqual(len(self.writes("window_act")), 1)

    def test_smelting_busy_furnace_or_wrong_type_does_not_transfer(self):
        self.furnace()
        self.world.counts = {"raw_iron": 1, "coal": 1}
        self.world.sections["input"] = [{"name": "raw_iron", "count": 2}]
        result = self.act("smelt_start", pos=self.pos, input_item="raw_iron", count=1, fuel_item="coal", fuel_count=1)
        self.assertEqual(result.error_code, "container_busy")
        self.world.block(self.pos, "smoker")
        self.world.sections["input"] = []
        self.assertEqual(self.act("smelt_start", pos=self.pos, input_item="raw_iron", count=1, fuel_item="coal", fuel_count=1).error_code, "unsupported_recipe")
        self.assertFalse(self.writes("window_act"))

    def test_smelting_partial_batch_retains_done_and_never_replays(self):
        self.furnace()
        self.world.counts = {"raw_iron": 3, "coal": 1}
        done = [{"op": "put", "put": "raw_iron", "count": 3, "to": "input"}]
        def partial(ops):
            self.world.counts["raw_iron"] = 0
            self.world.sections["input"] = [{"name": "raw_iron", "count": 3}]
            raise APIError("section_full", "Fuel rejected", details={"failed_op": 1, "done": done})
        self.world.effects["window"] = partial
        result = self.act("smelt_start", pos=self.pos, input_item="raw_iron", count=3, fuel_item="coal", fuel_count=1)
        self.assertEqual(result.error_code, "partial_completion")
        self.assertEqual(result.data["transferred"], done)
        self.assertTrue(result.data["executed"])
        self.assertEqual(len(self.writes("window_act")), 1)

    def test_uncertain_window_write_preserves_code_and_unknown_state(self):
        self.furnace()
        self.world.counts = {"raw_iron": 1, "coal": 1}
        def fail(ops):
            raise APIError("invalid_json", "Broken response")
        self.world.effects["window"] = fail
        result = self.act("smelt_start", pos=self.pos, input_item="raw_iron", count=1, fuel_item="coal", fuel_count=1)
        self.assertEqual(result.error_code, "invalid_json")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertTrue(result.data["executed"])
        self.assertEqual(len(self.writes("window_act")), 1)


    def test_window_op_failure_without_done_still_cannot_prove_no_write(self):
        self.world.block(self.pos, "chest")
        self.world.counts["oak_log"] = 3
        def uncertain(ops):
            self.world.counts["oak_log"] -= 1
            raise APIError("window_op_failed", "Click failed", details={"failed_op": 0, "done": []})
        self.world.effects["window"] = uncertain
        result = self.act("store_items", pos=self.pos, item="oak_log", count=3)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["api_code"], "window_op_failed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(len(self.writes("window_act")), 1)

    def test_success_envelope_with_window_interruption_is_still_unknown(self):
        self.world.block(self.pos, "chest")
        self.world.counts["oak_log"] = 1
        self.world.effects["window"] = lambda ops: envelope({"done": [{"op": "put", "count": 1}], "interrupted_by": "hurt"})
        result = self.act("store_items", pos=self.pos, item="oak_log", count=1)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(result.data["transferred"], [{"op": "put", "count": 1}])
        self.assertTrue(result.data["executed"])
        self.assertEqual(len(self.writes("window_act")), 1)

    def test_smelting_shared_input_and_fuel_does_not_double_count_stock(self):
        self.furnace()
        self.world.counts["oak_log"] = 1
        result = self.act("smelt_start", pos=self.pos, input_item="oak_log", count=1, fuel_item="oak_log", fuel_count=1)
        self.assertEqual(result.error_code, "missing_item")
        self.world.counts["oak_log"] = 2
        result = self.act("smelt_start", pos=self.pos, input_item="oak_log", count=1, fuel_item="oak_log", fuel_count=1)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["inventory_removed"], {"oak_log": 2})

    def test_post_write_observation_failure_stays_unknown(self):
        self.world.block(self.pos, "chest")
        self.world.counts["oak_log"] = 1
        def put(ops):
            self.world.inventory = lambda **kwargs: (_ for _ in ()).throw(APIError("network_error", "Read failed"))
            return envelope({"done": [{"op": "put", "count": 1}]})
        self.world.effects["window"] = put
        result = self.act("store_items", pos=self.pos, item="oak_log", count=1)
        self.assertEqual(result.error_code, "network_error")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertTrue(result.data["executed"])

    def test_respawn_during_transfer_cannot_use_new_life_inventory_as_evidence(self):
        self.world.block(self.pos, "chest")
        self.world.counts["oak_log"] = 1
        def put(ops):
            self.world.status["last_death"] = {"t": 1234, "pos": [.5, 64, .5]}
            self.world.counts["oak_log"] = 0
            self.world.sections["container"] = [{"name": "oak_log", "count": 1}]
            return envelope({"done": [{"op": "put", "count": 1}]})
        self.world.effects["window"] = put
        result = self.act("store_items", pos=self.pos, item="oak_log", count=1)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertEqual(len(self.writes("window_act")), 1)

    def test_smelt_collect_allows_official_whole_output_stack(self):
        self.furnace()
        self.world.sections["output"] = [{"name": "iron_ingot", "count": 3}]
        result = self.act("smelt_collect", pos=self.pos, item="iron_ingot", count=1)
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], 3)

    def test_no_finished_output_is_not_ready_and_no_write(self):
        self.furnace()
        result = self.act("smelt_collect", pos=self.pos, item="iron_ingot", count=1)
        self.assertEqual(result.error_code, "not_ready")
        self.assertFalse(result.data["executed"])
        self.assertFalse(self.writes("window_act"))

    def test_chest_store_take_and_inspect_use_actual_two_sided_changes(self):
        self.world.block(self.pos, "chest")
        self.world.counts["oak_log"] = 4
        stored = self.act("store_items", pos=self.pos, item="oak_log", count=3)
        self.assertTrue(stored.success, stored)
        self.assertEqual(stored.data["stored"], 3)
        taken = self.act("take_items", pos=self.pos, item="oak_log", count=2)
        self.assertTrue(taken.success, taken)
        self.assertEqual(taken.data["gained"], 2)
        view = self.act("inspect_container", pos=self.pos)
        self.assertTrue(view.success, view)
        self.assertFalse(view.data["executed"])
        self.assertEqual(view.data["window"]["sections"]["container"], [{"name": "oak_log", "count": 1}])

    def test_storage_requires_container_change_not_just_player_inventory(self):
        self.world.block(self.pos, "chest")
        self.world.counts["oak_log"] = 3
        def fake(ops):
            self.world.counts["oak_log"] = 0
            return envelope({"done": [{"put": "oak_log", "count": 3}]})
        self.world.effects["window"] = fake
        result = self.act("store_items", pos=self.pos, item="oak_log", count=3)
        self.assertEqual(result.error_code, "partial_completion")

    def test_pickup_needs_observed_item_and_actual_gain(self):
        self.world.entities = [{"id": 3, "name": "item", "type": "object", "pos": [2.5, 64, .5], "item": "oak_logx2"}]
        def pickup():
            self.world.counts["oak_log"] = 2
            return envelope({"pos": self.world.status["pos"]})
        self.world.effects["move"] = pickup
        result = self.act("collect_drops", entity_id=3, expected_item="oak_log")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], 2)
        self.assertEqual(self.writes("move")[0][2]["range"], 0)
        self.assertEqual(len(self.writes("move")), 1)

    def test_pickup_rejects_liquid_and_does_not_trust_move_ack(self):
        self.world.entities = [{"id": 3, "name": "item", "type": "object", "pos": [2.5, 64, .5], "item": "oak_logx2"}]
        self.world.block(self.pos, "water")
        self.assertEqual(self.act("collect_drops", entity_id=3, expected_item="oak_log").error_code, "unsafe")
        self.assertFalse(self.writes("move"))
        self.world.block(self.pos, "air")
        self.assertEqual(self.act("collect_drops", entity_id=3, expected_item="oak_log").error_code, "pickup_unverified")

    def test_bed_uses_actual_orientation_and_verifies_both_halves(self):
        self.world.counts["white_bed"] = 1
        result = self.act("place_bed", pos=self.pos, item="white_bed")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["facing"], "east")
        self.assertEqual(result.data["second_pos"], [3, 64, 0])

    def test_bed_rejects_missing_second_support_before_write(self):
        self.world.counts["white_bed"] = 1
        self.world.block([3, 63, 0], "air")
        result = self.act("place_bed", pos=self.pos, item="white_bed")
        self.assertEqual(result.error_code, "needs_position")
        self.assertFalse(self.writes("place"))

    def test_bed_facing_interval_contains_diagonal_rounding_uncertainty(self):
        self.assertEqual(self.skills.placement_facings([2, 64, 2], [.5, 64, .5]), ["east", "south"])
        self.assertEqual(self.skills.placement_facings([-1, 64, 0], [-.5, 64, -.5]), ["south"])
        self.assertEqual(self.skills.placement_facings([2, 64, 0], [.5, 64, .5]), ["east"])

    def test_bed_can_be_placed_along_side_wall_of_completed_five_by_five_room(self):
        for x in range(-2, 3):
            for z in range(-2, 3):
                for y in (64, 65, 66):
                    if abs(x) == 2 or abs(z) == 2 or y == 66:
                        self.world.block([x, y, z], "cobblestone")
        self.world.status["pos"] = [-.5, 64, -.5]
        self.world.counts["white_bed"] = 1
        result = self.act("place_bed", pos=[-1, 64, 0], item="white_bed")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["predicted_facings"], ["south"])
        self.assertEqual(result.data["second_pos"], [-1, 64, 1])
        self.assertEqual(self.world.blocks[(-2, 64, 0)]["name"], "cobblestone")
        self.assertEqual(len(self.writes("place")), 1)

    def test_corner_foot_from_inside_room_does_not_blindly_place_into_wall(self):
        self.world.counts["white_bed"] = 1
        self.world.block([-2, 64, 1], "cobblestone")
        self.world.block([-1, 64, 2], "cobblestone")
        result = self.act("place_bed", pos=[-1, 64, 1], item="white_bed")
        self.assertEqual(result.error_code, "needs_position")
        self.assertEqual(result.data["predicted_facings"], ["south", "west"])
        self.assertFalse(self.writes("place"))

    def test_bed_wrong_stance_returns_only_supported_clear_stance_candidates(self):
        self.world.counts["white_bed"] = 1
        self.world.block([-2, 64, 0], "cobblestone")
        result = self.act("place_bed", pos=[-1, 64, 0], item="white_bed")
        self.assertEqual(result.error_code, "needs_position")
        self.assertIn({"pos": [-.5, 64, -.5], "facing": "south"}, result.data["stand_candidates"])
        self.assertTrue(result.data["stance_geometry_only"])
        self.assertFalse(self.writes("place"))

    def test_final_bed_stance_change_prevents_write(self):
        self.world.status["pos"] = [-.5, 64, -.5]
        self.world.counts["white_bed"] = 1
        original = self.world.get_status
        reads = 0
        def status(**kwargs):
            nonlocal reads
            reads += 1
            if reads == 6:  # Last prewrite status, after the second set of cell reads.
                self.world.status["pos"] = [.5, 64, .5]
            return original(**kwargs)
        self.world.get_status = status
        result = self.act("place_bed", pos=[-1, 64, 0], item="white_bed")
        self.assertEqual(result.error_code, "needs_position")
        self.assertFalse(self.writes("place"))

    def test_new_daily_postwrite_death_and_business_interruption_are_unknown(self):
        from skills.doors import DoorSkills
        for failure in ("dead", "interrupted", "cancelled"):
            world = DailyWorld()
            world.status["pos"] = [.5, 64, -1.5]
            for y, half in ((64, "lower"), (65, "upper")):
                world.block([0, y, 0], "oak_door", properties={"half": half, "facing": "north", "hinge": "left", "open": False})
            event = threading.Event()
            def interact():
                if failure == "dead":
                    world.status.update(alive=False, health=0)
                elif failure == "cancelled":
                    event.set()
                return envelope({"interacted": True, **({"interrupted_by": "hurt"} if failure == "interrupted" else {})})
            world.effects["interact"] = interact
            result = DoorSkills(world).execute("set_door", {"pos": [0, 64, 0], "open": True}, event)
            with self.subTest(failure=failure):
                self.assertEqual(result.error_code, "action_unconfirmed")
                self.assertEqual(result.data["action_state"], "unknown")
                self.assertTrue(result.data["executed"])
                self.assertEqual(sum(call[0] == "interact" for call in world.calls), 1)

    def test_door_needs_matching_upper_and_lower_halves(self):
        self.world.counts["oak_door"] = 1
        result = self.act("place_door", pos=self.pos, item="oak_door")
        self.assertTrue(result.success, result)
        self.world.block(self.pos, "air")
        self.world.block([2, 65, 0], "air")
        self.world.counts["oak_door"] = 1
        def partial():
            self.world.block(self.pos, "oak_door", properties={"half": "lower", "facing": "south"})
            self.world.counts["oak_door"] = 0
            return envelope({"placed": "oak_door"})
        self.world.effects["place"] = partial
        self.assertEqual(self.act("place_door", pos=self.pos, item="oak_door").error_code, "place_unverified")

    def test_cancel_before_action_never_writes(self):
        event = threading.Event()
        event.set()
        result = self.skills.execute("place_bed", {"pos": self.pos, "item": "white_bed"}, event)
        self.assertEqual(result.error_code, "cancelled")
        self.assertFalse(result.data["executed"])
        self.assertFalse(self.writes("place"))

    def test_cancel_after_write_keeps_unknown_without_replay(self):
        self.world.block(self.pos, "chest")
        self.world.counts["oak_log"] = 1
        event = threading.Event()
        def put(ops):
            event.set()
            return envelope({"done": [{"put": "oak_log", "count": 1}]})
        self.world.effects["window"] = put
        result = self.skills.execute("store_items", {"pos": self.pos, "item": "oak_log", "count": 1}, event)
        self.assertEqual(result.error_code, "action_unconfirmed")
        self.assertEqual(result.data["action_state"], "unknown")
        self.assertTrue(result.data["executed"])
        self.assertEqual(len(self.writes("window_act")), 1)

    def test_latest_status_refuses_hazard_before_write(self):
        self.world.block(self.pos, "chest")
        self.world.counts["oak_log"] = 1
        self.world.status["nearest_hostile"] = {"dist": 3, "name": "zombie"}
        result = self.act("store_items", pos=self.pos, item="oak_log", count=1)
        self.assertEqual(result.error_code, "unsafe")
        self.assertFalse(self.writes("window_act"))

    def test_invalid_args_and_unsupported_container_never_write(self):
        self.assertEqual(self.act("smelt_start", pos=self.pos, input_item="raw_iron", count=True, fuel_item="coal", fuel_count=1).error_code, "bad_args")
        self.assertEqual(self.act("inspect_container", pos=self.pos, extra=True).error_code, "bad_args")
        self.world.block(self.pos, "trapped_chest")
        self.assertEqual(self.act("inspect_container", pos=self.pos).error_code, "unsupported_container")


if __name__ == "__main__":
    unittest.main()
