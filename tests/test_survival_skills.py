"""Survival skill state verification and wrapper tests; never contact a game."""

import copy
import math
import threading
import unittest
from unittest.mock import Mock, patch

from core.client import APIError, MinecraftAgentClient
from skills.survival import SurvivalSkills


def envelope(result):
    return {"ok": True, "result": copy.deepcopy(result)}


class World:
    def __init__(self):
        self.status = {"ready": True, "alive": True, "health": 20, "food": 10, "dimension": "overworld",
                       "pos": [0.5, 64, 0.5], "busy": None, "on_ground": True, "is_sleeping": False}
        self.counts = {}
        self.blocks = {}
        self.calls = []
        self.ground = "stone"
        self.move_effect = None
        self.dig_effect = None
        self.craft_effect = None
        self.place_effect = None
        self.eat_effect = None

    def get_status(self):
        self.calls.append(("status",))
        return envelope(self.status)

    def inventory(self):
        self.calls.append(("inventory",))
        return envelope({"items": [{"name": name, "count": count, "durability": [20, 100]}
                                   for name, count in self.counts.items() if count > 0]})

    def get_block(self, pos):
        key = tuple(math.floor(value) for value in pos)
        self.calls.append(("get_block", key))
        block = self.blocks.get(key, {"name": self.ground if key[1] < 64 else "air"})
        return envelope({"pos": list(key), "diggable": True, "properties": {}, **block})

    def set_block(self, pos, name, **extra):
        self.blocks[tuple(pos)] = {"name": name, **extra}

    def move_to(self, x, y, z, **kwargs):
        self.calls.append(("move_to", [x, y, z], kwargs))
        if self.move_effect:
            return self.move_effect([x, y, z], kwargs)
        self.status["pos"] = [x, y, z]
        return envelope({"pos": self.status["pos"]})

    def dig(self, pos, collect=True):
        self.calls.append(("dig", pos, collect))
        if self.dig_effect:
            return self.dig_effect(pos)
        name = self.blocks[tuple(pos)]["name"]
        drop = {"stone": "cobblestone", "coal_ore": "coal", "deepslate_coal_ore": "coal"}.get(name, name)
        self.set_block(pos, "air")
        self.counts[drop] = self.counts.get(drop, 0) + 1
        return envelope({"dug": name, "gained": {drop: 1}})

    def craft(self, item, count=1):
        self.calls.append(("craft", item, count))
        if self.craft_effect:
            return self.craft_effect(item, count)
        self.counts[item] = self.counts.get(item, 0) + count
        return envelope({"crafted": item, "gained": count})

    def place_block(self, x, y, z, item=None):
        self.calls.append(("place", item, [x, y, z]))
        if self.place_effect:
            return self.place_effect(item, [x, y, z])
        self.set_block([x, y, z], item)
        self.counts[item] -= 1
        return envelope({"placed": item})

    def use_item(self, item):
        self.calls.append(("use_item", item))
        if self.eat_effect:
            return self.eat_effect(item)
        self.counts[item] -= 1
        self.status["food"] = min(20, self.status["food"] + 5)
        return envelope({"used": {"name": item, "count": 1}})

    def stop(self):
        self.calls.append(("stop",))
        return envelope({"stopped": True})

    def writes(self):
        return [call for call in self.calls if call[0] in {"move_to", "dig", "craft", "place", "use_item", "stop"}]


class SurvivalTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.skills = SurvivalSkills(self.world)
        self.target = [2, 64, 0]

    def gather(self, name="oak_log"):
        self.world.set_block(self.target, name)
        return self.skills.execute("gather_block", {"pos": self.target, "expected_name": name})

    def test_log_gather_requires_removed_block_and_real_inventory_gain(self):
        result = self.gather()
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], {"oak_log": 1})
        self.assertEqual([call[0] for call in self.world.writes()], ["dig"])

    def test_stone_gather_requires_a_pickaxe_and_checks_actual_drop(self):
        result = self.gather("stone")
        self.assertEqual(result.error_code, "missing_tool")
        self.assertFalse(self.world.writes())
        self.world.counts["wooden_pickaxe"] = 1
        result = self.gather("stone")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], {"cobblestone": 1})

    def test_approach_accounts_for_goal_near_grid_and_actual_feet_position(self):
        # GoalNear checks integer path nodes, but feet are at the cell center.
        # A radius-3 endpoint on this side is sqrt(3.5**2 + 0.5**2) away.
        target = [-4, 64, 4]
        for action in ("gather_block", "place_item"):
            with self.subTest(action=action):
                world = World()
                world.status["pos"] = [4.4, 64, 3.5]
                world.counts = {"wooden_pickaxe": 1, "crafting_table": 1}

                def move_to(pos, kwargs):
                    node = [math.floor(pos[0]) + kwargs["range"], math.floor(pos[1]), math.floor(pos[2])]
                    world.status["pos"] = [node[0] + 0.5, node[1], node[2] + 0.5]
                    return envelope({"pos": world.status["pos"]})

                world.move_effect = move_to
                if action == "gather_block":
                    world.set_block(target, "stone")
                    args = {"pos": target, "expected_name": "stone"}
                else:
                    args = {"pos": target, "item": "crafting_table"}
                result = SurvivalSkills(world).execute(action, args)
                self.assertTrue(result.success, result)
                self.assertLessEqual(math.dist(world.status["pos"], target), 3.5)
                self.assertEqual([call[2]["range"] for call in world.calls if call[0] == "move_to"], [2])

    def test_approach_still_refuses_dig_when_actual_position_is_too_far(self):
        self.target = [-4, 64, 4]
        self.world.status["pos"] = [4.4, 64, 3.5]
        self.world.counts["wooden_pickaxe"] = 1

        def move_to(pos, kwargs):
            self.world.status["pos"] = [pos[0] + 3.5, pos[1], pos[2] + 0.5]
            return envelope({"pos": self.world.status["pos"]})

        self.world.move_effect = move_to
        result = self.gather("stone")
        self.assertEqual(result.error_code, "out_of_reach")
        self.assertEqual([call[0] for call in self.world.writes()], ["move_to", "stop"])

    def test_reachable_high_log_does_not_force_a_climb_before_digging(self):
        self.world.status["pos"] = [4.3, 109, -5.5]
        target = [6, 112, -4]
        self.world.set_block(target, "oak_log", can_dig=True, can_see=True)
        self.world.move_effect = lambda *_: self.fail("Reachable log must not force pathfinding")
        result = self.skills.execute("gather_block", {"pos": target, "expected_name": "oak_log"})
        self.assertTrue(result.success, result)
        self.assertEqual([call[0] for call in self.world.writes()], ["dig"])
        self.assertEqual(result.data["gained"], {"oak_log": 1})

    def test_occluded_high_log_does_not_use_reach_to_bypass_approach(self):
        self.world.status["pos"] = [4.3, 109, -5.5]
        target = [6, 112, -4]
        self.world.set_block(target, "oak_log", can_dig=True, can_see=False)
        result = self.skills.execute("gather_block", {"pos": target, "expected_name": "oak_log"})
        self.assertEqual(result.error_code, "target_occluded")
        self.assertFalse(result.data["executed"])
        self.assertFalse(self.world.writes())

    def test_failed_approach_keeps_api_path_diagnostics_for_model_feedback(self):
        target = [8, 64, 0]
        self.world.set_block(target, "oak_log", can_dig=False, can_see=True)
        def blocked(*_):
            raise APIError("path_not_found", "No supported route", details={"last_path_status": "noPath"})
        self.world.move_effect = blocked
        result = self.skills.execute("gather_block", {"pos": target, "expected_name": "oak_log"})
        self.assertEqual(result.error_code, "path_not_found")
        self.assertEqual(result.data["api_details"]["last_path_status"], "noPath")
        self.assertEqual([call[0] for call in self.world.writes()], ["move_to", "stop"])

    def test_dig_without_pickup_preserves_collection_failure_evidence(self):
        def partial(pos):
            self.world.set_block(pos, "air")
            return envelope({"dug": "oak_log", "collection": {
                "completed": False, "error": {"code": "stalled"}}})
        self.world.dig_effect = partial
        result = self.gather()
        self.assertEqual(result.error_code, "gather_unverified")
        self.assertTrue(result.data["block_removed"])
        self.assertEqual(result.data["collection"]["error"]["code"], "stalled")

    def test_copper_pickaxe_supported_by_current_minecraft_data_is_usable(self):
        self.world.counts["copper_pickaxe"] = 1
        result = self.gather("coal_ore")
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["gained"], {"coal": 1})

    def test_gained_response_without_actual_inventory_change_is_not_success(self):
        def dig(pos):
            self.world.set_block(pos, "air")
            return envelope({"gained": {"oak_log": 9}})
        self.world.dig_effect = dig
        result = self.gather()
        self.assertEqual(result.error_code, "gather_unverified")
        self.assertEqual(self.world.writes()[-1][0], "stop")

    def test_inventory_gain_without_block_removal_is_not_success(self):
        def dig(pos):
            self.world.counts["oak_log"] = 1
            return envelope({"gained": {"oak_log": 1}})
        self.world.dig_effect = dig
        self.assertEqual(self.gather().error_code, "gather_unverified")

    def test_unsupported_and_hazardous_blocks_are_never_dug(self):
        for name in ("water", "lava", "fire", "diamond_ore", "chest", "wheat"):
            with self.subTest(name=name):
                self.world.calls.clear()
                self.assertEqual(self.gather(name).error_code, "unsupported_block")
                self.assertFalse(self.world.writes())

    def test_gather_rejects_dangerous_dimensions_and_status(self):
        for update in ({"dimension": "the_nether"}, {"dimension": "the_end"}, {"in_water": True}, {"on_fire": True}):
            with self.subTest(update=update):
                original = copy.deepcopy(self.world.status)
                self.world.status.update(update)
                self.world.calls.clear()
                self.assertFalse(self.gather().success)
                self.assertFalse(self.world.writes())
                self.world.status = original

    def test_gather_rejects_same_vertical_column(self):
        for pos in ([0, 63, 0], [0, 66, 0]):
            self.world.set_block(pos, "oak_log")
            result = self.skills.execute("gather_block", {"pos": pos, "expected_name": "oak_log"})
            self.assertEqual(result.error_code, "unsafe")
        self.assertFalse(self.world.writes())

    def test_gather_rejects_adjacent_water_and_falling_sand(self):
        for block in ({"name": "water"}, {"name": "sand"}, {"name": "oak_log", "properties": {"waterlogged": True}}):
            with self.subTest(block=block):
                self.world.blocks[(2, 65, 0)] = block
                result = self.gather()
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(self.world.writes())

    def test_gather_expected_name_prevents_stale_target(self):
        self.world.set_block(self.target, "chest")
        result = self.skills.execute("gather_block", {"pos": self.target, "expected_name": "oak_log"})
        self.assertEqual(result.error_code, "target_changed")
        self.assertFalse(self.world.writes())

    def test_unknown_dig_completion_is_not_retried(self):
        def timeout(pos):
            raise APIError("request_timeout", "completion unknown")
        self.world.dig_effect = timeout
        result = self.gather()
        self.assertEqual(result.error_code, "request_timeout")
        self.assertEqual([call[0] for call in self.world.writes()], ["dig", "stop"])

    def test_busy_rejection_does_not_stop_someone_elses_action(self):
        def busy(pos):
            raise APIError("busy", "another action started")
        self.world.dig_effect = busy
        self.assertEqual(self.gather().error_code, "busy")
        self.assertEqual([call[0] for call in self.world.writes()], ["dig"])

    def test_invalid_observation_after_action_is_an_unknown_outcome(self):
        def craft(item, count):
            self.world.counts[item] = count
            self.world.status["pos"] = None
            return envelope({"gained": count})
        self.world.craft_effect = craft
        result = self.skills.execute("craft_item", {"item": "stick"})
        self.assertEqual(result.error_code, "invalid_response")
        self.assertEqual([call[0] for call in self.world.writes()], ["craft", "stop"])

    def test_craft_uses_increment_not_preexisting_inventory(self):
        self.world.counts["stick"] = 10
        result = self.skills.execute("craft_item", {"item": "stick", "count": 4})
        self.assertTrue(result.success, result)
        self.assertEqual((result.data["before"], result.data["after"], result.data["gained"]), (10, 14, 4))

    def test_partial_craft_has_explicit_partial_result(self):
        def craft(item, count):
            self.world.counts[item] = 2
            return envelope({"gained": count})
        self.world.craft_effect = craft
        result = self.skills.execute("craft_item", {"item": "stick", "count": 4})
        self.assertFalse(result.success)
        self.assertEqual((result.error_code, result.data["gained"]), ("partial_completion", 2))

    def test_server_failure_after_partial_craft_reports_inventory_gain(self):
        def craft(item, count):
            self.world.counts[item] = 2
            raise APIError("missing_ingredients", "ran out")
        self.world.craft_effect = craft
        result = self.skills.execute("craft_item", {"item": "stick", "count": 4})
        self.assertEqual(result.error_code, "partial_completion")
        self.assertEqual(result.data["cause"], "missing_ingredients")

    def test_no_crafted_inventory_is_not_success(self):
        self.world.craft_effect = lambda item, count: envelope({"gained": count})
        self.assertEqual(self.skills.execute("craft_item", {"item": "stick"}).error_code, "craft_unverified")

    def test_place_checks_world_and_consumption(self):
        self.world.counts["crafting_table"] = 1
        result = self.skills.execute("place_item", {"item": "crafting_table", "pos": self.target})
        self.assertTrue(result.success, result)
        self.assertEqual(result.data["used"], 1)

    def test_place_rejects_occupied_unsupported_or_self_cell_without_moving(self):
        self.world.counts["dirt"] = 3
        for pos, block in (([10, 64, 0], "chest"), ([10, 63, 0], "lava"), ([0, 64, 0], "air")):
            with self.subTest(pos=pos):
                self.world.blocks.clear()
                self.world.set_block(pos, block)
                target = [10, 64, 0] if pos[0] == 10 else pos
                result = self.skills.execute("place_item", {"item": "dirt", "pos": target})
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(self.world.writes())

    def test_place_does_not_trust_success_without_consumption(self):
        self.world.counts["dirt"] = 1
        def place(item, pos):
            self.world.set_block(pos, item)
            return envelope({"placed": item})
        self.world.place_effect = place
        self.assertEqual(self.skills.execute("place_item", {"item": "dirt", "pos": self.target}).error_code, "place_unverified")

    def test_dangerous_place_items_are_rejected(self):
        for item in ("tnt", "lava_bucket", "red_bed", "respawn_anchor"):
            result = self.skills.execute("place_item", {"item": item, "pos": self.target})
            self.assertEqual(result.error_code, "unsupported_item")
        self.assertFalse(self.world.writes())

    def test_eat_requires_food_gain_and_item_consumption(self):
        self.world.counts["bread"] = 1
        result = self.skills.execute("eat_food", {"item": "bread"})
        self.assertTrue(result.success, result)
        self.assertEqual((result.data["food_before"], result.data["food_after"]), (10, 15))

    def test_unsafe_food_full_food_and_no_food_are_rejected(self):
        for item in ("rotten_flesh", "pufferfish", "chorus_fruit", "suspicious_stew", "poisonous_potato"):
            self.assertEqual(self.skills.execute("eat_food", {"item": item}).error_code, "unsafe_food")
        self.assertEqual(self.skills.execute("eat_food", {"item": "bread"}).error_code, "missing_item")
        self.world.status["food"] = 20
        self.assertEqual(self.skills.execute("eat_food", {"item": "bread"}).error_code, "not_hungry")
        self.assertFalse(self.world.writes())

    def test_food_consumption_without_food_gain_is_not_success(self):
        self.world.counts["bread"] = 1
        def eat(item):
            self.world.counts[item] -= 1
            return envelope({"food": 20})
        self.world.eat_effect = eat
        self.assertEqual(self.skills.execute("eat_food", {"item": "bread"}).error_code, "eat_unverified")

    def test_movement_is_verified_from_actual_position(self):
        result = self.skills.execute("move_to", {"pos": [5, 64, 0]})
        self.assertTrue(result.success, result)
        self.world.status["pos"] = [0.5, 64, 0.5]
        self.world.move_effect = lambda pos, kwargs: envelope({"pos": pos})
        result = self.skills.execute("move_to", {"pos": [5, 64, 0]})
        self.assertEqual(result.error_code, "move_unverified")

    def test_already_near_goal_reports_no_movement_instead_of_recovery(self):
        self.world.status["pos"] = [4.3, 109, -5.5]
        result = self.skills.execute("move_to", {"pos": [4, 108, -6], "range": 2})
        self.assertTrue(result.success)
        self.assertTrue(result.data["already_in_range"])
        self.assertFalse(result.data["executed"])
        self.assertEqual(result.data["moved"], 0)
        self.assertIn("no movement", result.message)
        self.assertFalse(self.world.writes())

    def test_movement_partial_interruption_and_dimension_change_fail(self):
        self.world.move_effect = lambda pos, kwargs: envelope({"interrupted_by": "hurt", "pos": pos})
        self.assertEqual(self.skills.execute("move_to", {"pos": [5, 64, 0]}).error_code, "interrupted")
        def change_dimension(pos, kwargs):
            self.world.status.update(pos=pos, dimension="the_nether")
            return envelope({"pos": pos})
        self.world.move_effect = change_dimension
        self.assertEqual(self.skills.execute("move_to", {"pos": [5, 64, 0]}).error_code, "dimension_changed")

    def test_retreat_checks_safe_corridor_and_increases_threat_distance(self):
        result = self.skills.execute("retreat_from", {"pos": [-3, 64, 0.5]})
        self.assertTrue(result.success, result)
        self.assertGreater(result.data["distance_after"], result.data["distance_before"])
        self.assertEqual([call[0] for call in self.world.writes()], ["move_to"])

    def test_retreat_refuses_void_liquid_and_no_direction(self):
        for ground in ("air", "lava", "sand"):
            with self.subTest(ground=ground):
                self.world.ground = ground
                result = self.skills.execute("retreat_from", {"pos": [-3, 64, 0.5]})
                self.assertEqual(result.error_code, "unsafe")
                self.assertFalse(self.world.writes())
        result = self.skills.execute("retreat_from", {"pos": self.world.status["pos"]})
        self.assertEqual(result.error_code, "unsafe")

    def test_cancellation_before_start_makes_no_api_calls(self):
        cancel = threading.Event()
        cancel.set()
        result = self.skills.execute("craft_item", {"item": "stick"}, cancel_event=cancel)
        self.assertEqual(result.error_code, "cancelled")
        self.assertFalse(self.world.calls)

    def test_cancellation_after_action_does_not_claim_success(self):
        cancel = threading.Event()
        def craft(item, count):
            cancel.set()
            self.world.counts[item] = count
            return envelope({"gained": count})
        self.world.craft_effect = craft
        result = self.skills.execute("craft_item", {"item": "stick"}, cancel_event=cancel)
        self.assertEqual(result.error_code, "cancelled")
        self.assertEqual([call[0] for call in self.world.writes()], ["craft", "stop"])

    def test_dead_not_ready_and_busy_never_start_actions(self):
        for update, code in (({"alive": False}, "dead"), ({"ready": False}, "not_ready"), ({"busy": "dig"}, "busy")):
            old = copy.deepcopy(self.world.status)
            self.world.status.update(update)
            result = self.skills.execute("craft_item", {"item": "stick"})
            self.assertEqual(result.error_code, code)
            self.assertFalse(self.world.writes())
            self.world.status = old

    def test_invalid_arguments_do_not_mutate_world(self):
        for name, args in (("unknown", {}), ("move_to", {"pos": [float("nan"), 64, 0]}),
                           ("craft_item", {"item": "stick", "count": True}),
                           ("craft_item", {"item": "../secret"}),
                           ("gather_block", {"pos": [1.5, 64, 0], "expected_name": "stone"}),
                           ("retreat_from", {"pos": [1, 64, 0], "distance": 99}),
                           ("move_to", {"pos": [1, 64, 0], "unknown": 1})):
            with self.subTest(name=name, args=args):
                self.assertFalse(self.skills.execute(name, args).success)
                self.assertFalse(self.world.writes())


class SurvivalWrapperTests(unittest.TestCase):
    @patch("core.client.requests.request")
    def test_dig_and_craft_wait_cover_fixed_backend_phases(self, request):
        request.return_value = Mock(status_code=200)
        request.return_value.json.return_value = envelope({})
        client = MinecraftAgentClient()
        client.dig([1, 64, 2])
        self.assertEqual(request.call_args.kwargs["json"], {"x": 1, "y": 64, "z": 2, "collect": True})
        self.assertGreaterEqual(request.call_args.kwargs["timeout"], 135)
        client.craft("stick", 4)
        self.assertEqual(request.call_args.kwargs["json"], {"item": "stick", "count": 4})
        self.assertGreaterEqual(request.call_args.kwargs["timeout"], 420)
        client.connect()
        self.assertTrue(request.call_args.args[1].endswith("/connect"))


if __name__ == "__main__":
    unittest.main()
