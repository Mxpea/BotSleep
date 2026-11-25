"""Offline progression advice must be evidence-bound and capability-bound."""

import copy
import json
import unittest

from core.survival_playbook import KNOWN_TOOLS, MAX_BYTES, STAGES, SurvivalPlaybook


def world(counts=None, dimension="overworld"):
    return {"fresh": True, "inventory_fresh": True,
            "status": {"ready": True, "alive": True, "gamemode": "survival", "dimension": dimension,
                       "pos": [.5, 64, .5], "health": 20, "food": 20, "on_ground": True, "is_day": True},
            "counts": {"bread": 8, "stone_pickaxe": 1, **(counts or {})},
            "blocks": [{"name": "crafting_table", "pos": [2, 64, 0]},
                       {"name": "furnace", "pos": [2, 64, 1]}], "entities": []}


def advanced(goal="survival"):
    return world({"shield": 1, "water_bucket": 1, "iron_pickaxe": 1,
                  "bread": 16, "cobblestone": 32, "golden_boots": 1, "flint_and_steel": 1})


def verified(**extra):
    return {"fresh": True, "verified": True, "source": "api_verified", "dimension": "overworld", **extra}


class SurvivalPlaybookTests(unittest.TestCase):
    def setUp(self):
        self.book = SurvivalPlaybook(KNOWN_TOOLS)

    def assert_bounded(self, result):
        self.assertLessEqual(len(json.dumps(result, allow_nan=False).encode()), MAX_BYTES)
        self.assertLessEqual(len(result["next_actions"]), 3)
        self.assertTrue(set(result["bindings"]) <= KNOWN_TOOLS)

    def test_shield_precedes_bucket_and_pick_with_exact_ingredients(self):
        obs = world({"iron_ingot": 7, "oak_planks": 6, "stick": 2})
        first = self.book.build(obs)
        self.assertEqual(first["stage"], "iron_defense")
        self.assertEqual(first["next_actions"][0]["args"], {"item": "shield", "count": 1})
        obs["counts"].update(shield=1, iron_ingot=6)
        self.assertEqual(self.book.build(obs)["next_actions"][0]["args"]["item"], "bucket")
        obs["counts"].update(bucket=1, iron_ingot=3)
        self.assertEqual(self.book.build(obs)["next_actions"][0]["args"]["item"], "iron_pickaxe")
        self.assert_bounded(first)

    def test_no_iron_does_not_invent_craft_or_repeat_empty_furnace_inspection(self):
        obs = world({"oak_planks": 6})
        obs["furnaces"] = [verified(pos=[2, 64, 1], input=None, fuel=None, output=None, burning=False)]
        obs["blocks"].append({"name": "iron_ore", "pos": [3, 64, 1], "can_see": True})
        result = self.book.build(obs)
        self.assertEqual(result["next_actions"][0]["name"], "gather_resource")
        self.assertEqual(result["missing"], ["shield", "iron_ingot:1"])

    def test_fresh_offhand_and_equipped_gold_are_not_recrafted(self):
        obs = advanced()
        obs["counts"].update(shield=0, golden_boots=0)
        obs["status"]["offhand"] = {"name": "shield", "count": 1}
        obs["armor"] = {"feet": {"name": "golden_boots", "count": 1}}
        result = SurvivalPlaybook(KNOWN_TOOLS, goal="speedrun").build(obs)
        self.assertEqual(result["stage"], "nether_portal")
        self.assertNotIn("gold_armor", result["missing"])

    def test_furnace_start_and_collection_are_different_current_stages(self):
        obs = world({"raw_iron": 2, "coal": 1, "oak_planks": 6})
        obs["furnaces"] = [verified(pos=[2, 64, 1], input=None, fuel=None, output=None, burning=False)]
        self.assertEqual(self.book.build(obs)["next_actions"][0]["name"], "smelt_start")
        obs["furnaces"][0].update(input={"name": "raw_iron", "count": 2}, burning=True)
        self.assertEqual(self.book.build(obs)["next_actions"][0]["name"], "inspect_container")
        obs["furnaces"][0].update(output={"name": "iron_ingot", "count": 1})
        result = self.book.build(obs)
        self.assertEqual(result["next_actions"][0]["name"], "smelt_collect")
        self.assertNotEqual(result["state"], "complete")

    def test_missing_workstation_does_not_craft_shield_in_inventory(self):
        obs = world({"iron_ingot": 1, "oak_planks": 6})
        obs["blocks"] = [{"name": "furnace", "pos": [2, 64, 1]}]
        self.assertEqual(self.book.build(obs)["next_actions"][0]["args"]["item"], "crafting_table")

    def test_smelting_does_not_load_more_input_than_owned_fuel_can_process(self):
        for fuel, quantity, expected_input, expected_fuel in (("coal", 1, 8, 1), ("oak_planks", 1, 1, 1), ("stick", 2, 1, 2)):
            with self.subTest(fuel=fuel):
                obs = world({"raw_iron": 8, fuel: quantity})
                obs["furnaces"] = [verified(pos=[2, 64, 1], input=None, fuel=None, output=None, burning=False)]
                result = self.book.build(obs)
                self.assertEqual(result["next_actions"][0]["name"], "smelt_start")
                self.assertEqual(result["next_actions"][0]["args"]["count"], expected_input)
                self.assertEqual(result["next_actions"][0]["args"]["fuel_count"], expected_fuel)

    def test_empty_inventory_can_prepare_tools_from_visible_wood(self):
        obs = world()
        obs["counts"] = {}
        obs["blocks"] = [{"name": "oak_log", "pos": [3, 64, 0], "can_see": True}]
        result = self.book.build(obs)
        self.assertEqual(result["stage"], "opening")
        self.assertEqual(result["next_actions"][0]["name"], "gather_block")

    def test_food_hunt_uses_only_observed_supported_entities(self):
        for name in ("pig", "cow", "sheep", "chicken"):
            obs = world({"bread": 0})
            obs["entities"] = [{"name": name, "id": 7, "pos": [3, 64, 0]}]
            result = self.book.build(obs)
            self.assertEqual(result["next_actions"][0]["args"], {"entity_id": 7, "expected_name": name})
            self.assertIn("cannot verify age or ownership", result["next_actions"][0]["reason"])
            self.assertNotIn("hunt_with_lava", result["bindings"])

    def test_low_health_without_food_blocks_hunting(self):
        obs = world({"bread": 0})
        obs["status"]["health"] = 4
        obs["entities"] = [{"name": "cow", "id": 7, "pos": [2, 64, 0]}]
        result = self.book.build(obs)
        self.assertEqual(result["stage"], "safety")
        self.assertEqual(result["next_actions"], [])

    def test_known_food_precedes_low_health_production(self):
        obs = world()
        obs["status"].update(health=6, food=12)
        self.assertEqual(self.book.build(obs)["next_actions"][0]["name"], "eat_food")

    def test_near_hostile_never_generates_production_or_unsafe_lava(self):
        for name in ("skeleton", "creeper", "zombie"):
            obs = advanced()
            obs["entities"] = [{"name": name, "id": 1, "pos": [2, 64, 1]}]
            result = self.book.build(obs)
            self.assertEqual(result["stage"], "safety")
            self.assertEqual(result["next_actions"], [])

    def test_unknown_unready_dead_partial_and_unsupported_version_fail_closed(self):
        for update in ({"fresh": False}, {"inventory_fresh": False}, {"observation_scope": "safety"}):
            obs = advanced()
            obs.update(update)
            self.assertEqual(self.book.build(obs)["next_actions"], [])
        for update in ({"alive": False}, {"ready": False}, {"pos": None}, {"on_ground": False}):
            obs = advanced()
            obs["status"].update(update)
            self.assertEqual(self.book.build(obs)["state"], "blocked")
        self.assertEqual(SurvivalPlaybook(KNOWN_TOOLS, java_version="1.16.1").build(advanced())["state"], "blocked")

    def test_uncertain_writes_are_not_replayed_even_when_inventory_has_resources(self):
        for context in ({"pending": {"name": "craft_item"}}, {"action_state": "unknown"},
                        {"last_result": {"data": {"action_state": "unknown"}}},
                        {"last_result": {"error_code": "dig_unconfirmed"}}):
            result = self.book.build(advanced(), context)
            self.assertEqual(result["stage"], "recovery")
            self.assertEqual(result["next_actions"], [])

    def test_death_recovery_needs_current_item_entity(self):
        obs = world()
        obs["entities"] = [{"id": 9, "name": "item", "item": "iron_ingotx3", "pos": [3, 64, 1]}]
        result = self.book.build(obs, {"recovering": True})
        self.assertEqual(result["next_actions"][0]["args"], {"entity_id": 9, "expected_item": "iron_ingot"})
        obs["entities"] = []
        self.assertEqual(self.book.build(obs, {"recovering": True, "memory": {"death_pos": [3, 64, 1]}})["next_actions"], [])

    def test_no_implicit_capability_or_fake_tool_name(self):
        obs = world({"iron_ingot": 1, "oak_planks": 6})
        result = SurvivalPlaybook(["cast_nether_portal", "calculate_stronghold", "invented_tool"]).build(obs)
        self.assertEqual(result["next_actions"], [])
        self.assertEqual(result["state"], "blocked")

    def test_shelter_memory_and_open_door_are_not_maintenance_completion(self):
        obs = advanced()
        result = self.book.build(obs, {"memory": {"places": [verified(name="shelter")]}})
        self.assertEqual(result["stage"], "home_farm")
        obs["daily_evidence"] = {key: verified() for key in ("shelter", "bed", "storage", "food_production")}
        obs["daily_evidence"]["shelter"].update(structure_complete=True, shelter_verified=False)
        obs["daily_evidence"]["food_production"]["renewable"] = True
        self.assertIn("shelter", self.book.build(obs)["missing"])

    def test_current_facilities_allow_maintenance_not_permanent_survival_complete(self):
        obs = advanced()
        obs["daily_evidence"] = {key: verified() for key in ("shelter", "bed", "storage", "food_production")}
        obs["daily_evidence"]["shelter"]["shelter_verified"] = True
        obs["daily_evidence"]["food_production"]["renewable"] = True
        result = self.book.build(obs)
        self.assertEqual((result["stage"], result["state"]), ("maintenance", "ready"))
        obs["daily_evidence"]["storage"]["dimension"] = "the_nether"
        self.assertEqual(self.book.build(obs)["stage"], "home_farm")

    def test_speedrun_water_requires_owned_empty_bucket_and_source_evidence(self):
        book = SurvivalPlaybook(KNOWN_TOOLS, goal="speedrun")
        obs = advanced()
        obs["counts"].update(water_bucket=0, bucket=1)
        obs["blocks"].append({"name": "water", "pos": [2, 64, 2], "can_see": True, "properties": {"level": "0"}})
        result = book.build(obs)
        self.assertEqual(result["stage"], "nether_preparation")
        self.assertEqual(result["next_actions"][0]["name"], "collect_liquid")
        obs["blocks"][-1]["properties"]["level"] = "1"
        self.assertEqual(book.build(obs)["next_actions"], [])

    def test_portal_scripts_and_lower_level_api_do_not_become_bound_speedrun_actions(self):
        book = SurvivalPlaybook([*KNOWN_TOOLS, "cast_nether_portal"], goal="speedrun")
        result = book.build(advanced())
        self.assertEqual(result["stage"], "nether_portal")
        self.assertEqual(result["next_actions"], [])
        self.assertIn("automatic_lava_portal_site_and_casting", result["unsupported"])

    def test_nether_requires_fresh_return_and_never_uses_overworld_tools(self):
        obs = advanced()
        obs["status"]["dimension"] = "the_nether"
        obs["counts"].update(blaze_rod=7, ender_pearl=14)
        obs["progression_evidence"] = {"return_portal": verified()}
        result = SurvivalPlaybook(KNOWN_TOOLS, goal="speedrun").build(obs)
        self.assertEqual(result["stage"], "nether_resources")
        self.assertIn("verified_return_portal", result["missing"])
        self.assertEqual(result["next_actions"], [])
        self.assertNotIn("blaze_powder_equivalent:14", result["missing"])

    def test_eyes_can_be_crafted_but_calculated_stronghold_is_not_portal_evidence(self):
        book = SurvivalPlaybook(KNOWN_TOOLS, goal="speedrun")
        obs = advanced()
        obs["counts"].update(blaze_powder=14, ender_pearl=14)
        self.assertEqual(book.build(obs)["next_actions"][0]["args"], {"item": "ender_eye", "count": 14})
        obs["counts"].update(blaze_powder=0, ender_pearl=0, ender_eye=14)
        result = book.build(obs, {"stronghold": {"pos": [100, 64, 200], "probability": .99}})
        self.assertEqual(result["stage"], "stronghold")
        self.assertEqual(result["next_actions"], [])

    def test_end_portal_requires_fresh_same_dimension_checked_frames(self):
        book = SurvivalPlaybook(KNOWN_TOOLS, goal="speedrun")
        obs = advanced()
        obs["counts"]["ender_eye"] = 14
        obs["progression_evidence"] = {"end_portal": verified(empty_frames=12)}
        self.assertEqual(book.build(obs)["stage"], "end_preparation")
        obs["progression_evidence"]["end_portal"]["fresh"] = False
        self.assertEqual(book.build(obs)["stage"], "stronghold")

    def test_dragon_success_requires_explicit_server_or_skill_evidence(self):
        obs = advanced()
        obs["status"]["dimension"] = "the_end"
        book = SurvivalPlaybook(KNOWN_TOOLS, goal="speedrun")
        self.assertEqual(book.build(obs, {"completed_stages": ["dragon"]})["stage"], "dragon")
        obs["progression_evidence"] = {"dragon_defeated": verified(dimension="the_end", source="server_advancement")}
        result = book.build(obs)
        self.assertEqual(result["stage"], "post_dragon")
        self.assertNotEqual(result["state"], "complete")

    def test_build_is_deterministic_does_not_mutate_and_stays_bounded(self):
        for dimension in ("overworld", "the_nether", "the_end"):
            for goal in ("survival", "speedrun"):
                obs = advanced()
                obs["status"]["dimension"] = dimension
                original = copy.deepcopy(obs)
                book = SurvivalPlaybook(KNOWN_TOOLS, goal=goal)
                first = book.build(obs)
                self.assertEqual(first, book.build(obs))
                self.assertEqual(obs, original)
                self.assert_bounded(first)
        self.assertTrue({"opening", "home_farm", "nether_portal", "nether_resources", "stronghold", "dragon", "recovery"} <= set(STAGES))


if __name__ == "__main__":
    unittest.main()
