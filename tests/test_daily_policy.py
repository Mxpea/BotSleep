"""Pure policy regressions: no model, HTTP client, or game is used."""

import copy
import unittest
from unittest.mock import patch

from agent.survival_planner import Decision
from core.daily_policy import DailySafetyPolicy, FOOD_POINTS
from skills.daily_survival import DailySurvivalSkills


def observed(**status):
    return {"fresh": True, "status": {"alive": True, "ready": True, "health": 20, "food": 20,
            "gamemode": "survival", "dimension": "overworld", "pos": [.5, 64, .5],
            "on_ground": True, "is_day": True, "time_of_day": 5000, "thundering": False,
            "busy": None, **status}, "counts": {}, "blocks": [], "entities": [], "terrain": []}


def action(name, **args):
    return Decision(name, args, "Test action", "food" if name in {"eat_food", "harvest_crop"} else "test")


class DailyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = DailySafetyPolicy({"low_health_threshold": 8, "food_threshold": 16, "threat_radius": 8})
        self.baseline = Decision("wait", {"seconds": 5}, "Food acquisition not implemented", "food")

    def crop(self, observation, pos=None, name="carrots", mature=True, complete=False):
        pos = pos or [2, 64, 0]
        observation["blocks"].append({"name": name, "pos": pos, "mature": mature})
        if complete:
            x, y, z = pos
            for dx, dy, dz in ((-1, 0, 0), (1, 0, 0), (0, -1, 0), (0, 1, 0), (0, 0, -1), (0, 0, 1)):
                observation["blocks"].append({"name": "farmland" if dy == -1 else "air", "pos": [x + dx, y + dy, z + dz]})

    def shelter(self, observation, *, inside=False, path=None):
        observation["daily_evidence"] = {"shelter": {"pos": [3, 64, 0], "dimension": "overworld",
            "fresh": True, "verified": True, "geometry_complete": True, "inside": inside,
            "return_route": {"fresh": True, "verified": True, "dimension": "overworld",
                             "origin": list(observation["status"]["pos"]), "path": path or [[1, 64, 0], [2, 64, 0], [3, 64, 0]]}}}
        return observation["daily_evidence"]["shelter"]

    def gated_shelter(self, observation, *, opened=False, inside=False):
        observation["status"]["pos"] = [.5, 64, .5] if inside else [.5, 64, -2.5]
        row = self.shelter(observation, inside=inside, path=[[0, 64, -2], [0, 64, -1], [0, 64, 0]])
        row.update(pos=[0, 64, 0], geometry_complete=not opened, shelter_verified=not opened,
                   structure_complete=True, geometry={"structure_complete": True},
                   door={"pos": [0, 64, -2], "name": "oak_door", "facing": "north",
                         "frame_verified": True, "open": opened, "complete": not opened})
        row["return_route"]["verified"] = opened
        if not opened:
            row["return_route"]["blocked_door"] = {"pos": [0, 64, -2], "verified": True, "open": False}
            row["return_route"]["all_other_body_and_support_verified"] = True
        return row

    def test_critical_hunger_releases_only_local_food_planning(self):
        obs = observed(food=4)
        self.crop(obs)
        self.assertIsNone(self.policy.choose(obs, self.baseline))
        limits = self.policy.constraints(obs)
        self.assertEqual(limits["scenario"], "critical_hunger")
        self.assertTrue(limits["model_allowed"])
        self.assertEqual(limits["max_action_distance"], 4)
        self.assertIsNone(self.policy.validate(action("harvest_crop", pos=[2, 64, 0], expected_name="carrots"), obs))
        for name, args in (("hunt_animal", {"entity_id": 1, "expected_name": "cow"}),
                           ("gather_block", {"pos": [2, 64, 0], "expected_name": "oak_log"}),
                           ("gather_resource", {"pos": [2, 64, 0], "expected_name": "iron_ore"}),
                           ("move_to", {"pos": [2, 64, 0], "range": 1}),
                           ("smelt_start", {"pos": [2, 64, 0]}), ("place_bed", {"pos": [2, 64, 0]})):
            with self.subTest(name=name):
                self.assertEqual(self.policy.validate(action(name, **args), obs), "critical_hunger_nearby_food_only")

    def test_food_crop_must_be_current_mature_nearby_and_not_dangerous(self):
        for modification in ("far", "immature", "missing", "occluded", "hazard", "target_hostile"):
            obs = observed(food=3)
            self.crop(obs)
            pos = [2, 64, 0]
            if modification == "far":
                pos = [6, 64, 0]
                obs["blocks"][0]["pos"] = pos
            elif modification == "immature":
                obs["blocks"][0]["mature"] = False
            elif modification == "missing":
                obs["blocks"] = []
            elif modification == "occluded":
                obs["blocks"][0]["can_see"] = False
            elif modification == "hazard":
                obs["terrain"] = [{"name": "lava", "pos": [2, 63, 0]}]
            else:
                obs["entities"] = [{"type": "hostile", "name": "skeleton", "pos": [9, 64, 0]}]
            with self.subTest(modification=modification):
                self.assertIsNotNone(self.policy.validate(action("harvest_crop", pos=pos, expected_name="carrots"), obs))

    def test_fresh_current_container_food_is_allowed_but_old_or_nonfood_is_not(self):
        obs = observed(food=5)
        obs["containers"] = [{"name": "chest", "pos": [2, 64, 0], "fresh": True, "verified": True,
                              "dimension": "overworld", "items": [{"name": "bread", "count": 2}]}]
        take = action("take_items", pos=[2, 64, 0], item="bread", count=2)
        self.assertIsNone(self.policy.validate(take, obs))
        self.assertIsNotNone(self.policy.validate(action("take_items", pos=[2, 64, 0], item="coal", count=1), obs))
        obs["containers"][0]["fresh"] = False
        self.assertIsNotNone(self.policy.validate(take, obs))

    def test_nearby_inspection_is_allowed_without_claiming_food_exists(self):
        obs = observed(food=5)
        inspect = action("inspect_container", pos=[2, 64, 0])
        self.assertIsNotNone(self.policy.validate(inspect, obs))
        obs["blocks"] = [{"name": "chest", "pos": [2, 64, 0]}]
        self.assertIsNone(self.policy.validate(inspect, obs))

    def test_bread_needs_held_wheat_and_nearby_observed_table(self):
        obs = observed(food=5)
        obs["counts"] = {"wheat": 6}
        bread = action("craft_item", item="bread", count=2)
        self.assertIsNotNone(self.policy.validate(bread, obs))
        obs["blocks"] = [{"name": "crafting_table", "pos": [2, 64, 0]}]
        self.assertIsNone(self.policy.validate(bread, obs))
        self.assertIsNotNone(self.policy.validate(action("craft_item", item="stone_sword", count=1), obs))
        self.assertIsNotNone(self.policy.validate(action("craft_item", item="bread", count=3), obs))

    def test_smelting_collection_needs_actual_food_output(self):
        obs = observed(food=5)
        obs["furnaces"] = [{"pos": [2, 64, 0], "fresh": True, "verified": True, "dimension": "overworld",
                            "output": {"name": "cooked_beef", "count": 1}}]
        self.assertIsNone(self.policy.validate(action("smelt_collect", pos=[2, 64, 0], item="cooked_beef", count=1), obs))
        obs["furnaces"][0]["output"] = {"name": "iron_ingot", "count": 3}
        self.assertIsNotNone(self.policy.validate(action("smelt_collect", pos=[2, 64, 0], item="iron_ingot", count=1), obs))

    def test_existing_food_is_local_override_even_without_model(self):
        obs = observed(food=5)
        obs["counts"] = {"bread": 1}
        choice = self.policy.choose(obs, self.baseline)
        self.assertEqual(choice.name, "eat_food")
        self.assertEqual(choice.args, {"item": "bread"})
        self.assertFalse(self.policy.constraints(obs)["model_allowed"])
        self.assertEqual(self.policy.validate(action("harvest_crop", pos=[2, 64, 0], expected_name="carrots"), obs), "existing_food_has_priority")

    def test_approved_raw_foods_are_edible_only_when_registered(self):
        raw = {"beef": 3, "porkchop": 3, "rabbit": 3, "mutton": 2, "cod": 2, "salmon": 2, "potato": 1}
        with patch.object(DailySurvivalSkills, "FOODS", DailySurvivalSkills.FOODS | set(raw)):
            for item, points in raw.items():
                obs = observed(food=5)
                obs["counts"] = {item: 1}
                self.assertEqual(self.policy.choose(obs, self.baseline).args, {"item": item})
                self.assertEqual(FOOD_POINTS[item], points)
        for item in ("chicken", "rotten_flesh", "poisonous_potato", "spider_eye", "pufferfish", "chorus_fruit"):
            obs = observed(food=5)
            obs["counts"] = {item: 1}
            self.assertIsNotNone(self.policy.validate(action("eat_food", item=item), obs))

    def test_low_health_eats_or_waits_and_never_hunts(self):
        obs = observed(health=5, food=4)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertFalse(self.policy.constraints(obs)["model_allowed"])
        self.assertEqual(self.policy.validate(action("hunt_animal", entity_id=1, expected_name="cow"), obs), "low_health_food_only")
        obs["counts"] = {"bread": 1}
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "eat_food")

    def test_low_health_exception_needs_complete_close_immediate_food_evidence(self):
        obs = observed(health=6, food=4)
        self.crop(obs, pos=[1, 64, 0])
        decision = action("harvest_crop", pos=[1, 64, 0], expected_name="carrots")
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertIsNotNone(self.policy.validate(decision, obs))
        obs["blocks"] = []
        self.crop(obs, pos=[1, 64, 0], complete=True)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "harvest_crop")
        self.assertIsNone(self.policy.validate(decision, obs))
        self.assertEqual(self.policy.constraints(obs)["max_action_distance"], 2)
        self.assertFalse(self.policy.constraints(obs)["model_allowed"])
        obs["terrain"] = [{"name": "fire", "pos": [1, 65, 0]}]
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_low_health_does_not_harvest_wheat_or_prospect_unknown_chests(self):
        obs = observed(health=6, food=4)
        self.crop(obs, pos=[1, 64, 0], name="wheat", complete=True)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertIsNotNone(self.policy.validate(action("harvest_crop", pos=[1, 64, 0], expected_name="wheat"), obs))
        self.assertIsNotNone(self.policy.validate(action("inspect_container", pos=[1, 64, 0]), obs))

    def test_low_health_with_regeneration_food_waits_instead_of_collecting(self):
        obs = observed(health=6, food=20)
        self.crop(obs, pos=[1, 64, 0], complete=True)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertFalse(self.policy.constraints(obs)["model_allowed"])

    def test_food_seventeen_is_not_described_as_waiting_for_regeneration(self):
        obs = observed(health=6, food=17)
        decision = self.policy.choose(obs, self.baseline)
        self.assertEqual(decision.name, "wait")
        self.assertIn("cannot regenerate", decision.reason)
        self.assertIn("below 18", self.policy.constraints(obs)["reason"])
        obs["status"]["food"] = 18
        self.assertIn("Observe natural regeneration", self.policy.choose(obs, self.baseline).reason)

    def canopy(self, obs):
        origin = obs["status"]["pos"]
        x, y, z = self.policy._cell(origin)
        obs["footing"] = {"pos": [x, y - 1, z], "name": "oak_leaves", "properties": {"waterlogged": False},
                          "fresh": True, "verified": True, "source": "api_verified"}
        obs["footing_origin"] = list(origin)
        return obs

    def test_low_health_empty_inventory_can_escape_current_verified_canopy(self):
        obs = self.canopy(observed(health=6, food=17, pos=[-4.5, 124, 5.7]))
        before = copy.deepcopy(obs)
        decision = self.policy.choose(obs, self.baseline)
        self.assertEqual((decision.name, decision.args, decision.goal), ("canopy_escape", {}, "safety"))
        self.assertIsNone(self.policy.validate(decision, obs))
        self.assertEqual(self.policy.constraints(obs)["scenario"], "canopy_escape")
        self.assertFalse(self.policy.constraints(obs)["model_allowed"])
        self.assertEqual(obs, before)

    def test_canopy_requires_exact_fresh_dry_support_not_a_diagnostic_leaf(self):
        for change in ("stale", "unverified", "wrong_source", "solid", "wrong_cell", "wet", "unknown_dry", "numeric_dry", "moved", "partial"):
            obs = self.canopy(observed(health=6, food=17))
            if change == "stale": obs["footing"]["fresh"] = False
            elif change == "unverified": obs["footing"]["verified"] = False
            elif change == "wrong_source": obs["footing"]["source"] = "memory"
            elif change == "solid": obs["footing"]["name"] = "dirt"
            elif change == "wrong_cell": obs["footing"]["pos"][1] += 1
            elif change == "wet": obs["footing"]["properties"]["waterlogged"] = True
            elif change == "unknown_dry": obs["footing"]["properties"] = {}
            elif change == "numeric_dry": obs["footing"]["properties"]["waterlogged"] = 0
            elif change == "moved": obs["footing_origin"][0] += .3
            elif change == "partial": obs["scope"] = "safety"
            with self.subTest(change=change):
                self.assertNotEqual(self.policy.choose(obs, self.baseline).name, "canopy_escape")
                self.assertIsNotNone(self.policy.validate(action("canopy_escape"), obs))

    def test_canopy_meal_hostile_and_existing_cooldown_take_priority(self):
        obs = self.canopy(observed(health=6, food=17))
        obs["counts"] = {"apple": 1}
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "eat_food")
        obs["entities"] = [{"type": "hostile", "name": "skeleton", "pos": [12.5, 64, .5]}]
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "retreat_from")
        self.assertIsNotNone(self.policy.validate(action("canopy_escape"), obs))
        obs["entities"] = []
        cooldown = Decision("wait", {"seconds": 5}, "Do not repeat the unchanged failed search", "safety")
        self.assertIs(self.policy.choose(obs, cooldown), cooldown)

    def test_canopy_escape_precedes_normal_resource_work(self):
        obs = self.canopy(observed())
        self.assertEqual(self.policy.choose(obs, action("gather_block", pos=[2, 64, 0], expected_name="oak_log")).name, "canopy_escape")
        self.assertEqual(self.policy.validate(action("gather_block", pos=[2, 64, 0], expected_name="oak_log"), obs), "canopy_escape_required")

    def trunk_scene(self):
        # Minimal deterministic extract of canopy-scene-20261116T090539Z:
        # the bot descended from leaves onto a natural trunk, not the ground.
        obs = observed(health=6, food=17, pos=[4.3, 121, 6.5], is_day=False, time_of_day=20149)
        obs["footing"] = {"pos": [4, 120, 6], "name": "oak_log", "properties": {"axis": "y"},
                          "fresh": True, "verified": True, "source": "api_verified"}
        obs["footing_origin"] = [4.3, 121, 6.5]
        obs["blocks"] = [{"name": "oak_leaves", "pos": [x, y, z],
                          "properties": {"waterlogged": False, "persistent": False, "distance": 1 if y == 120 else 2}}
                         for x, z in ((3, 6), (5, 6), (4, 5), (4, 7)) for y in (120, 121)]
        context = {"canopy_recovery": {"dimension": "overworld", "life": None, "origin": [-4.5, 124, 5.7],
                                       "steps": 18, "evacuating": True, "visited": [[4, 121, 6]], "excluded_until": {}}}
        return obs, context

    def test_natural_trunk_snapshot_continues_current_evacuation_even_at_night(self):
        obs, context = self.trunk_scene()
        before = copy.deepcopy((obs, context))
        decision = self.policy.choose(obs, self.baseline, context=context)
        self.assertEqual(decision.name, "canopy_escape")
        self.assertEqual(self.policy.constraints(obs, context)["scenario"], "canopy_escape")
        self.assertIsNone(self.policy.validate(decision, obs, context))
        self.assertEqual((obs, context), before)

    def test_logs_and_wood_continue_only_an_existing_current_life_evacuation(self):
        for name in ("oak_log", "oak_wood", "birch_log", "mangrove_wood"):
            obs, context = self.trunk_scene()
            obs["footing"]["name"] = name
            with self.subTest(name=name):
                self.assertEqual(self.policy.choose(obs, self.baseline, context=context).name, "canopy_escape")
                self.assertNotEqual(self.policy.choose(obs, self.baseline).name, "canopy_escape")
                self.assertEqual(self.policy.validate(action("canopy_escape"), obs), "canopy_support_unverified")

    def test_trunk_recovery_cannot_use_old_life_dimension_unknown_or_completed_state(self):
        for change in ("life", "dimension", "unknown", "completed", "ground", "invalid_origin", "not_started"):
            obs, context = self.trunk_scene()
            state = context["canopy_recovery"]
            if change == "life": state["life"] = {"time": "previous_life"}
            elif change == "dimension": state["dimension"] = "the_nether"
            elif change == "unknown": state["unknown"] = True
            elif change == "completed": state["retreat_complete"] = True
            elif change == "ground": state["evacuation_stage"] = "ground"
            elif change == "invalid_origin": state["origin"] = [None, 124, 5.7]
            elif change == "not_started": state.update(steps=0, evacuating=False)
            with self.subTest(change=change):
                self.assertNotEqual(self.policy.choose(obs, self.baseline, context=context).name, "canopy_escape")

    def test_failed_first_canopy_step_still_continues_from_fresh_trunk(self):
        obs, context = self.trunk_scene()
        context["canopy_recovery"].update(steps=1, evacuating=False)
        self.assertEqual(self.policy.choose(obs, self.baseline, context=context).name, "canopy_escape")
        cooldown = Decision("wait", {"seconds": 2}, "Preserve bounded failed-route cooldown", "safety")
        self.assertIs(self.policy.choose(obs, cooldown, context=context), cooldown)

    def test_trunk_continuation_still_requires_fresh_support_and_preserves_hostile_priority(self):
        obs, context = self.trunk_scene()
        obs["footing"]["fresh"] = False
        self.assertNotEqual(self.policy.choose(obs, self.baseline, context=context).name, "canopy_escape")
        obs["footing"]["fresh"] = True
        obs["entities"] = [{"type": "hostile", "name": "skeleton", "pos": [10, 121, 6]}]
        self.assertEqual(self.policy.choose(obs, self.baseline, context=context).name, "retreat_from")
        self.assertIsNotNone(self.policy.validate(action("canopy_escape"), obs, context))

    def test_tree_trunk_never_qualifies_as_cautious_bootstrap_or_ground_food_search(self):
        for recovering in (True, False):
            obs, context = self.trunk_scene()
            obs["status"].update(is_day=True, time_of_day=3000)
            obs["local_geometry_origin"] = list(obs["status"]["pos"])
            obs["safe_stand_candidates"] = [{"pos": [5.5, 121, 6.5], "support": "dirt"}]
            if not recovering:
                context.pop("canopy_recovery")
            with self.subTest(recovering=recovering):
                self.assertIsNone(self.policy._food_search(obs, context))
                context["food_recovery"] = {"needs_ai": True, "steps": 8}
                self.assertFalse(self.policy.cautious_bootstrap(obs, context))
                self.assertIsNotNone(self.policy.validate(action("run_survival_task", task="gather", resource="logs", count=2), obs, context))

    def test_normal_ground_log_does_not_initiate_canopy_escape(self):
        obs, _ = self.trunk_scene()
        obs["status"].update(health=20, food=20, is_day=True, time_of_day=3000)
        obs["blocks"] = []
        self.assertFalse(self.policy._canopy(obs))
        self.assertIsNone(self.policy.choose(obs, action("gather_block", pos=[6, 121, 6], expected_name="oak_log")))

    def test_ranged_danger_is_not_hidden_by_a_nearer_out_of_range_spider(self):
        obs = observed(health=6, food=17)
        obs["entities"] = [{"type": "hostile", "name": "spider", "pos": [9.5, 64, .5]},
                           {"type": "hostile", "name": "skeleton", "pos": [15.5, 64, .5]}]
        decision = self.policy.choose(obs, self.baseline)
        self.assertEqual(decision.name, "retreat_from")
        self.assertEqual(decision.args["pos"], [15.5, 64, .5])
        self.assertIsNone(self.policy.validate(decision, obs))

    def test_unshielded_ranged_threat_prevents_fighting_instead_of_withdrawing(self):
        obs = observed()
        obs["entities"] = [{"type": "hostile", "name": "skeleton", "pos": [5, 64, 0]}]
        with patch("core.daily_policy.combat_options", return_value={"allowed": True, "shield": False,
                "target": {"id": 1, "name": "skeleton"}}):
            self.assertEqual(self.policy.choose(obs, self.baseline).name, "retreat_from")

    def test_ranged_summary_without_position_blocks_food_and_canopy(self):
        obs = self.canopy(observed(health=6, food=17, nearest_hostile={"name": "stray", "dist": 12}))
        self.assertEqual(self.policy.constraints(obs)["scenario"], "hostile_position_unknown")
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def ground_search(self, obs):
        self.canopy(obs)
        obs["footing"]["name"] = "grass_block"
        obs["local_geometry_origin"] = list(obs["status"]["pos"])
        obs["safe_stand_candidates"] = [{"pos": [1.5, 64, .5], "support": "grass_block"},
                                         {"pos": [.5, 64, 1.5], "support": "dirt"}]
        return obs

    def test_low_health_food_search_is_one_fresh_ground_move_not_a_route(self):
        obs = self.ground_search(observed(health=6, food=17))
        context = {"food_recovery": {"steps": 0, "visited": [], "excluded_until": {}}}
        before = copy.deepcopy((obs, context))
        decision = self.policy.choose(obs, self.baseline, context=context)
        self.assertEqual(decision.name, "position_for_task")
        self.assertEqual(decision.goal, "food_recovery")
        self.assertEqual(self.policy.constraints(obs, context)["scenario"], "food_recovery")
        self.assertIsNone(self.policy.validate(decision, obs, context))
        self.assertEqual((obs, context), before)

    def test_critical_hunger_recovery_can_reconsider_one_adjacent_terrace(self):
        obs = self.ground_search(observed(pos=[.7, 64, .5], health=1.33, food=17))
        obs["safe_stand_candidates"] = [{"pos": [.5, 65, 1.5], "support": "stone"},
                                         {"pos": [.5, 65, 2.5], "support": "stone"}]
        context = {"food_recovery": {"steps": 0, "needs_ai": True}}
        decision = self.policy.choose(obs, self.baseline, context=context)
        self.assertEqual((decision.name, decision.args), ("position_for_task", {"pos": [.5, 65, 1.5]}))
        self.assertIsNone(self.policy.validate(decision, obs, context))
        context["food_recovery"]["steps"] = 8
        self.assertEqual(self.policy.choose(obs, self.baseline, context=context).name, "wait")

    def test_critical_food_search_can_clear_observed_dirt_headroom_at_dead_end(self):
        obs = self.ground_search(observed(health=1.33, food=17))
        obs["safe_stand_candidates"] = []
        obs["food_headroom_candidates"] = [{"pos": [1, 65, 0], "name": "dirt"}]
        decision = self.policy.choose(obs, self.baseline)
        self.assertEqual((decision.name, decision.goal), ("clear_food_headroom", "food_recovery"))
        self.assertIsNone(self.policy.validate(decision, obs))
        self.assertNotIn(decision.name, self.policy.constraints({**obs, "fresh": False})["allowed_actions"])
        obs["status"]["food"] = 11
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_food_search_skips_visited_failed_and_current_cells(self):
        obs = self.ground_search(observed(health=6, food=17))
        context = {"food_recovery": {"steps": 2, "visited": [[0, 64, 1]], "excluded_until": {}}}
        decision = self.policy.choose(obs, self.baseline, context=context)
        self.assertEqual(decision.args["pos"], [1.5, 64, .5])
        context["food_recovery"]["excluded_until"] = {"1,64,0": 9999999999}
        self.assertEqual(self.policy.choose(obs, self.baseline, context=context).name, "wait")
        self.assertEqual(self.policy.constraints(obs, context)["scenario"], "food_recovery_blocked")

    def test_food_search_stops_after_eight_steps_and_never_claims_regeneration(self):
        obs = self.ground_search(observed(health=6, food=17))
        context = {"food_recovery": {"steps": 8}}
        decision = self.policy.choose(obs, self.baseline, context=context)
        self.assertEqual(decision.name, "wait")
        self.assertIn("cannot regenerate", decision.reason)
        self.assertEqual(self.policy.constraints(obs, context)["scenario"], "food_recovery_blocked")

    def test_food_search_rejects_night_stale_geometry_height_leaf_and_far_targets(self):
        for change in ("night", "moved", "height", "leaf", "far", "water", "safety"):
            obs = self.ground_search(observed(health=6, food=17))
            obs["safe_stand_candidates"] = obs["safe_stand_candidates"][:1]
            if change == "night": obs["status"]["is_day"] = False
            elif change == "moved": obs["local_geometry_origin"][0] += .3
            elif change == "height": obs["safe_stand_candidates"][0]["pos"][1] += 1
            elif change == "leaf": obs["safe_stand_candidates"][0]["support"] = "oak_leaves"
            elif change == "far": obs["safe_stand_candidates"][0]["pos"][0] = 4.5
            elif change == "water": obs["blocks"] = [{"name": "water", "pos": [1, 64, 0]}]
            elif change == "safety": obs["scope"] = "safety"
            with self.subTest(change=change):
                self.assertNotEqual(self.policy.choose(obs, self.baseline).name, "position_for_task")

    def test_partial_safety_does_not_claim_food_search_has_been_exhausted(self):
        obs = observed(health=6, food=17)
        obs["observation_scope"] = "safety"
        self.assertEqual(self.policy.constraints(obs)["scenario"], "food_observation_required")
        self.assertEqual(self.policy.choose(obs, self.baseline).goal, "observe")

    def test_verified_food_is_taken_locally_before_search_or_canopy_escape(self):
        obs = self.canopy(observed(health=6, food=17))
        self.crop(obs, [1, 64, 0], complete=True)
        decision = self.policy.choose(obs, self.baseline)
        self.assertEqual(decision.name, "harvest_crop")
        self.assertFalse(self.policy.constraints(obs)["model_allowed"])
        self.assertIsNone(self.policy.validate(decision, obs))

    def test_food_drop_requires_real_item_shape_and_complete_short_standing_corridor(self):
        obs = self.ground_search(observed(health=6, food=17))
        obs["entities"] = [{"id": 3, "type": "item", "name": "item", "item": "applex1", "pos": [1.5, 64, .5]}]
        cells = {(1, 64, 0), (0, 64, 0), (2, 64, 0), (1, 63, 0), (1, 65, 0), (1, 64, -1), (1, 64, 1),
                 (0, 63, 0), (0, 65, 0)}
        obs["blocks"] = [{"pos": list(pos), "name": "dirt" if pos[1] == 63 else "air"} for pos in cells]
        decision = self.policy.choose(obs, self.baseline)
        self.assertEqual((decision.name, decision.args), ("collect_drops", {"entity_id": 3, "expected_item": "apple"}))
        self.assertIsNone(self.policy.validate(decision, obs))
        obs["entities"][0]["item"] = "applex0"
        self.assertIsNotNone(self.policy.validate(decision, obs))
        obs["entities"][0]["item"] = "applex1"
        obs["blocks"] = [row for row in obs["blocks"] if row["pos"] != [0, 65, 0]]
        self.assertIsNotNone(self.policy.validate(decision, obs))

    def test_exhausted_food_search_permits_only_small_wooden_goals_on_safe_ground(self):
        obs = self.ground_search(observed(health=6, food=17))
        context = {"food_recovery": {"steps": 8, "needs_ai": True}}
        self.assertTrue(self.policy.cautious_bootstrap(obs, context))
        self.assertIsNone(self.policy.choose(obs, self.baseline, context=context))
        limits = self.policy.constraints(obs, context)
        self.assertEqual(limits["scenario"], "cautious_bootstrap")
        self.assertEqual(limits["allowed_actions"], ["run_survival_task", "wait"])
        for args in ({"task": "gather", "resource": "logs", "count": 8},
                     {"task": "craft", "item": "wooden_pickaxe", "count": 1}):
            self.assertIsNone(self.policy.validate(action("run_survival_task", **args), obs, context))
        for args in ({"task": "gather", "resource": "logs", "count": 9}, {"task": "gather", "resource": "coal"},
                     {"task": "starter_tools"}, {"task": "food"}, {"task": "craft", "item": "stone_pickaxe"}):
            self.assertIsNotNone(self.policy.validate(action("run_survival_task", **args), obs, context))

    def test_bootstrap_rechecks_full_health_hunger_day_support_and_all_hostiles_sixteen(self):
        context = {"food_recovery": {"needs_ai": True}}
        for change in ("health", "food", "night", "wet", "leaf", "partial", "enemy", "saturated", "ready", "dead"):
            obs = self.ground_search(observed(health=6, food=17))
            if change == "health": obs["status"]["health"] = 4
            elif change == "food": obs["status"]["food"] = 11
            elif change == "night": obs["status"]["is_day"] = False
            elif change == "wet": obs["status"]["in_water"] = True
            elif change == "leaf": obs["footing"]["name"] = "oak_leaves"
            elif change == "partial": obs["scope"] = "safety"
            elif change == "enemy": obs["entities"] = [{"type": "hostile", "name": "zombie", "pos": [15, 64, 0]}]
            elif change == "saturated": obs["entities"] = [{"type": "passive", "name": "cow"}] * 40
            elif change == "ready": obs["status"]["ready"] = False
            elif change == "dead": obs["status"]["alive"] = False
            with self.subTest(change=change):
                self.assertFalse(self.policy.cautious_bootstrap(obs, context))

    def test_bootstrap_atomic_requires_trusted_goal_and_current_visible_reachable_log(self):
        obs = self.ground_search(observed(health=6, food=17))
        obs["blocks"] = [{"name": "oak_log", "pos": [2, 64, 0], "can_see": True, "can_dig": True}]
        context = {"food_recovery": {"needs_ai": True}}
        gather = action("gather_block", pos=[2, 64, 0], expected_name="oak_log")
        self.assertIsNotNone(self.policy.validate(gather, obs, context))
        context["cautious_bootstrap"] = True
        self.assertIsNone(self.policy.validate(gather, obs, context))
        for field in ("can_see", "can_dig"):
            obs["blocks"][0][field] = None
            self.assertIsNotNone(self.policy.validate(gather, obs, context))
            obs["blocks"][0][field] = True
        for name, args in (("move_to", {"pos": [2, 64, 0]}), ("clear_foliage", {"pos": [2, 64, 0], "expected_name": "oak_leaves"}),
                           ("hunt_animal", {"entity_id": 1, "expected_name": "cow"}), ("craft_item", {"item": "stone_pickaxe", "count": 1})):
            self.assertIsNotNone(self.policy.validate(action(name, **args), obs, context))

    def test_bootstrap_tool_crafting_requires_nearby_table_not_implicit_approach(self):
        obs = self.ground_search(observed(health=6, food=17))
        context = {"food_recovery": {"needs_ai": True}, "cautious_bootstrap": True}
        craft = action("craft_item", item="wooden_pickaxe", count=1)
        self.assertIsNotNone(self.policy.validate(craft, obs, context))
        obs["blocks"] = [{"name": "crafting_table", "pos": [10, 64, 0]}]
        self.assertIsNotNone(self.policy.validate(craft, obs, context))
        obs["blocks"][0]["pos"] = [2, 64, 0]
        self.assertIsNone(self.policy.validate(craft, obs, context))

    def test_stale_lifecycle_mode_hazard_and_airborne_never_release_food_plan(self):
        cases = [({"alive": False}, "dead"), ({"ready": False}, "not_ready"), ({"busy": "dig"}, "busy"),
                 ({"dimension": "the_nether"}, "unsafe_dimension"), ({"gamemode": "creative"}, "unsafe_mode"),
                 ({"in_lava": True}, "environment_hazard"), ({"on_fire": True}, "environment_hazard"),
                 ({"submerged": True}, "environment_hazard"), ({"on_ground": False}, "unstable"),
                 ({"time_of_day": float("nan")}, "observation_required")]
        for fields, scenario in cases:
            obs = observed(food=4, **fields)
            self.crop(obs)
            with self.subTest(scenario=scenario, fields=fields):
                self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
                self.assertEqual(self.policy.constraints(obs)["scenario"], scenario)
                self.assertIsNotNone(self.policy.validate(action("harvest_crop", pos=[2, 64, 0], expected_name="carrots"), obs))
        self.assertEqual(self.policy.choose({"fresh": False}, self.baseline).name, "wait")

    def test_hostile_retreat_and_harness_cooldown_take_priority(self):
        obs = observed(food=4, health=4)
        obs["counts"] = {"bread": 1}
        obs["entities"] = [{"type": "hostile", "name": "zombie", "pos": [4, 64, 0]}]
        retreat = self.policy.choose(obs, self.baseline)
        self.assertEqual(retreat.name, "retreat_from")
        self.assertIsNone(self.policy.validate(retreat, obs))
        cooldown = Decision("wait", {"seconds": 5}, "No route; preserve cooldown", "safety")
        self.assertIs(self.policy.choose(obs, cooldown), cooldown)
        obs["entities"] = []
        obs["status"]["nearest_hostile"] = {"dist": 3, "name": "zombie"}
        self.assertEqual(self.policy.constraints(obs)["scenario"], "hostile_position_unknown")
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_baseline_relay_and_safety_wait_are_not_replaced_by_resources(self):
        obs = observed()
        relay = Decision("retreat_from", {"pos": [-5, 64, 0], "distance": 8}, "Continue relay", "safety")
        self.assertIs(self.policy.choose(obs, relay), relay)
        cooldown = Decision("wait", {"seconds": 5}, "Continue bounded cooldown", "safety")
        self.assertIs(self.policy.choose(obs, cooldown), cooldown)
        resource = action("gather_block", pos=[2, 64, 0], expected_name="oak_log")
        self.assertIsNone(self.policy.choose(obs, resource))

    def test_night_bed_uses_local_sleep_without_model(self):
        obs = observed(is_day=False, time_of_day=14000)
        obs["blocks"] = [{"name": "white_bed", "pos": [2, 64, 0], "properties": {"occupied": False}}]
        decision = self.policy.choose(obs, self.baseline)
        self.assertEqual(decision.name, "auto_sleep")
        self.assertIsNone(self.policy.validate(decision, obs))
        obs["blocks"][0]["properties"]["occupied"] = True
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_dusk_returns_along_fresh_short_shelter_route_to_its_center(self):
        obs = observed(time_of_day=11000)
        self.shelter(obs)
        decision = self.policy.choose(obs, self.baseline)
        self.assertEqual(decision.name, "move_to")
        self.assertEqual(decision.args, {"pos": [3.5, 64, .5], "range": .5})
        self.assertIn("terrain evidence", decision.reason)
        self.assertIsNone(self.policy.validate(decision, obs))
        changed = action("move_to", pos=[4.5, 64, .5], range=.5)
        self.assertIsNotNone(self.policy.validate(changed, obs))

    def test_return_home_opens_reobserves_moves_reobserves_then_closes(self):
        obs = observed(time_of_day=11000)
        shelter = self.gated_shelter(obs)
        before = copy.deepcopy(obs)
        opened = self.policy.choose(obs, self.baseline)
        self.assertEqual(opened.name, "set_door")
        self.assertEqual(opened.args, {"pos": [0, 64, -2], "open": True})
        self.assertIsNone(self.policy.validate(opened, obs))
        self.assertIsNotNone(self.policy.validate(action("move_to", pos=[.5, 64, .5], range=.5), obs))
        self.assertEqual(obs, before)
        shelter["door"].update(open=True, complete=False)
        shelter.update(geometry_complete=False, shelter_verified=False)
        # A door update alone does not make the old blocked route traversable.
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        shelter["return_route"].update(verified=True)
        shelter["return_route"].pop("blocked_door")
        moved = self.policy.choose(obs, self.baseline)
        self.assertEqual(moved.name, "move_to")
        self.assertEqual(moved.args, {"pos": [.5, 64, .5], "range": .5})
        self.assertIsNone(self.policy.validate(moved, obs))
        self.assertIsNotNone(self.policy.validate(opened, obs))
        # Only current inside evidence allows the separate close decision.
        shelter["inside"] = True
        obs["status"]["pos"] = [.5, 64, .5]
        closed = self.policy.choose(obs, self.baseline)
        self.assertEqual(closed.name, "set_door")
        self.assertEqual(closed.args, {"pos": [0, 64, -2], "open": False})
        self.assertIsNone(self.policy.validate(closed, obs))
        shelter["door"].update(open=False, complete=True)
        shelter.update(geometry_complete=True, shelter_verified=True)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertIsNotNone(self.policy.validate(closed, obs))

    def test_close_current_open_door_precedes_sleep_and_never_toggles_closed_door(self):
        obs = observed(is_day=False, time_of_day=14000)
        shelter = self.gated_shelter(obs, opened=True, inside=True)
        obs["blocks"] = [{"name": "white_bed", "pos": [1, 64, 0], "properties": {"occupied": False}}]
        self.assertEqual(self.policy.choose(obs, self.baseline).args, {"pos": [0, 64, -2], "open": False})
        self.assertIsNotNone(self.policy.validate(action("auto_sleep"), obs))
        shelter["door"].update(open=False, complete=True)
        shelter.update(geometry_complete=True, shelter_verified=True)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "auto_sleep")

    def test_inside_far_corner_requires_fresh_route_before_repositioning_to_close(self):
        obs = observed(time_of_day=11000)
        shelter = self.gated_shelter(obs, opened=True, inside=True)
        obs["status"]["pos"] = [1.5, 64, 1.5]
        shelter.pop("return_route")
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        shelter["return_route"] = {"fresh": True, "verified": True, "dimension": "overworld",
                                   "origin": [1.5, 64, 1.5], "path": [[0, 64, 1], [0, 64, 0]]}
        decision = self.policy.choose(obs, self.baseline)
        self.assertEqual(decision.name, "move_to")
        self.assertEqual(decision.args, {"pos": [.5, 64, .5], "range": .5})
        self.assertIsNone(self.policy.validate(decision, obs))
        obs["status"]["pos"] = [.5, 64, .5]
        self.assertEqual(self.policy.choose(obs, self.baseline).args, {"pos": [0, 64, -2], "open": False})

    def test_door_opening_never_bypasses_hostile_or_harness_retreat_cooldown(self):
        obs = observed(time_of_day=11000)
        self.gated_shelter(obs)
        obs["entities"] = [{"type": "hostile", "name": "zombie", "pos": [3, 64, -2]}]
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "retreat_from")
        self.assertEqual(self.policy.validate(action("set_door", pos=[0, 64, -2], open=True), obs), "near_hostile")
        cooldown = Decision("wait", {"seconds": 5}, "Retreat route cooldown", "safety")
        self.assertIs(self.policy.choose(obs, cooldown), cooldown)

    def test_door_steps_require_complete_current_structure_and_actual_wooden_frame(self):
        for problem in ("stale", "dimension", "structure", "frame", "open_unknown", "fractional_pos",
                        "iron_door", "wrong_facing", "missing_door", "unknown_inside"):
            obs = observed(time_of_day=11000)
            shelter = self.gated_shelter(obs)
            if problem == "stale": shelter["fresh"] = False
            if problem == "dimension": shelter["dimension"] = "the_nether"
            if problem == "structure": shelter["structure_complete"] = False
            if problem == "frame": shelter["door"]["frame_verified"] = False
            if problem == "open_unknown": shelter["door"]["open"] = "false"
            if problem == "fractional_pos": shelter["door"]["pos"] = [0.5, 64, -2]
            if problem == "iron_door": shelter["door"]["name"] = "iron_door"
            if problem == "wrong_facing": shelter["door"]["facing"] = "east"
            if problem == "missing_door": shelter.pop("door")
            if problem == "unknown_inside": shelter.pop("inside")
            with self.subTest(problem=problem):
                self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_opening_requires_sole_verified_door_obstruction_on_fresh_short_route(self):
        for problem in ("blocked_unverified", "blocked_open", "other_cells_unknown", "different_door", "outside_path",
                        "origin_changed", "route_stale", "route_wrong_dimension", "hazard", "too_far"):
            obs = observed(time_of_day=11000)
            shelter = self.gated_shelter(obs)
            route = shelter["return_route"]
            if problem == "blocked_unverified": route["blocked_door"]["verified"] = False
            if problem == "blocked_open": route["blocked_door"]["open"] = True
            if problem == "other_cells_unknown": route.pop("all_other_body_and_support_verified")
            if problem == "different_door": route["blocked_door"]["pos"] = [1, 64, -2]
            if problem == "outside_path":
                route["path"] = [[1, 64, -3], [1, 64, -2], [1, 64, -1], [1, 64, 0]]
            if problem == "origin_changed": route["origin"] = [.5, 64, -1.5]
            if problem == "route_stale": route["fresh"] = False
            if problem == "route_wrong_dimension": route["dimension"] = "the_nether"
            if problem == "hazard": obs["blocks"] = [{"name": "lava", "pos": [0, 63, -2]}]
            if problem == "too_far":
                obs["status"]["pos"] = [.9, 64, -4.9]
                route["origin"] = list(obs["status"]["pos"])
                route["path"] = [[0, 64, z] for z in (-4, -3, -2, -1)]
            with self.subTest(problem=problem):
                self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_open_door_route_still_needs_current_origin_and_no_contradiction(self):
        obs = observed(time_of_day=11000)
        shelter = self.gated_shelter(obs, opened=True)
        shelter.pop("structure_complete")  # Accept the collector's nested geometry field too.
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "move_to")
        shelter["door"]["open"] = False
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        shelter["door"]["open"] = True
        obs["status"]["pos"] = [1.5, 64, -2.5]
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_daytime_does_not_start_automatic_home_door_actions(self):
        obs = observed()
        self.gated_shelter(obs)
        self.assertIsNone(self.policy.choose(obs, self.baseline))

    def test_memory_or_geometry_without_route_never_authorizes_return(self):
        obs = observed(time_of_day=11000)
        obs["memory"] = {"shelter": {"pos": [3, 64, 0], "verified": True}}
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        shelter = self.shelter(obs)
        shelter.pop("return_route")
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertFalse(self.policy.constraints(obs)["model_allowed"])

    def test_route_rejects_stale_changed_origin_dimension_nonadjacent_or_height(self):
        for problem in ("stale", "origin", "dimension", "gap", "height", "too_long", "outside", "hazard"):
            obs = observed(time_of_day=11000)
            shelter = self.shelter(obs)
            route = shelter["return_route"]
            if problem == "stale": route["fresh"] = False
            if problem == "origin": route["origin"] = [1.5, 64, .5]
            if problem == "dimension": route["dimension"] = "the_nether"
            if problem == "gap": route["path"] = [[3, 64, 0]]
            if problem == "height": route["path"] = [[1, 65, 0], [2, 65, 0]]
            if problem == "too_long": route["path"] = [[x, 64, 0] for x in range(1, 6)]
            if problem == "outside": shelter["pos"] = [7, 64, 0]
            if problem == "hazard": obs["blocks"] = [{"name": "lava", "pos": [2, 63, 0]}]
            with self.subTest(problem=problem):
                self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_inside_or_very_near_shelter_waits_without_false_move_progress(self):
        obs = observed(time_of_day=11000)
        self.shelter(obs, inside=True)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        obs = observed(time_of_day=11000, pos=[1.5, 64, .5])
        self.shelter(obs, path=[[2, 64, 0]])
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")

    def test_dusk_does_not_release_critical_food_hunting_or_resource_work(self):
        obs = observed(food=3, time_of_day=11000)
        self.crop(obs)
        self.assertEqual(self.policy.choose(obs, self.baseline).name, "wait")
        self.assertEqual(self.policy.validate(action("harvest_crop", pos=[2, 64, 0], expected_name="carrots"), obs),
                         "night_requires_verified_sleep_or_shelter_route")

    def test_pure_policy_does_not_mutate_observation_or_use_network(self):
        obs = observed(food=4)
        self.crop(obs)
        before = copy.deepcopy(obs)
        with patch("requests.request", side_effect=AssertionError("Policy must not use HTTP")):
            self.policy.choose(obs, self.baseline)
            limits = self.policy.constraints(obs)
            self.policy.validate(action("harvest_crop", pos=[2, 64, 0], expected_name="carrots"), obs)
        self.assertEqual(obs, before)
        self.assertEqual(limits, self.policy.constraints(obs))
        self.assertFalse(limits["route_evidence_is_actual_trajectory"])
        self.assertEqual(limits["shelter_spawn_safety"], "unknown")

    def test_opportunistic_sleep_policy_allows_thunder_and_night_work_without_bed_still_sleeps_near_bed(self):
        opp = DailySafetyPolicy({"sleep_policy": "opportunistic"})
        thunder_day = observed(is_day=True, thundering=True, time_of_day=3000)
        self.assertIsNone(opp.choose(thunder_day, self.baseline))
        self.assertIsNone(opp.validate(action("gather_block", pos=[2, 64, 0], expected_name="oak_log"), thunder_day))
        self.assertTrue(opp.constraints(thunder_day)["model_allowed"])

        night_no_bed = observed(is_day=False, time_of_day=14000)
        self.assertIsNone(opp.choose(night_no_bed, self.baseline))
        self.assertIsNone(opp.validate(action("gather_block", pos=[2, 64, 0], expected_name="oak_log"), night_no_bed))

        night_with_bed = observed(is_day=False, time_of_day=14000)
        night_with_bed["blocks"] = [{"name": "white_bed", "pos": [2, 64, 0], "properties": {"occupied": False}}]
        self.assertEqual(opp.choose(night_with_bed, self.baseline).name, "auto_sleep")
        thunder_day["blocks"] = night_with_bed["blocks"]
        self.assertEqual(opp.choose(thunder_day, self.baseline).name, "auto_sleep")

    def test_opportunistic_home_route_has_priority_over_night_work_at_low_health(self):
        policy = DailySafetyPolicy({"sleep_policy": "opportunistic", "low_health_threshold": 8})
        obs = observed(is_day=False, time_of_day=14000, health=7, food=15)
        self.gated_shelter(obs)
        decision = policy.choose(obs, self.baseline)
        self.assertEqual(decision.name, "set_door")
        self.assertIsNone(policy.validate(decision, obs))
        self.assertEqual(policy.constraints(obs)["allowed_actions"], ["wait", "set_door"])
        self.assertIsNotNone(policy.validate(action("gather_resource", pos=[2, 64, 0], expected_name="iron_ore"), obs))

    def test_in_water_chooses_water_escape_and_respects_exhausted_cooldown(self):
        obs = observed(in_water=True, on_ground=False)
        choice = self.policy.choose(obs, self.baseline)
        self.assertEqual(choice.name, "water_escape")
        self.assertEqual(self.policy.constraints(obs)["scenario"], "water_escape")
        self.assertIsNone(self.policy.validate(Decision("water_escape", {}, "exit water", "safety"), obs))
        self.assertEqual(
            self.policy.validate(Decision("water_escape", {"pos": [1, 64, 0]}, "bad", "safety"), obs),
            "water_escape",
        )
        # Submerged WITH in_water=True also allows water_escape (live bot state).
        submerged_wet = observed(in_water=True, submerged=True, on_ground=False)
        self.assertEqual(self.policy.choose(submerged_wet, self.baseline).name, "water_escape")
        # Exhausted recovery falls back to environment_hazard wait unless head is actively submerged.
        exhausted_ctx = {"water_recovery": {"exhausted": True}}
        self.assertEqual(self.policy.choose(obs, self.baseline, context=exhausted_ctx).name, "wait")
        self.assertEqual(self.policy.constraints(obs, context=exhausted_ctx)["scenario"], "environment_hazard")
        self.assertEqual(self.policy.choose(submerged_wet, self.baseline, context=exhausted_ctx).name, "water_escape")

    def test_opportunistic_releases_after_food_or_boxed_retreat_exhausted(self):
        opp = DailySafetyPolicy({"low_health_threshold": 8, "food_threshold": 16, "threat_radius": 8, "sleep_policy": "opportunistic"})
        food_blocked = observed(health=6, food=10)
        wait_strict = self.policy.choose(food_blocked, self.baseline, context={"food_recovery": {"needs_ai": True}})
        self.assertEqual((wait_strict.name, wait_strict.args.get("seconds")), ("wait", 1))
        ctx_food = {"food_recovery": {"needs_ai": True}}
        self.assertIsNone(opp.choose(food_blocked, self.baseline, context=ctx_food))
        self.assertTrue(opp.constraints(food_blocked, ctx_food)["model_allowed"])
        self.assertIsNone(opp.validate(action("move_to", pos=[4, 64, 0]), food_blocked, ctx_food))
        # Distant hostile (dist 10 > threat_radius 8) must not freeze low-health opportunistic recovery.
        distant_hostile = observed(health=3, food=10)
        distant_hostile["entities"] = [{"id": 21, "type": "hostile", "name": "zombie", "pos": [10.5, 64.0, 0.5]}]
        self.assertIsNone(opp.choose(distant_hostile, self.baseline, context=ctx_food))

        boxed_obs = observed(health=18, food=18)
        boxed_obs["entities"] = [{"id": 22, "type": "hostile", "name": "spider", "pos": [6.0, 64.0, 0.5]}]
        self.assertEqual(
            opp.choose(boxed_obs, self.baseline, context={"retreat_recovery": {"no_route_exhausted": True}}),
            None,
        )
        close_obs = observed(health=18, food=18)
        close_obs["entities"] = [{"id": 23, "type": "hostile", "name": "spider", "pos": [2.5, 64.0, 0.5]}]
        self.assertEqual(
            opp.choose(close_obs, self.baseline, context={"retreat_recovery": {"no_route_exhausted": True}}).name,
            "retreat_from",
        )

    def test_llm_override_on_block_downgrades_tactical_locks_to_warnings_while_keeping_lava_hard_stop(self):
        over = DailySafetyPolicy({"low_health_threshold": 8, "food_threshold": 16, "threat_radius": 8,
                                  "sleep_policy": "opportunistic", "llm_override_on_block": True})
        wet = observed(in_water=True, on_ground=False)
        self.assertIsNone(over.choose(wet, self.baseline, context={"water_recovery": {"steps": 2}}))
        c_wet = over.constraints(wet, context={"water_recovery": {"steps": 2}})
        self.assertTrue(c_wet["model_allowed"])
        self.assertTrue(any("in_water" in w for w in c_wet["warnings"]))

        hungry = observed(health=5, food=4, is_day=False, time_of_day=14000)
        self.assertIsNone(over.choose(hungry, self.baseline, context={}))
        self.assertTrue(over.constraints(hungry)["model_allowed"])
        self.assertIsNone(over.validate(action("move_to", pos=[4, 64, 0]), hungry))

        lava = observed(in_lava=True, on_ground=False)
        self.assertEqual(over.choose(lava, self.baseline).goal, "recover")
        self.assertFalse(over.constraints(lava)["model_allowed"])


if __name__ == "__main__":
    unittest.main()
